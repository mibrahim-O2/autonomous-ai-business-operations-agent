"""ToolGateway: the single choke point every tool call passes through.

Order of checks for each call:
  1. the tool exists
  2. the calling agent is allowed to use it (allow-list)
  3. the arguments validate against the tool's schema
  4. replay: an identical call already completed in this step is returned from
     the record instead of being executed again (safe re-execution after pause)
  5. the approval gate decides allow / require approval / deny
  6. execution, timed and traced, with failures converted to structured results

Because agents cannot reach tools except through this class, permissions and
approval are enforced below the model: a model cannot talk its way past them.
"""

from __future__ import annotations

import hashlib
import json
import time
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from ..core.errors import BizOpsError, ToolError
from ..core.models import PlanStep, RiskLevel, RunState, ToolCall, ToolResult
from ..observability import Tracer
from ..storage import Database
from .base import ToolContext, ToolExecutor, ToolSpec


class GateDecision(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class GateVerdict(BaseModel):
    decision: GateDecision
    reason: str = ""
    approval_id: str | None = None


class ApprovalRequired(BizOpsError):
    """Raised to suspend a run until a human decides. Carries what is being asked."""

    def __init__(self, verdict: GateVerdict, call: ToolCall, spec_name: str, risk: RiskLevel):
        super().__init__(verdict.reason or "approval required")
        self.verdict, self.call, self.tool, self.risk = verdict, call, spec_name, risk


class ApprovalGate(Protocol):
    """Extension point for human-in-the-loop control. The policy engine implements it."""

    def evaluate(
        self, state: RunState, step: PlanStep, agent: str, spec: ToolSpec, call: ToolCall, fingerprint: str
    ) -> GateVerdict: ...


class AllowAllGate:
    """Default gate used until an approval policy is configured."""

    def evaluate(
        self, state: RunState, step: PlanStep, agent: str, spec: ToolSpec, call: ToolCall, fingerprint: str
    ) -> GateVerdict:
        return GateVerdict(decision=GateDecision.ALLOW, reason="no approval policy configured")


def fingerprint(tool: str, arguments: dict[str, Any]) -> str:
    canonical = json.dumps({"tool": tool, "arguments": arguments}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:24]


class ToolGateway:
    def __init__(self, executors: list[ToolExecutor], db: Database, tracer: Tracer, gate: ApprovalGate | None = None):
        self.db, self.tracer = db, tracer
        self.gate: ApprovalGate = gate or AllowAllGate()
        self._executors = list(executors)
        self._specs: dict[str, tuple[ToolSpec, ToolExecutor]] = {}
        self.refresh()

    def refresh(self) -> None:
        """Re-read tool lists from every executor (tool discovery)."""
        self._specs = {spec.name: (spec, executor) for executor in self._executors for spec in executor.specs()}

    def add_executor(self, executor: ToolExecutor) -> None:
        self._executors.append(executor)
        self.refresh()

    def spec(self, name: str) -> ToolSpec | None:
        entry = self._specs.get(name)
        return entry[0] if entry else None

    def specs_for(self, allowed: list[str]) -> list[ToolSpec]:
        return [self._specs[name][0] for name in allowed if name in self._specs]

    def all_specs(self) -> list[ToolSpec]:
        return [entry[0] for entry in self._specs.values()]

    async def execute(
        self, state: RunState, step: PlanStep, agent: str, allowed: list[str], call: ToolCall
    ) -> ToolResult:
        def failed(error: str, retryable: bool = False, fp: str = "") -> ToolResult:
            result = ToolResult(
                call_id=call.call_id, tool=call.tool, ok=False, error=error, retryable=retryable, fingerprint=fp
            )
            self.tracer.event(state.tenant_id, state.run_id, "tool.rejected", tool=call.tool, agent=agent, error=error)
            return result

        entry = self._specs.get(call.tool)
        if entry is None:
            return failed(f"unknown tool '{call.tool}'")
        spec, executor = entry
        if call.tool not in allowed:
            return failed(f"agent '{agent}' is not permitted to use '{call.tool}'")
        arguments = call.arguments
        if spec.input_model is not None:
            try:
                arguments = spec.input_model.model_validate(call.arguments).model_dump(mode="json")
            except ValidationError as exc:
                problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
                return failed(f"invalid arguments: {problems}")
        else:
            missing = [k for k in spec.input_schema.get("required", []) if k not in arguments]
            if missing:
                return failed(f"invalid arguments: missing {missing}")

        fp = fingerprint(call.tool, arguments)
        for earlier in step.tool_calls:
            if earlier.ok and earlier.fingerprint == fp:
                return earlier.model_copy(update={"call_id": call.call_id, "replayed": True})

        verdict = self.gate.evaluate(state, step, agent, spec, call.model_copy(update={"arguments": arguments}), fp)
        if verdict.decision == GateDecision.DENY:
            return failed(f"denied by policy: {verdict.reason}", fp=fp)
        if verdict.decision == GateDecision.REQUIRE_APPROVAL:
            raise ApprovalRequired(verdict, call.model_copy(update={"arguments": arguments}), spec.name, spec.risk)

        ctx = ToolContext(tenant_id=state.tenant_id, run_id=state.run_id, db=self.db, actor=agent)
        started = time.perf_counter()
        with self.tracer.span(
            state.tenant_id,
            state.run_id,
            "tool.call",
            tool=call.tool,
            agent=agent,
            risk=spec.risk.value,
            step_id=step.step_id,
        ) as span:
            try:
                output = await executor.call(call.tool, arguments, ctx)
                result = ToolResult(
                    call_id=call.call_id,
                    tool=call.tool,
                    ok=True,
                    output=output,
                    untrusted=spec.returns_untrusted,
                    fingerprint=fp,
                )
            except ToolError as exc:
                result = ToolResult(
                    call_id=call.call_id,
                    tool=call.tool,
                    ok=False,
                    error=str(exc),
                    retryable=exc.retryable,
                    fingerprint=fp,
                )
            except Exception as exc:  # a tool bug must not crash the run
                result = ToolResult(
                    call_id=call.call_id, tool=call.tool, ok=False, error=f"{type(exc).__name__}: {exc}", fingerprint=fp
                )
            result.duration_ms = round((time.perf_counter() - started) * 1000, 2)
            span.update(ok=result.ok, error=result.error)
        if result.untrusted:
            state.tainted = True
        step.tool_calls.append(result)
        return result
