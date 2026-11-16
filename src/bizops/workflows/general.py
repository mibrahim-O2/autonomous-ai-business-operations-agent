"""Fallback workflow for objectives no specific playbook matches: recall what is known and report it."""

from __future__ import annotations

from typing import Any

from ..core.models import Task
from .base import Workflow


def _plan(task: Task, _analysis: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "agent": "research",
            "action": "recall_context",
            "description": "Search memory for anything relevant to the objective.",
            "inputs": {"query": task.objective},
        }
    ]


def general_workflow() -> Workflow:
    # recall_context's script is provided by the lead_intake workflow; scripts are keyed by action.
    return Workflow(
        name="general",
        description="Answer a general request from stored knowledge.",
        keywords=[],
        plan=_plan,
        expects={"recall_context": ["known"]},
    )
