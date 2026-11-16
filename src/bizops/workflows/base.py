"""Workflow definitions.

A workflow is a playbook: when it applies, what a sound plan looks like, how to
recover when a step fails, and which outputs prove each step worked. Hosted
models receive the playbook as guidance; the offline scripted model executes it
directly through the handlers registered by `install`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..core.models import Task, ToolCall
from ..llm.scripted import Handler, ScriptedLLM

PlanBuilder = Callable[[Task, dict[str, Any]], list[dict[str, Any]]]
ReplanBuilder = Callable[[dict[str, Any]], list[dict[str, Any]]]


@dataclass
class Workflow:
    name: str
    description: str
    keywords: list[str]
    plan: PlanBuilder
    replan: ReplanBuilder | None = None
    # action name -> scripted handler used by the offline model
    scripts: dict[str, Handler] = field(default_factory=dict)
    # action name -> output keys the verifier requires
    expects: dict[str, list[str]] = field(default_factory=dict)
    version: str = "1"


class WorkflowRegistry:
    def __init__(self) -> None:
        self._workflows: dict[str, Workflow] = {}

    def register(self, workflow: Workflow) -> None:
        self._workflows[workflow.name] = workflow

    def get(self, name: str) -> Workflow | None:
        return self._workflows.get(name)

    def names(self) -> list[str]:
        return list(self._workflows)

    def describe(self) -> list[dict[str, str]]:
        return [{"name": w.name, "description": w.description, "version": w.version} for w in self._workflows.values()]

    def detect(self, task: Task) -> Workflow:
        """Pick a workflow: an explicit valid hint wins, then keyword overlap, then the fallback."""
        if task.workflow and task.workflow in self._workflows:
            return self._workflows[task.workflow]
        text = task.objective.lower()
        scored = [(sum(k in text for k in w.keywords), w) for w in self._workflows.values()]
        best_score, best = max(scored, key=lambda pair: pair[0])
        return best if best_score > 0 else list(self._workflows.values())[-1]

    def install(self, llm: ScriptedLLM) -> None:
        """Teach the offline model to analyse, plan, replan, and act using these workflows."""

        def analyze(envelope: dict[str, Any], _results: list[dict[str, Any]]) -> dict[str, Any]:
            task = Task(
                objective=envelope["objective"],
                workflow=envelope.get("workflow_hint"),
                inputs=envelope.get("inputs") or {},
            )
            workflow = self.detect(task)
            return {
                "workflow": workflow.name,
                "intent": workflow.description,
                "entities": {k: v for k, v in task.inputs.items() if isinstance(v, str | int | float)},
                "needs_retrieval": workflow.name != "general" or "?" in task.objective,
            }

        def plan(envelope: dict[str, Any], _results: list[dict[str, Any]]) -> dict[str, Any]:
            workflow = self._workflows[envelope["workflow"]]
            task = Task(objective=envelope["objective"], inputs=envelope.get("inputs") or {})
            return {
                "rationale": f"Follow the {workflow.name} playbook (version {workflow.version}).",
                "steps": workflow.plan(task, envelope.get("analysis") or {}),
            }

        def replan(envelope: dict[str, Any], _results: list[dict[str, Any]]) -> dict[str, Any]:
            workflow = self._workflows[envelope["workflow"]]
            steps = workflow.replan(envelope) if workflow.replan else []
            return {"rationale": f"Recover from failure of {envelope['failed_action']}.", "steps": steps}

        llm.register("analyze", analyze)
        llm.register("plan", plan)
        llm.register("replan", replan)
        for workflow in self._workflows.values():
            for action, handler in workflow.scripts.items():
                llm.register(f"act:{action}", handler)


def call(tool: str, **arguments: Any) -> ToolCall:
    """Shorthand used by scripted handlers."""
    return ToolCall(tool=tool, arguments=arguments)


def last_result(results: list[dict[str, Any]], tool: str) -> Any:
    """Most recent result for a tool within the current step, or None."""
    for entry in reversed(results):
        if entry["tool"] == tool:
            return entry["result"]
    return None
