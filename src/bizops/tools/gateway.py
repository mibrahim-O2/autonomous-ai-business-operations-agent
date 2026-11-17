"""ToolGateway: the single choke point every tool call passes through.

Order of checks for each call:
  1. the tool exists
  2. the calling agent is allowed to use it (allow-list)
  3. the arguments validate against the tool's schema
  4. replay: an identical call already completed in this step is returned from
     the record instead of being executed again (safe re-execution after pause)
  5. the approval gate decides allow / require approval / deny
  6. fence check: the worker must still own the run
  7. execution, timed and traced, with failures converted to structured results

Because agents cannot reach tools except through this class, permissions and
approval are enforced below the model: a model cannot talk its way past them.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Any, Protocol

import jsonschema
from pydantic import BaseModel, ValidationError

from ..core.errors import BizOpsError, StaleLease, ToolError
from ..core.models import PlanStep, RiskLevel, RunState, ToolCall, ToolResult
from ..memory.store import bm25_scores
from ..observability import Tracer
from ..security.audit import AuditLog
from ..security.injection import extract_refs, scan_for_injection
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
    def __init__(
        self,
        executors: list[ToolExecutor],
        db: Database,
        tracer: Tracer,
        gate: ApprovalGate | None = None,
        audit: AuditLog | None = None,
        tool_timeout_seconds: float = 30.0,
    ):
        self.db, self.tracer, self.audit, self.tool_timeout_seconds = db, tracer, audit, tool_timeout_seconds
        self.gate: ApprovalGate = gate or AllowAllGate()
        self._executors = list(executors)
        self._specs: dict[str, tuple[ToolSpec, ToolExecutor]] = {}
        # Set by the orchestrator: raises StaleLease if the worker no longer owns the run.
        self.fence_guard: Callable[[RunState], None] | None = None
        # Set by the orchestrator: fenced write of the run state, used for write-ahead intents.
        self.checkpoint: Callable[[RunState], None] | None = None
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

    @staticmethod
    def is_allowed(tool: str, allowed: list[str]) -> bool:
        """Allow-lists may name tools exactly or with shell-style patterns such as 'billing_*'."""
        return any(fnmatch.fnmatchcase(tool, pattern) for pattern in allowed)

    def specs_for(self, allowed: list[str], query: str | None = None, limit: int | None = None) -> list[ToolSpec]:
        """Tools an agent may use. With `query` and `limit`, return only the most relevant ones.

        Tool selection matters once an agent has many tools: offering the model a
        short, relevant list improves its choices and saves context. Relevance is
        BM25 over tool names and descriptions.
        """
        permitted = [spec for name, (spec, _) in self._specs.items() if self.is_allowed(name, allowed)]
        if query is None or limit is None or len(permitted) <= limit:
            return permitted
        scores = bm25_scores(query, [f"{s.name.replace('_', ' ')} {s.description}" for s in permitted])
        ranked = sorted(zip(permitted, scores, strict=True), key=lambda pair: -pair[1])
        return [spec for spec, _ in ranked[:limit]]

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
        if not self.is_allowed(call.tool, allowed):
            return failed(f"agent '{agent}' is not permitted to use '{call.tool}'")
        arguments = call.arguments
        if spec.input_model is not None:
            try:
                arguments = spec.input_model.model_validate(call.arguments).model_dump(mode="json")
            except ValidationError as exc:
                problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
                return failed(f"invalid arguments: {problems}")
        elif spec.input_schema:
            # Tools discovered over MCP describe their arguments with JSON Schema.
            errors = sorted(jsonschema.Draft202012Validator(spec.input_schema).iter_errors(arguments), key=str)
            if errors:
                problems = "; ".join(f"{'.'.join(map(str, e.path)) or 'arguments'}: {e.message}" for e in errors[:3])
                return failed(f"invalid arguments: {problems}")

        fp = fingerprint(call.tool, arguments)
        for earlier in step.tool_calls:
            if earlier.ok and earlier.fingerprint == fp:
                return earlier.model_copy(update={"call_id": call.call_id, "replayed": True})

        # Recovery of an interrupted call. If this step was cut off while a side-effecting call to
        # this tool was in flight, nobody knows whether it landed. Send exactly that call again, with
        # its original arguments and therefore its original idempotency key, so the target either
        # performs it for the first time or recognises it. This holds even when the model words the
        # call differently on the second attempt, which a live model may do.
        intent = step.in_flight
        if spec.side_effect and intent is not None and intent.tool == call.tool:
            pinned_fp = fingerprint(call.tool, intent.arguments)
            if pinned_fp != fp:
                self.tracer.event(
                    state.tenant_id, state.run_id, "tool.intent_recovered", tool=call.tool, step_id=step.step_id
                )
                arguments, fp = intent.arguments, pinned_fp

        verdict = self.gate.evaluate(state, step, agent, spec, call.model_copy(update={"arguments": arguments}), fp)
        if verdict.decision == GateDecision.DENY:
            if self.audit is not None:
                self.audit.record(
                    state.tenant_id,
                    f"agent:{agent}",
                    "tool.denied",
                    call.tool,
                    actor_role="agent",
                    run_id=state.run_id,
                    step=step.action,
                    reason=verdict.reason,
                    approval_id=verdict.approval_id,
                )
            return failed(f"denied: {verdict.reason}", fp=fp)
        if verdict.decision == GateDecision.REQUIRE_APPROVAL:
            raise ApprovalRequired(verdict, call.model_copy(update={"arguments": arguments}), spec.name, spec.risk)

        ctx = ToolContext(
            tenant_id=state.tenant_id,
            run_id=state.run_id,
            db=self.db,
            actor=agent,
            idempotency_key=f"{state.run_id}:{step.step_id}:{fp}",
            fencing_token=state.generation,
        )
        # Last check before acting: a worker whose claim was superseded stops here.
        if self.fence_guard is not None:
            self.fence_guard(state)
        if spec.side_effect and self.checkpoint is not None and state.generation:
            # Write the intent ahead of the call (only for a claimed run; an unclaimed state is not
            # being executed by a worker). The write is fenced, so it is also refused if this
            # worker was superseded.
            step.in_flight = ToolCall(call_id=call.call_id, tool=call.tool, arguments=arguments)
            self.checkpoint(state)
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
            timed_out = False
            try:
                output = await asyncio.wait_for(executor.call(call.tool, arguments, ctx), self.tool_timeout_seconds)
                # A tool may flag an individual result as containing outside-authored text.
                dynamic_untrusted = isinstance(output, dict) and bool(output.pop("_untrusted", False))
                result = ToolResult(
                    call_id=call.call_id,
                    tool=call.tool,
                    ok=True,
                    output=output,
                    untrusted=spec.returns_untrusted or dynamic_untrusted,
                    fingerprint=fp,
                )
            except StaleLease:
                raise  # not a tool failure: this worker must stop, so do not convert it to a result
            except TimeoutError:
                # The outcome is unknown: the call may or may not have landed. It is reported as a
                # retryable failure and the write-ahead intent is kept, so the retry sends this same
                # call under the same idempotency key.
                timed_out = True
                result = ToolResult(
                    call_id=call.call_id,
                    tool=call.tool,
                    ok=False,
                    error=f"timed out after {self.tool_timeout_seconds:g}s",
                    retryable=True,
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
            self._note_untrusted(state, step, call.tool, result.output)
        if spec.side_effect and self.audit is not None:
            # Every action on a business system is on the audit record, with who allowed it.
            self.audit.record(
                state.tenant_id,
                f"agent:{agent}",
                "tool.executed" if result.ok else "tool.failed",
                call.tool,
                actor_role="agent",
                run_id=state.run_id,
                step=step.action,
                risk=spec.risk.value,
                fingerprint=fp,
                allowed_by=verdict.reason,
                approval_id=verdict.approval_id,
                error=result.error,
            )
        step.tool_calls.append(result)
        if not timed_out:
            # The outcome is known now (a failed call is retried under the same key, so it is safe too).
            # The caller checkpoints the record and the cleared intent together.
            step.in_flight = None
        return result

    def _note_untrusted(self, state: RunState, step: PlanStep, tool: str, output: Any) -> None:
        """Record that outside-authored text entered the run, and what it contained that matters later."""
        state.tainted = True
        trusted = extract_refs(state.task.inputs)
        fresh = sorted((extract_refs(output) - trusted) - set(state.untrusted_refs))
        state.untrusted_refs = (state.untrusted_refs + fresh)[:200]
        findings = scan_for_injection(output)
        if findings:
            known = {(f["tool"], f["pattern"]) for f in state.injection_findings}
            new = [{"tool": tool, "step": step.action, **f} for f in findings if (tool, f["pattern"]) not in known]
            state.injection_findings = (state.injection_findings + new)[:50]
            self.tracer.event(
                state.tenant_id,
                state.run_id,
                "security.injection_suspected",
                tool=tool,
                step=step.action,
                patterns=[f["pattern"] for f in findings],
            )
