"""Workflow library: playbooks the planner can use, plus scripted behaviour for offline runs."""

from .base import Workflow, WorkflowRegistry
from .collections import collections_workflow
from .general import general_workflow
from .inbox import inbox_workflow
from .lead_intake import lead_intake_workflow
from .refund import refund_workflow


def default_workflows() -> WorkflowRegistry:
    registry = WorkflowRegistry()
    registry.register(lead_intake_workflow())
    registry.register(collections_workflow())
    registry.register(inbox_workflow())
    registry.register(refund_workflow())
    registry.register(general_workflow())  # registered last: it is the fallback
    return registry


__all__ = ["Workflow", "WorkflowRegistry", "default_workflows"]
