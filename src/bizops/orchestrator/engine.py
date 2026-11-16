"""The orchestration engine.

One run moves through: analyse -> plan -> (execute step -> verify outputs)* ->
verify run -> report. The engine owns control flow; agents own the work.

Durability: `RunState` is written to the database after every transition, so a
run can stop for any reason (human approval, pause, process exit) and continue
later from its last checkpoint. Side effects are not repeated on re-execution
because the tool gateway replays completed calls from the step record.

Control signals (pause, cancel) are polled between steps, never mid-tool-call,
so a signal cannot leave a tool half-applied.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..agents import AgentContext, AgentRegistry, PlannerAgent, build_report, verify_run
from ..core.config import Settings
from ..core.errors import BizOpsError, BudgetExceeded
from ..core.models import (
    TERMINAL_RUN_STATES,
    AgentResult,
    PlanStep,
    RunState,
    RunStatus,
    StepStatus,
    Task,
)
from ..memory import MemoryStore
from ..observability import Tracer, get_logger
from ..storage import Database
from ..tools import ApprovalRequired
from ..workflows import WorkflowRegistry

log = get_logger("orchestrator")
MAX_STEP_ATTEMPTS = 2
_MISSING = object()


def resolve_refs(value: Any, state: RunState) -> Any:
    """Replace "$task.inputs.x" and "$steps.<action>.<key>" references with real values.

    Plans refer to earlier outputs by reference so that a plan can be written
    before those outputs exist. An unresolvable reference becomes None.
    """
    if isinstance(value, dict):
        return {k: resolve_refs(v, state) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_refs(v, state) for v in value]
    if not (isinstance(value, str) and value.startswith("$")):
        return value
    parts = value[1:].split(".")
    if parts[:2] == ["task", "inputs"]:
        current: Any = state.task.inputs
        parts = parts[2:]
    elif parts[0] == "steps":
        current = state.working_memory
        parts = parts[1:]
    else:
        return value
    for part in parts:
        current = current.get(part, _MISSING) if isinstance(current, dict) else _MISSING
        if current is _MISSING:
            return None
    return current


class Orchestrator:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        tracer: Tracer,
        memory: MemoryStore,
        registry: AgentRegistry,
        workflows: WorkflowRegistry,
        planner: PlannerAgent,
        agent_ctx: AgentContext,
    ):
        self.settings, self.db, self.tracer, self.memory = settings, db, tracer, memory
        self.registry, self.workflows, self.planner, self.ctx = registry, workflows, planner, agent_ctx

    # Public API --------------------------------------------------------------
    async def submit(self, task: Task) -> RunState:
        """Start a run and drive it until it completes or suspends."""
        if task.idempotency_key:
            existing = self.db.find_run_by_idempotency_key(task.tenant_id, task.idempotency_key)
            if existing is not None:
                self.tracer.event(
                    task.tenant_id, existing.run_id, "run.duplicate_ignored", idempotency_key=task.idempotency_key
                )
                return existing
        state = RunState(tenant_id=task.tenant_id, task=task)
        self.db.save_run(state)
        self.tracer.event(
            state.tenant_id,
            state.run_id,
            "run.created",
            objective=task.objective,
            source=task.source,
            requested_by=task.requested_by,
        )
        return await self._drive(state)

    async def resume(self, tenant_id: str, run_id: str) -> RunState:
        """Continue a suspended or interrupted run from its last checkpoint."""
        state = self.db.load_run(tenant_id, run_id)
        if state.status in TERMINAL_RUN_STATES:
            return state
        self.db.set_control(tenant_id, run_id, "")
        state.pending_approval_id = None
        if state.plan:
            for step in state.plan.steps:
                # A step caught mid-flight is re-run; completed tool calls inside it are replayed, not repeated.
                if step.status in (StepStatus.RUNNING, StepStatus.WAITING_APPROVAL):
                    step.status = StepStatus.PENDING
        self.tracer.event(tenant_id, run_id, "run.resumed", from_status=state.status.value)
        return await self._drive(state)

    def request_pause(self, tenant_id: str, run_id: str) -> None:
        self.db.set_control(tenant_id, run_id, "pause")
        self.tracer.event(tenant_id, run_id, "run.pause_requested")

    def cancel(self, tenant_id: str, run_id: str) -> RunState:
        """Cancel a run. A suspended run is cancelled at once; a running one at its next step boundary."""
        state = self.db.load_run(tenant_id, run_id)
        if state.status in TERMINAL_RUN_STATES:
            return state
        self.db.set_control(tenant_id, run_id, "cancel")
        if state.status != RunStatus.RUNNING:
            state.status = RunStatus.CANCELLED
            self._checkpoint(state, "run.cancelled")
        return state

    # Internals ---------------------------------------------------------------
    def _checkpoint(self, state: RunState, event: str | None = None, **payload: Any) -> None:
        self.db.save_run(state)
        if event:
            self.tracer.event(state.tenant_id, state.run_id, event, status=state.status.value, **payload)

    def _fail(self, state: RunState, error: str) -> RunState:
        state.status, state.error = RunStatus.FAILED, error
        self._checkpoint(state, "run.failed", error=error)
        return state

    async def _drive(self, state: RunState) -> RunState:
        state.status = RunStatus.RUNNING
        self._checkpoint(state, "run.started")
        try:
            with self.tracer.span(state.tenant_id, state.run_id, "run.drive") as span:
                result = await self._loop(state)
                span.update(final_status=result.status.value)
                return result
        except BudgetExceeded as exc:
            return self._fail(state, f"budget exceeded: {exc}")
        except BizOpsError as exc:
            return self._fail(state, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # last line of defence: record the failure, never lose the run
            log.exception("unexpected error in run %s", state.run_id)
            return self._fail(state, f"unexpected {type(exc).__name__}: {exc}")

    async def _loop(self, state: RunState) -> RunState:
        if not state.analysis:
            state.analysis = await self.planner.analyze(state, self.ctx)
            self._checkpoint(state, "run.analyzed", workflow=state.analysis.get("workflow"))
        if state.plan is None:
            state.plan = await self.planner.plan(state, self.ctx)
            self._checkpoint(
                state,
                "plan.created",
                version=state.plan.version,
                rationale=state.plan.rationale,
                steps=[f"{s.agent}.{s.action}" for s in state.plan.steps],
            )

        while True:
            signal = self.db.get_control(state.tenant_id, state.run_id)
            if signal == "cancel":
                state.status = RunStatus.CANCELLED
                self._checkpoint(state, "run.cancelled")
                return state
            if signal == "pause":
                self.db.set_control(state.tenant_id, state.run_id, "")
                state.status = RunStatus.PAUSED
                self._checkpoint(state, "run.paused")
                return state

            step = state.plan.next_runnable()
            if step is None:
                break
            if state.steps_executed >= self.settings.max_steps:
                raise BudgetExceeded(f"step limit {self.settings.max_steps} reached")

            try:
                result = await self._execute_step(state, step)
            except ApprovalRequired as request:
                step.status = StepStatus.WAITING_APPROVAL
                state.status = RunStatus.WAITING_APPROVAL
                state.pending_approval_id = request.verdict.approval_id
                self._checkpoint(
                    state,
                    "run.waiting_approval",
                    step=step.action,
                    tool=request.tool,
                    risk=request.risk.value,
                    reason=request.verdict.reason,
                    approval_id=request.verdict.approval_id,
                )
                return state

            if result.ok:
                continue
            if result.retryable and step.attempts < MAX_STEP_ATTEMPTS:
                step.status = StepStatus.PENDING
                self._checkpoint(state, "step.retry_scheduled", step=step.action, attempt=step.attempts)
                continue
            if not await self._replan(state, step):
                return self._fail(state, f"step '{step.action}' failed and no recovery plan exists: {step.error}")

        return self._finish(state)

    async def _execute_step(self, state: RunState, step: PlanStep) -> AgentResult:
        agent = self.registry.get(step.agent)
        step.status, step.attempts = StepStatus.RUNNING, step.attempts + 1
        step.started_at = step.started_at or datetime.now(UTC)
        state.steps_executed += 1
        inputs = resolve_refs(step.inputs, state)
        self._checkpoint(state, "step.started", step=step.action, agent=step.agent, attempt=step.attempts)

        with self.tracer.span(
            state.tenant_id, state.run_id, "step.execute", step=step.action, agent=step.agent, step_id=step.step_id
        ) as span:
            if agent is None:
                result = AgentResult(ok=False, summary=f"no agent named '{step.agent}'", error="unknown_agent")
            else:
                result = await agent.run(step, state, inputs, self.ctx)
                # One handoff is allowed per attempt: the first agent may pass the step to a better-suited one.
                target = self.registry.get(result.handoff_to) if result.handoff_to else None
                if target is not None and target is not agent and step.action in target.spec.actions:
                    self.tracer.event(
                        state.tenant_id,
                        state.run_id,
                        "step.handoff",
                        step=step.action,
                        from_agent=step.agent,
                        to_agent=target.spec.name,
                        reason=result.handoff_reason,
                    )
                    step.agent = target.spec.name
                    result = await target.run(step, state, inputs, self.ctx)
            span.update(ok=result.ok, summary=result.summary)

        step.finished_at = datetime.now(UTC)
        if result.ok:
            step.status, step.error = StepStatus.SUCCEEDED, None
            step.result = {**result.data, "_summary": result.summary}
            state.working_memory[step.action] = result.data  # short-term memory for later steps
            for fact in result.learned:
                self.memory.add_fact(state.tenant_id, fact, run_id=state.run_id, source=step.agent)
        else:
            step.status, step.error = StepStatus.FAILED, result.error or result.summary
        self._checkpoint(
            state, "step.finished", step=step.action, ok=result.ok, summary=result.summary, error=step.error
        )
        return result

    async def _replan(self, state: RunState, failed: PlanStep) -> bool:
        """Ask the planner for a way around a failed step. Returns False when the run cannot continue."""
        if state.replans >= self.settings.max_replans:
            return False
        new_plan = await self.planner.replan(state, failed, self.ctx)
        if new_plan is None:
            return False
        assert state.plan is not None
        state.replans += 1
        superseded = [s.action for s in state.plan.steps if s.status != StepStatus.SUCCEEDED]
        state.working_memory.setdefault("_failures", []).append({"action": failed.action, "error": failed.error})
        state.plan = new_plan
        self._checkpoint(
            state,
            "plan.replanned",
            version=new_plan.version,
            superseded=superseded,
            failed_step=failed.action,
            steps=[f"{s.agent}.{s.action}" for s in new_plan.steps],
        )
        return True

    def _finish(self, state: RunState) -> RunState:
        verification = verify_run(state, self.workflows)
        state.final_report = build_report(state, verification)
        state.status = RunStatus.COMPLETED
        # Episodic memory: a compact record of what happened, retrievable by later runs.
        outcome = "; ".join(f"{s['action']}={s['status']}" for s in state.final_report["steps"])
        self.memory.add_episode(
            state.tenant_id,
            state.run_id,
            f"Run for '{state.task.objective}' ({state.analysis.get('workflow')}) finished "
            f"{state.final_report['outcome']} after {state.replans} replans. Steps: {outcome}.",
            workflow=state.analysis.get("workflow"),
            outcome=state.final_report["outcome"],
        )
        self._checkpoint(
            state,
            "run.completed",
            outcome=state.final_report["outcome"],
            verification_passed=verification["passed"],
            cost_usd=state.usage.cost_usd,
        )
        return state
