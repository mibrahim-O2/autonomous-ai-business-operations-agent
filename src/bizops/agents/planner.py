"""Planner: task analysis, plan generation, validation, and replanning.

The planner asks the model for a structured plan, validates it against the
agent registry, gives the model one chance to repair an invalid plan, and falls
back to the workflow library's template if the model still fails. A plan is
therefore always executable before the orchestrator sees it.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from ..core.errors import PlanningError
from ..core.models import Plan, PlanStep, RunState, StepStatus, Task
from ..llm.base import LLMMessage
from ..workflows import WorkflowRegistry
from .base import AgentContext, parse_final
from .registry import AgentRegistry

PLANNER_PROMPT_VERSION = "1"
PLANNER_PROMPT = (
    "You are the planning agent of a business operations system. Break the objective into the fewest steps "
    "that achieve it. Each step names one specialist agent and one action that agent supports. Use "
    "depends_on to reference earlier steps by their action name. Reply with JSON only: "
    '{"rationale": str, "steps": [{"agent": str, "action": str, "description": str, "inputs": object, '
    '"depends_on": [str]}]}.'
)


class _StepDraft(BaseModel):
    agent: str
    action: str
    description: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)


class _PlanDraft(BaseModel):
    rationale: str = ""
    steps: list[_StepDraft] = Field(min_length=1)


class PlannerAgent:
    name = "planner"

    def __init__(self, registry: AgentRegistry, workflows: WorkflowRegistry):
        self.registry, self.workflows = registry, workflows

    async def _ask(self, state: RunState, ctx: AgentContext, envelope: dict[str, Any], extra: list[LLMMessage]) -> str:
        messages = [
            LLMMessage(role="system", content=PLANNER_PROMPT),
            LLMMessage(role="user", content=json.dumps(envelope, default=str)),
            *extra,
        ]
        with ctx.tracer.span(
            state.tenant_id,
            state.run_id,
            "llm.call",
            agent=self.name,
            action=envelope["kind"],
            prompt_version=PLANNER_PROMPT_VERSION,
        ) as span:
            response = await ctx.llm.complete(messages, [])
            span.update(
                model=response.model,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
        state.usage.add(response.usage)
        return response.text

    async def analyze(self, state: RunState, ctx: AgentContext) -> dict[str, Any]:
        """Classify the task: which workflow it matches, which entities it names, whether knowledge is needed."""
        task = state.task
        envelope = {
            "kind": "analyze",
            "objective": task.objective,
            "workflow_hint": task.workflow,
            "inputs": task.inputs,
            "workflows": self.workflows.describe(),
        }
        analysis = parse_final(await self._ask(state, ctx, envelope, []))
        if analysis.get("workflow") not in self.workflows.names():
            # The model named no usable workflow; fall back to deterministic detection.
            analysis["workflow"] = self.workflows.detect(task).name
        return analysis

    def _to_plan(self, draft: _PlanDraft, version: int, max_steps: int) -> Plan:
        """Validate a draft against the registry and convert action references to step ids."""
        if len(draft.steps) > max_steps:
            raise PlanningError(f"plan has {len(draft.steps)} steps; limit is {max_steps}")
        steps: list[PlanStep] = []
        by_action: dict[str, str] = {}
        for index, item in enumerate(draft.steps):
            if not self.registry.supports(item.agent, item.action):
                raise PlanningError(f"step {index}: agent '{item.agent}' does not support action '{item.action}'")
            if item.action in by_action:
                raise PlanningError(f"step {index}: action '{item.action}' appears twice")
            unknown = [d for d in item.depends_on if d not in by_action]
            if unknown:
                raise PlanningError(f"step {index}: depends on unknown or later steps {unknown}")
            step = PlanStep(
                index=index,
                agent=item.agent,
                action=item.action,
                description=item.description or item.action,
                inputs=item.inputs,
                depends_on=[by_action[d] for d in item.depends_on],
            )
            by_action[item.action] = step.step_id
            steps.append(step)
        return Plan(version=version, rationale=draft.rationale, steps=steps)

    async def plan(self, state: RunState, ctx: AgentContext) -> Plan:
        task: Task = state.task
        workflow = self.workflows.get(state.analysis.get("workflow", "")) or self.workflows.detect(task)
        envelope = {
            "kind": "plan",
            "objective": task.objective,
            "workflow": workflow.name,
            "inputs": task.inputs,
            "analysis": state.analysis,
            "agents": self.registry.describe(),
            "playbook": workflow.description,
        }
        extra: list[LLMMessage] = []
        for attempt in range(2):
            text = await self._ask(state, ctx, envelope, extra)
            try:
                return self._to_plan(_PlanDraft.model_validate(parse_final(text)), 1, ctx.settings.max_steps)
            except (ValidationError, PlanningError) as exc:
                ctx.tracer.event(state.tenant_id, state.run_id, "plan.invalid", attempt=attempt + 1, error=str(exc))
                extra = [
                    LLMMessage(role="assistant", content=text),
                    LLMMessage(role="user", content=f"That plan is invalid: {exc}. Reply with corrected JSON only."),
                ]
        # Last resort: the workflow's own template, which is known to be valid.
        ctx.tracer.event(state.tenant_id, state.run_id, "plan.fallback", workflow=workflow.name)
        draft = _PlanDraft(rationale=f"template for {workflow.name}", steps=workflow.plan(task, state.analysis))
        return self._to_plan(draft, 1, ctx.settings.max_steps)

    async def replan(self, state: RunState, failed: PlanStep, ctx: AgentContext) -> Plan | None:
        """Build a new plan around a failed or rejected step. Returns None if no recovery exists."""
        assert state.plan is not None
        workflow = self.workflows.get(state.analysis.get("workflow", "")) or self.workflows.detect(state.task)
        envelope = {
            "kind": "replan",
            "objective": state.task.objective,
            "workflow": workflow.name,
            "inputs": state.task.inputs,
            "failed_action": failed.action,
            "failure": failed.error,
            "failed_status": failed.status.value,
            "completed": [s.action for s in state.plan.steps if s.status == StepStatus.SUCCEEDED],
            "agents": self.registry.describe(),
        }
        parsed = parse_final(await self._ask(state, ctx, envelope, []))
        if not parsed.get("steps"):
            return None
        completed = [s for s in state.plan.steps if s.status == StepStatus.SUCCEEDED]
        try:
            draft = _PlanDraft.model_validate(parsed)
            # Recovery steps may depend on work that already succeeded, so validate them together.
            done_drafts = [
                _StepDraft(agent=s.agent, action=s.action, description=s.description, inputs=s.inputs)
                for s in completed
            ]
            merged = self._to_plan(
                _PlanDraft(rationale=draft.rationale, steps=done_drafts + draft.steps),
                state.plan.version + 1,
                ctx.settings.max_steps,
            )
        except (ValidationError, PlanningError) as exc:
            ctx.tracer.event(state.tenant_id, state.run_id, "replan.invalid", error=str(exc))
            return None
        # Carry over finished work unchanged; superseded steps stay visible in the trace, not the plan.
        id_map = {}
        for new_step, old_step in zip(merged.steps, completed, strict=False):
            id_map[new_step.step_id] = old_step.step_id
            merged.steps[new_step.index] = old_step.model_copy(update={"index": new_step.index})
        for step in merged.steps[len(completed) :]:
            step.depends_on = [id_map.get(d, d) for d in step.depends_on]
        return merged
