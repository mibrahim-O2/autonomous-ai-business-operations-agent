"""Structured inputs, outputs, and state.

Everything that crosses a boundary (task intake, plans, tool calls, results,
persisted run state) is a pydantic model, so malformed data is rejected at the
edge instead of failing deep inside a workflow.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(UTC)


class RiskLevel(StrEnum):
    """How much harm a tool call can do. The approval policy keys off this."""

    READ = "read"  # no side effects
    LOW = "low"  # internal, reversible write
    MEDIUM = "medium"  # internal write that is awkward to undo
    HIGH = "high"  # external, financial, or irreversible

    @property
    def rank(self) -> int:
        return ["read", "low", "medium", "high"].index(self.value)


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    REJECTED = "rejected"


TERMINAL_RUN_STATES = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}


class Task(BaseModel):
    """A unit of work submitted by a person, a webhook, or a schedule."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(default_factory=lambda: new_id("task"))
    tenant_id: str = "demo"
    objective: str = Field(min_length=3, max_length=4000)
    workflow: str | None = None  # optional hint naming a registered workflow
    inputs: dict[str, Any] = Field(default_factory=dict)
    source: str = "cli"  # cli | api | webhook | schedule
    requested_by: str = "system"
    idempotency_key: str | None = None


class ToolCall(BaseModel):
    """A request from an agent to run one tool with validated arguments."""

    call_id: str = Field(default_factory=lambda: new_id("call"))
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    call_id: str
    tool: str
    ok: bool
    output: Any = None
    error: str | None = None
    retryable: bool = False
    duration_ms: float = 0.0
    # True when the output contains text written by an outside party (email body,
    # web page, document). Downstream code treats such text as data, never as instructions.
    untrusted: bool = False
    # Hash of tool name and arguments. Used to replay a completed call instead of
    # repeating its side effect when a step is re-executed after a pause.
    fingerprint: str = ""
    replayed: bool = False


class PlanStep(BaseModel):
    """One step of a plan: which specialist does what, and what it depends on."""

    step_id: str = Field(default_factory=lambda: new_id("step"))
    index: int = 0
    agent: str
    action: str  # short verb phrase the agent understands, for example "lookup_contact"
    description: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    result: dict[str, Any] | None = None
    error: str | None = None
    tool_calls: list[ToolResult] = Field(default_factory=list)
    # Write-ahead intent: the side-effecting call this step is about to make, saved before it is
    # sent and cleared once its result is recorded. Present after a crash means "outcome unknown".
    in_flight: ToolCall | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class Plan(BaseModel):
    plan_id: str = Field(default_factory=lambda: new_id("plan"))
    version: int = 1
    rationale: str = ""
    steps: list[PlanStep] = Field(default_factory=list)

    def step(self, step_id: str) -> PlanStep:
        for step in self.steps:
            if step.step_id == step_id:
                return step
        raise KeyError(step_id)

    def next_runnable(self) -> PlanStep | None:
        """First pending step whose dependencies have all succeeded."""
        done = {s.step_id for s in self.steps if s.status == StepStatus.SUCCEEDED}
        for step in self.steps:
            if step.status == StepStatus.PENDING and set(step.depends_on) <= done:
                return step
        return None


class Usage(BaseModel):
    """Token and cost accounting for one model call or a whole run."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cost_usd = round(self.cost_usd + other.cost_usd, 6)
        self.calls += other.calls


class RunState(BaseModel):
    """Everything needed to continue a run after a pause or a process restart.

    The orchestrator saves this after every state transition (a checkpoint), so
    the database row is always a consistent point to resume from.
    """

    run_id: str = Field(default_factory=lambda: new_id("run"))
    tenant_id: str
    task: Task
    status: RunStatus = RunStatus.PENDING
    plan: Plan | None = None
    analysis: dict[str, Any] = Field(default_factory=dict)
    working_memory: dict[str, Any] = Field(default_factory=dict)
    replans: int = 0
    steps_executed: int = 0
    usage: Usage = Field(default_factory=Usage)
    # Set once any untrusted content has entered the run; later tasks tighten policy on it.
    tainted: bool = False
    pending_approval_id: str | None = None
    # Suspected prompt-injection attempts seen in untrusted content during this run.
    injection_findings: list[dict[str, Any]] = Field(default_factory=list)
    # Email addresses and links that entered the run only through untrusted content. A side-effecting
    # call that uses one of them is pointed out to the approver.
    untrusted_refs: list[str] = Field(default_factory=list)
    # Fencing token: the claim generation under which this copy of the state is being executed.
    # It increases every time a worker claims the run and is checked on every write.
    generation: int = 0
    final_report: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class AgentResult(BaseModel):
    """What a specialist returns to the orchestrator after working on a step."""

    ok: bool
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    tool_results: list[ToolResult] = Field(default_factory=list)
    # Facts worth remembering beyond this run (written to semantic memory).
    learned: list[str] = Field(default_factory=list)
    error: str | None = None
    retryable: bool = False
    # A specialist may hand a step to another specialist instead of doing it.
    handoff_to: str | None = None
    handoff_reason: str | None = None
