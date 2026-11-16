"""Verification and reporting.

Both are deterministic. Verification checks that each completed step produced
the outputs its workflow declares; a run is not reported as successful merely
because no exception was raised. The report is the structured final answer.
"""

from __future__ import annotations

from typing import Any

from ..core.models import RunState, StepStatus
from ..workflows import WorkflowRegistry


def verify_run(state: RunState, workflows: WorkflowRegistry) -> dict[str, Any]:
    assert state.plan is not None
    workflow = workflows.get(state.analysis.get("workflow", ""))
    expects = workflow.expects if workflow else {}
    checks: list[dict[str, Any]] = []
    for step in state.plan.steps:
        if step.status != StepStatus.SUCCEEDED:
            checks.append(
                {
                    "step": step.action,
                    "check": "step_succeeded",
                    "passed": False,
                    "detail": f"status is {step.status.value}",
                }
            )
            continue
        data = step.result or {}
        missing = [key for key in expects.get(step.action, []) if key not in data]
        checks.append(
            {
                "step": step.action,
                "check": "expected_outputs",
                "passed": not missing,
                "detail": f"missing {missing}" if missing else "all expected outputs present",
            }
        )
        failed_tools = [t.tool for t in step.tool_calls if not t.ok]
        if failed_tools:
            checks.append(
                {
                    "step": step.action,
                    "check": "tool_errors_handled",
                    "passed": True,
                    "detail": f"recovered from failed calls: {failed_tools}",
                }
            )
    return {"passed": all(c["passed"] for c in checks) and bool(checks), "checks": checks}


def build_report(state: RunState, verification: dict[str, Any]) -> dict[str, Any]:
    assert state.plan is not None
    return {
        "run_id": state.run_id,
        "objective": state.task.objective,
        "workflow": state.analysis.get("workflow"),
        "outcome": "verified" if verification["passed"] else "completed_with_issues",
        "plan_version": state.plan.version,
        "replans": state.replans,
        "steps": [
            {
                "agent": s.agent,
                "action": s.action,
                "status": s.status.value,
                "summary": (s.result or {}).get("_summary", s.error or ""),
                "attempts": s.attempts,
                "tools": [t.tool for t in s.tool_calls],
            }
            for s in state.plan.steps
        ],
        "results": {
            s.action: {k: v for k, v in (s.result or {}).items() if not k.startswith("_")}
            for s in state.plan.steps
            if s.status == StepStatus.SUCCEEDED
        },
        "verification": verification,
        "tainted_by_untrusted_content": state.tainted,
        "usage": state.usage.model_dump(),
    }
