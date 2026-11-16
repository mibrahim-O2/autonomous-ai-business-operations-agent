"""Invoice collections workflow.

    research_policy   retrieve the collections policy and extract its numbers
    review_overdue    list overdue invoices and decide what the policy says for each
    send_reminders    send the reminders the policy allows

The plan depends on retrieval twice over. The reviewed decisions use the numbers
extracted from the policy document, so changing the document changes behaviour
with no code change. And if no grounded policy is found, the planner replaces
the remaining steps with an escalation to a person.
"""

from __future__ import annotations

from typing import Any

from ..core.models import Task, ToolCall
from .base import Workflow, call, last_result
from .research import research_policy

POLICY_FACTS = {
    "grace_days": r"more than (\d+) days past",
    "max_reminders": r"at most (\d+) reminders",
    "approval_threshold": r"above (\d+(?:\.\d+)?) USD must be approved",
}


def _plan(_task: Task, _analysis: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "agent": "research",
            "action": "research_policy",
            "description": "Retrieve the collections policy: grace period, reminder limit, approval threshold.",
            "inputs": {
                "question": "collections policy payment reminder grace period reminder limit approval threshold",
                "doc_types": ["policy"],
                "fallback_query": "overdue invoice reminders",
                "extract": POLICY_FACTS,
            },
        },
        {
            "agent": "finance_ops",
            "action": "review_overdue",
            "description": "List overdue invoices and apply the retrieved policy to each.",
            "inputs": {"policy": "$steps.research_policy.facts"},
            "depends_on": ["research_policy"],
        },
        {
            "agent": "finance_ops",
            "action": "send_reminders",
            "description": "Send payment reminders for the invoices the policy allows.",
            "inputs": {"to_send": "$steps.review_overdue.to_send"},
            "depends_on": ["review_overdue"],
        },
    ]


def _replan(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    if envelope["failed_action"] == "escalate_policy_gap":
        return []
    return [
        {
            "agent": "finance_ops",
            "action": "escalate_policy_gap",
            "description": "No reminders can be sent safely; hand the collections review to a person.",
            "inputs": {"reason": f"{envelope['failed_action']} {envelope['failed_status']}: {envelope['failure']}"},
        }
    ]


def decide(invoice: dict[str, Any], policy: dict[str, Any]) -> tuple[str, str]:
    """Apply the policy to one invoice. Returns (decision, reason)."""
    if invoice["days_overdue"] <= policy["grace_days"]:
        return (
            "wait",
            f"{invoice['days_overdue']} days overdue is within the {int(policy['grace_days'])}-day grace period",
        )
    if invoice.get("reminders_sent", 0) >= policy["max_reminders"]:
        return "escalate", f"reminder limit of {int(policy['max_reminders'])} reached"
    return "remind", f"{invoice['days_overdue']} days overdue, {invoice.get('reminders_sent', 0)} reminders so far"


def _review(envelope: dict[str, Any], results: list[dict[str, Any]]) -> list[ToolCall] | dict[str, Any]:
    policy = envelope["inputs"].get("policy") or {}
    if not all(key in policy for key in POLICY_FACTS):
        return {"ok": False, "summary": "Policy values are missing; refusing to guess.", "error": "no_grounded_policy"}
    listing = last_result(results, "billing_list_invoices")
    if listing is None:
        return [call("billing_list_invoices", status="overdue")]
    if "error" in listing:
        return {
            "ok": False,
            "summary": "Could not list invoices.",
            "error": listing["error"],
            "retryable": bool(listing.get("retryable")),
        }
    decisions = []
    for invoice in listing["invoices"]:
        decision, reason = decide(invoice, policy)
        decisions.append(
            {
                "invoice_id": invoice["invoice_id"],
                "customer": invoice["customer"],
                "amount": invoice["amount"],
                "decision": decision,
                "reason": reason,
                "needs_approval": decision == "remind" and invoice["amount"] > policy["approval_threshold"],
            }
        )
    to_send = [d for d in decisions if d["decision"] == "remind"]
    return {
        "ok": True,
        "summary": f"Reviewed {len(decisions)} overdue invoices; {len(to_send)} reminders due.",
        "data": {
            "decisions": decisions,
            "to_send": to_send,
            "escalate": [d["invoice_id"] for d in decisions if d["decision"] == "escalate"],
        },
    }


def _send(envelope: dict[str, Any], results: list[dict[str, Any]]) -> list[ToolCall] | dict[str, Any]:
    to_send = envelope["inputs"].get("to_send") or []
    done = [r for r in results if r["tool"] == "billing_send_reminder"]
    if len(done) < len(to_send):
        # One reminder per turn, so each is a separate, individually controlled tool call.
        return [call("billing_send_reminder", invoice_id=to_send[len(done)]["invoice_id"])]
    sent = [r["result"]["invoice_id"] for r in done if "error" not in r["result"]]
    failed = [
        {"invoice_id": item["invoice_id"], "error": r["result"]["error"]}
        for item, r in zip(to_send, done, strict=False)
        if "error" in r["result"]
    ]
    if failed and not sent and any(r["result"].get("retryable") for r in done):
        return {"ok": False, "summary": "Reminders could not be sent.", "error": failed[0]["error"], "retryable": True}
    return {
        "ok": True,
        "summary": f"Sent {len(sent)} reminders; {len(failed)} not sent.",
        "data": {"sent": sent, "failed": failed},
    }


def _escalate(envelope: dict[str, Any], results: list[dict[str, Any]]) -> list[ToolCall] | dict[str, Any]:
    created = last_result(results, "ops_create_task")
    if created is None:
        return [
            call(
                "ops_create_task",
                title="Collections review needs a person",
                assignee="finance",
                detail=f"Automated collections stopped. {envelope['inputs'].get('reason', '')}",
            )
        ]
    if "error" in created:
        return {"ok": False, "summary": "Could not create the escalation task.", "error": created["error"]}
    return {"ok": True, "summary": "Escalated to finance.", "data": {"task_id": created["task_id"], "escalated": True}}


def collections_workflow() -> Workflow:
    return Workflow(
        name="invoice_collections",
        description="Review overdue invoices and send payment reminders according to the collections policy.",
        keywords=["invoice", "overdue", "collections", "reminder", "payment"],
        plan=_plan,
        replan=_replan,
        scripts={
            "research_policy": research_policy,
            "review_overdue": _review,
            "send_reminders": _send,
            "escalate_policy_gap": _escalate,
        },
        expects={
            "research_policy": ["grounded", "facts", "citations"],
            "review_overdue": ["decisions", "to_send"],
            "send_reminders": ["sent"],
            "escalate_policy_gap": ["escalated"],
        },
    )
