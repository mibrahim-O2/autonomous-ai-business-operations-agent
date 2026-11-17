"""The orchestration engine.

One run moves through: analyse -> plan -> (execute step -> verify outputs)* ->
verify run -> report. The engine owns control flow; agents own the work.

Durability: `RunState` is written to the database after every transition and
after every tool call, so a run can stop for any reason (human approval, pause,
process exit) and continue later from its last checkpoint.

Single writer: a worker must claim a run before executing it. The claim is an
atomic conditional update that also increments the run's generation, which is
the fencing token. Every checkpoint is conditional on (owner, generation), the
gateway re-checks ownership before each tool call, and target systems refuse a
token lower than one they have seen. A worker that stalls past its lease and
wakes up after another worker took over can therefore neither commit state nor
perform a side effect: it receives `StaleLease` and stops.

Side effects are not repeated on re-execution: the gateway replays completed
calls from the step record, and each side-effecting call carries an
idempotency key that the target system honours.

Control signals (pause, cancel) are polled between steps, never mid-tool-call,
so a signal cannot leave a tool half-applied.
"""

from __future__ import annotations

import asyncio
import random
import sqlite3
from datetime import UTC, datetime
from typing import Any

from ..agents import AgentContext, AgentRegistry, PlannerAgent, build_report, verify_run
from ..core.config import Settings
from ..core.errors import BizOpsError, BudgetExceeded, PlanningError, StaleLease
from ..core.models import (
    TERMINAL_RUN_STATES,
    AgentResult,
    PlanStep,
    RunState,
    RunStatus,
    StepStatus,
    Task,
    new_id,
)
from ..memory import MemoryStore
from ..observability import Tracer, get_logger
from ..storage import Database
from ..tools import ApprovalRequired
from ..workflows import WorkflowRegistry
from ..workflows.guides import GUIDES, required_output_keys

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
        # Identity of this orchestrator instance in lease records.
        self.worker_id = new_id("worker")
        self.heartbeat_enabled = True
        # Used for retry backoff only; replaceable in tests without touching the event loop's own sleep.
        self.backoff_sleep = asyncio.sleep
        # Agents persist each tool record through the fenced checkpoint, and the gateway
        # verifies ownership immediately before every tool call.
        agent_ctx.checkpoint = self._checkpoint
        agent_ctx.gateway.fence_guard = self._assert_current
        agent_ctx.gateway.checkpoint = self._checkpoint
        agent_ctx.step_guide = self._step_guide
        # Set by the runtime when human approval is configured (ApprovalService).
        self.approvals: Any = None

    # Public API --------------------------------------------------------------
    def create(self, task: Task) -> tuple[RunState, bool]:
        """Register a run without executing it. Returns (state, created).

        Separating creation from execution lets an API respond with a run id at
        once and drive the run in the background.
        """
        if task.idempotency_key:
            existing = self.db.find_run_by_idempotency_key(task.tenant_id, task.idempotency_key)
            if existing is not None:
                self.tracer.event(
                    task.tenant_id, existing.run_id, "run.duplicate_ignored", idempotency_key=task.idempotency_key
                )
                return existing, False
        state = RunState(tenant_id=task.tenant_id, task=task)
        try:
            self.db.save_run(state)
        except sqlite3.IntegrityError:
            # Another submission with the same idempotency key won the race between the lookup
            # above and this insert. The unique index is the real guarantee; return the winner.
            winner = self.db.find_run_by_idempotency_key(task.tenant_id, task.idempotency_key or "")
            if winner is None:
                raise
            return winner, False
        self.tracer.event(
            state.tenant_id,
            state.run_id,
            "run.created",
            objective=task.objective,
            source=task.source,
            requested_by=task.requested_by,
        )
        return state, True

    async def submit(self, task: Task) -> RunState:
        """Start a run and drive it until it completes or suspends."""
        state, created = self.create(task)
        return await self.drive(state.tenant_id, state.run_id) if created else state

    async def drive(self, tenant_id: str, run_id: str) -> RunState:
        """Claim a created run and execute it until it completes or suspends."""
        return await self._drive(tenant_id, run_id)

    async def resume(self, tenant_id: str, run_id: str) -> RunState:
        """Continue a suspended or interrupted run from its last checkpoint.

        Safe to call from several workers at once: only the one that wins the
        claim executes; the others return the current state without acting.
        """
        state = self.db.load_run(tenant_id, run_id)
        if state.status in TERMINAL_RUN_STATES:
            return state
        if (
            state.status == RunStatus.WAITING_APPROVAL
            and self.approvals is not None
            and self.approvals.is_pending(tenant_id, state.pending_approval_id)
        ):
            # Nothing can proceed until a person decides; do not burn model calls re-running the step.
            self.tracer.event(tenant_id, run_id, "run.resume_blocked", approval_id=state.pending_approval_id)
            return state
        return await self._drive(tenant_id, run_id, resumed=True)

    async def recover_interrupted(self) -> list[str]:
        """Resume runs whose worker died or stalled (marked running, lease gone). Returns the run ids taken up.

        Safe to call from every worker at any time: the claim decides who recovers a run, and a run
        whose worker is merely slow still holds its lease and is not listed.
        """
        recovered = []
        for tenant_id, run_id in self.db.list_interrupted_runs():
            self.tracer.event(tenant_id, run_id, "run.recovery_started", worker=self.worker_id)
            await self.resume(tenant_id, run_id)
            recovered.append(run_id)
        return recovered

    def request_pause(self, tenant_id: str, run_id: str) -> None:
        self.db.set_control(tenant_id, run_id, "pause")
        self.tracer.event(tenant_id, run_id, "run.pause_requested")

    def cancel(self, tenant_id: str, run_id: str) -> RunState:
        """Cancel a run. A suspended run is cancelled at once; a running one at its next step boundary."""
        state = self.db.load_run(tenant_id, run_id)
        if state.status in TERMINAL_RUN_STATES:
            return state
        self.db.set_control(tenant_id, run_id, "cancel")
        generation = self.db.claim_run(tenant_id, run_id, self.worker_id, self.settings.run_lease_seconds)
        if generation is None:
            return state  # a worker is executing it; that worker sees the signal at its next step boundary
        state = self.db.load_run(tenant_id, run_id)
        state.generation = generation
        try:
            if state.status not in TERMINAL_RUN_STATES:
                self._cancel(state)
        finally:
            self.db.release_lease(tenant_id, run_id, self.worker_id, generation)
        return state

    # Internals ---------------------------------------------------------------
    def _assert_current(self, state: RunState) -> None:
        """Raise StaleLease unless this worker still owns the run under the state's generation."""
        if state.generation and not self.db.lease_is_current(
            state.tenant_id, state.run_id, self.worker_id, state.generation
        ):
            raise StaleLease(
                f"worker {self.worker_id} (generation {state.generation}) no longer owns run {state.run_id}"
            )

    def _checkpoint(self, state: RunState, event: str | None = None, **payload: Any) -> None:
        """Fenced write of the run state. Raises StaleLease, writing nothing, if the claim was lost."""
        self.db.save_run(state, fence=(self.worker_id, state.generation), lease_seconds=self.settings.run_lease_seconds)
        if event:
            self.tracer.event(state.tenant_id, state.run_id, event, status=state.status.value, **payload)

    def _cancel(self, state: RunState) -> RunState:
        state.status = RunStatus.CANCELLED
        self._checkpoint(state, "run.cancelled")
        if self.approvals is not None:
            # Nobody should be asked to approve an action for a run that will never perform it.
            self.approvals.cancel_for_run(state.tenant_id, state.run_id, "system")
        return state

    def _fail(self, state: RunState, error: str) -> RunState:
        state.status, state.error = RunStatus.FAILED, error
        self._checkpoint(state, "run.failed", error=error)
        return state

    async def _heartbeat(self, tenant_id: str, run_id: str, generation: int) -> None:
        """Renew the lease while a long step is in flight. Stops as soon as the lease is lost."""
        interval = max(0.02, self.settings.run_lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            if not self.db.renew_lease(tenant_id, run_id, self.worker_id, generation, self.settings.run_lease_seconds):
                return

    async def _drive(self, tenant_id: str, run_id: str, resumed: bool = False) -> RunState:
        # 1. Claim. The claim is atomic; a worker that does not win it does nothing.
        generation = self.db.claim_run(tenant_id, run_id, self.worker_id, self.settings.run_lease_seconds)
        if generation is None:
            self.tracer.event(tenant_id, run_id, "run.claim_rejected", worker=self.worker_id)
            return self.db.load_run(tenant_id, run_id)

        heartbeat: asyncio.Task[None] | None = None
        try:
            # 2. Load only after the claim, so the state is the latest committed checkpoint.
            state = self.db.load_run(tenant_id, run_id)
            if state.status in TERMINAL_RUN_STATES:
                return state
            state.generation = generation
            state.pending_approval_id = None
            if resumed:
                self.db.set_control(tenant_id, run_id, "")
            for step in state.plan.steps if state.plan else []:
                # 3. Recover in-flight work. A step caught mid-flight is run again from its start.
                # Tool calls it already completed are replayed from the record or recognised by the
                # target system through their idempotency key, so their effects are not repeated.
                if step.status in (StepStatus.RUNNING, StepStatus.WAITING_APPROVAL):
                    step.status = StepStatus.PENDING
            if self.heartbeat_enabled:
                heartbeat = asyncio.create_task(self._heartbeat(tenant_id, run_id, generation))
            state.status = RunStatus.RUNNING
            self._checkpoint(
                state, "run.resumed" if resumed else "run.started", generation=generation, worker=self.worker_id
            )
            try:
                with self.tracer.span(tenant_id, run_id, "run.drive", generation=generation) as span:
                    result = await self._loop(state)
                    span.update(final_status=result.status.value)
                    return result
            except StaleLease:
                raise
            except BudgetExceeded as exc:
                return self._fail(state, f"budget exceeded: {exc}")
            except BizOpsError as exc:
                return self._fail(state, f"{type(exc).__name__}: {exc}")
            except Exception as exc:  # last line of defence: record the failure, never lose the run
                log.exception("unexpected error in run %s", run_id)
                return self._fail(state, f"unexpected {type(exc).__name__}: {exc}")
        except StaleLease as exc:
            # 4. Superseded. Another worker owns the run under a newer generation. This worker has
            # written nothing since losing the claim and must not start now; report what is true.
            self.tracer.event(
                tenant_id,
                run_id,
                "run.stale_worker_stopped",
                worker=self.worker_id,
                generation=generation,
                reason=str(exc),
            )
            return self.db.load_run(tenant_id, run_id)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
            # Conditional on owner and generation, so a stale worker releases nothing.
            self.db.release_lease(tenant_id, run_id, self.worker_id, generation)

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

        disabled = sorted(
            {s.agent for s in state.plan.steps if not self.registry.is_enabled(self.db, state.tenant_id, s.agent)}
        )
        if disabled and any(s.status == StepStatus.PENDING and s.agent in disabled for s in state.plan.steps):
            raise PlanningError(f"the plan needs agent(s) that an operator has disabled: {disabled}")

        while True:
            signal = self.db.get_control(state.tenant_id, state.run_id)
            if signal == "cancel":
                return self._cancel(state)
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
                # Waiting for a person is not a failed attempt; it must not use up the step's retries.
                step.attempts = max(0, step.attempts - 1)
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
                # Exponential backoff with full jitter: transient faults usually clear, and retrying
                # at once (or in step with other workers) makes an overloaded dependency worse.
                ceiling = min(
                    self.settings.retry_max_seconds, self.settings.retry_base_seconds * 2 ** (step.attempts - 1)
                )
                delay = random.uniform(ceiling / 2, ceiling) if ceiling > 0 else 0.0
                step.status = StepStatus.PENDING
                self._checkpoint(
                    state, "step.retry_scheduled", step=step.action, attempt=step.attempts, delay_s=round(delay, 3)
                )
                if delay:
                    await self.backoff_sleep(delay)
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
                result = await self._run_agent(agent, step, state, inputs)
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
                    result = await self._run_agent(target, step, state, inputs)
            span.update(ok=result.ok, summary=result.summary)

        step.finished_at = datetime.now(UTC)
        will_retry = not result.ok and result.retryable and step.attempts < MAX_STEP_ATTEMPTS
        if step.in_flight is not None and not will_retry:
            # A side-effecting call whose outcome is unknown (interrupted or timed out) and that will not
            # be re-sent. It may or may not have happened. Record it where a person will see it: the
            # trace, and the final report. While a retry is coming the intent is kept, so the retry
            # sends the same call under the same idempotency key and resolves the question.
            unresolved = {
                "tool": step.in_flight.tool,
                "arguments": step.in_flight.arguments,
                "step": step.action,
            }
            state.working_memory.setdefault("_unresolved_side_effects", []).append(unresolved)
            self.tracer.event(
                state.tenant_id, state.run_id, "tool.intent_unresolved", tool=step.in_flight.tool, step_id=step.step_id
            )
            step.in_flight = None
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

    def _step_guide(self, state: RunState, step: PlanStep) -> dict[str, Any]:
        """What a hosted model needs beyond the step itself: a method, and the output keys that matter."""
        workflow = self.workflows.get(state.analysis.get("workflow") or "")
        expects = workflow.expects.get(step.action, []) if workflow else []
        later_inputs = [s.inputs for s in state.plan.steps] if state.plan else []
        return {
            "guide": GUIDES.get(step.action, ""),
            "required_output_keys": required_output_keys(step.action, expects, later_inputs),
        }

    async def _run_agent(self, agent: Any, step: PlanStep, state: RunState, inputs: dict[str, Any]) -> AgentResult:
        """Run one specialist on one step under the step time limit."""
        try:
            return await asyncio.wait_for(agent.run(step, state, inputs, self.ctx), self.settings.step_timeout_seconds)
        except TimeoutError:
            self.tracer.event(
                state.tenant_id,
                state.run_id,
                "step.timed_out",
                step=step.action,
                limit_s=self.settings.step_timeout_seconds,
            )
            return AgentResult(
                ok=False,
                summary=f"step exceeded its {self.settings.step_timeout_seconds:g}s time limit",
                error="step_timeout",
                retryable=True,
            )

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
