"""Refund request workflow. The action it ends in is financial and cannot be undone.

    research_policy       retrieve the refund policy and the amount above which finance must approve
    assess_refund         read the invoice record and decide whether the request is eligible
    issue_refund          issue the refund (the approval policy stops here and asks a person)
    draft_refund_notice   draft a confirmation to the customer on record (a draft, never sent)

What the workflow demonstrates:

  * Amounts and the recipient come from the invoice record, never from the request text.
    A request for more than the refundable balance is found ineligible before anyone is asked.
  * `issue_refund` cannot run without a person's decision. If the person rejects, or nobody
    answers before the deadline, the refund is not issued and the plan is replaced by
    `record_refund_outcome`, which hands the case to the finance team with the reason.
"""

from __future__ import annotations

from typing import Any

from ..core.models import Task, ToolCall
from .base import Workflow, call, last_result
from .research import research_policy

POLICY_FACTS = {"finance_threshold": r"above (\d+(?:\.\d+)?) USD additionally require"}


def _plan(task: Task, _analysis: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "agent": "research",
            "action": "research_policy",
            "description": "Retrieve the refund policy: eligibility and who must approve.",
            "inputs": {
                "question": "refund policy eligibility approval finance approver amount",
                "doc_types": ["policy"],
                "fallback_query": "refund paid invoice approval",
                "extract": POLICY_FACTS,
            },
        },
        {
            "agent": "finance_ops",
            "action": "assess_refund",
            "description": "Check the invoice record and decide whether the requested refund is eligible.",
            "inputs": {
                "invoice_id": "$task.inputs.invoice_id",
                "requested_amount": task.inputs.get("amount"),
                "reason": task.inputs.get("reason", "customer request"),
                "policy": "$steps.research_policy.facts",
            },
            "depends_on": ["research_policy"],
        },
        {
            "agent": "finance_ops",
            "action": "issue_refund",
            "description": "Issue the refund. Financial and irreversible: needs a person's approval.",
            "inputs": {
                "invoice_id": "$steps.assess_refund.invoice_id",
                "amount": "$steps.assess_refund.amount",
                "reason": "$steps.assess_refund.reason",
            },
            "depends_on": ["assess_refund"],
        },
        {
            "agent": "communications",
            "action": "draft_refund_notice",
            "description": "Draft a confirmation to the customer on the invoice record.",
            "inputs": {
                "to": "$steps.assess_refund.customer_email",
                "customer": "$steps.assess_refund.customer",
                "invoice_id": "$steps.assess_refund.invoice_id",
                "amount": "$steps.assess_refund.amount",
                "currency": "$steps.assess_refund.currency",
            },
            "depends_on": ["issue_refund"],
        },
    ]


def _replan(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    if envelope["failed_action"] == "record_refund_outcome":
        return []
    return [
        {
            "agent": "finance_ops",
            "action": "record_refund_outcome",
            "description": "The refund was not issued; hand the case to the finance team with the reason.",
            "inputs": {
                "invoice_id": (envelope.get("inputs") or {}).get("invoice_id", ""),
                "reason": f"{envelope['failed_action']} {envelope['failed_status']}: {envelope['failure']}",
            },
        }
    ]


def _assess(envelope: dict[str, Any], results: list[dict[str, Any]]) -> list[ToolCall] | dict[str, Any]:
    inputs = envelope["inputs"]
    policy = inputs.get("policy") or {}
    if "finance_threshold" not in policy:
        return {
            "ok": False,
            "summary": "Refund policy values are missing; refusing to guess.",
            "error": "no_grounded_policy",
        }
    invoice = last_result(results, "billing_get_invoice")
    if invoice is None:
        return [call("billing_get_invoice", invoice_id=str(inputs.get("invoice_id") or ""))]
    if "error" in invoice:
        return {
            "ok": False,
            "summary": "Could not read the invoice.",
            "error": invoice["error"],
            "retryable": bool(invoice.get("retryable")),
        }
    refundable = round(invoice["amount"] - invoice.get("refunded", 0.0), 2)
    try:
        requested = (
            float(inputs["requested_amount"]) if inputs.get("requested_amount") not in (None, "") else refundable
        )
    except (TypeError, ValueError):
        return {
            "ok": False,
            "summary": "The requested amount is not a number.",
            "error": "not_eligible: invalid amount",
        }
    if invoice["status"] != "paid":
        return {
            "ok": False,
            "summary": f"Invoice {invoice['invoice_id']} is {invoice['status']}; only paid invoices can be refunded.",
            "error": f"not_eligible: invoice is {invoice['status']}",
        }
    if requested <= 0 or requested > refundable:
        return {
            "ok": False,
            "summary": f"Requested {requested:g} but the refundable balance on the record is {refundable:g}.",
            "error": f"not_eligible: requested {requested:g} exceeds refundable balance {refundable:g}",
        }
    return {
        "ok": True,
        "summary": f"Eligible: {requested:g} {invoice['currency']} of {refundable:g} refundable "
        f"on {invoice['invoice_id']}.",
        "data": {
            "eligible": True,
            "invoice_id": invoice["invoice_id"],
            "amount": requested,
            "currency": invoice["currency"],
            "customer": invoice["customer"],
            "customer_email": invoice["customer_email"],
            "reason": str(inputs.get("reason") or "customer request"),
            "needs_finance_approver": requested > policy["finance_threshold"],
        },
    }


def _issue(envelope: dict[str, Any], results: list[dict[str, Any]]) -> list[ToolCall] | dict[str, Any]:
    inputs = envelope["inputs"]
    refund = last_result(results, "billing_issue_refund")
    if refund is None:
        return [
            call(
                "billing_issue_refund",
                invoice_id=inputs["invoice_id"],
                amount=float(inputs["amount"]),
                reason=str(inputs.get("reason") or "customer request"),
            )
        ]
    if "error" in refund:
        return {
            "ok": False,
            "summary": "The refund was not issued.",
            "error": refund["error"],
            "retryable": bool(refund.get("retryable")),
        }
    return {
        "ok": True,
        "summary": f"Refund {refund['refund_id']} of {refund['amount']:g} issued on {refund['invoice_id']}.",
        "data": {"refund_id": refund["refund_id"], "amount": refund["amount"], "invoice_id": refund["invoice_id"]},
    }


def _notice(envelope: dict[str, Any], results: list[dict[str, Any]]) -> list[ToolCall] | dict[str, Any]:
    inputs = envelope["inputs"]
    saved = last_result(results, "email_create_draft")
    if saved is None:
        body = (
            f"Hello,\n\nWe have refunded {float(inputs['amount']):.2f} {inputs.get('currency', 'USD')} against invoice "
            f"{inputs['invoice_id']}. It can take a few working days to appear on your statement.\n\n"
            "This message was prepared by an AI assistant and reviewed before sending."
        )
        return [call("email_create_draft", to=inputs["to"], subject=f"Refund for {inputs['invoice_id']}", body=body)]
    if "error" in saved:
        return {"ok": False, "summary": "Could not save the draft.", "error": saved["error"]}
    return {
        "ok": True,
        "summary": "Refund confirmation drafted (not sent).",
        "data": {"draft_id": saved["draft_id"], "sent": False},
    }


def _record(envelope: dict[str, Any], results: list[dict[str, Any]]) -> list[ToolCall] | dict[str, Any]:
    inputs = envelope["inputs"]
    created = last_result(results, "ops_create_task")
    if created is None:
        return [
            call(
                "ops_create_task",
                title=f"Refund not issued for {inputs.get('invoice_id') or 'an invoice'}",
                assignee="finance",
                detail=f"No refund was issued. {inputs.get('reason', '')} Review and reply to the customer.",
            )
        ]
    if "error" in created:
        return {"ok": False, "summary": "Could not record the outcome.", "error": created["error"]}
    return {
        "ok": True,
        "summary": "No refund issued; the case was handed to finance.",
        "data": {"task_id": created["task_id"], "refund_issued": False},
    }


def refund_workflow() -> Workflow:
    return Workflow(
        name="refund_request",
        description="Assess a refund request against the invoice record and policy, and issue it with approval.",
        # Phrases of intent, not the bare word: "what is our refund policy?" is a question for the
        # general workflow, not a request to move money.
        keywords=["refund request", "issue a refund", "refund for", "refund invoice", "money back", "chargeback"],
        plan=_plan,
        replan=_replan,
        scripts={
            "research_policy": research_policy,
            "assess_refund": _assess,
            "issue_refund": _issue,
            "draft_refund_notice": _notice,
            "record_refund_outcome": _record,
        },
        expects={
            "research_policy": ["grounded", "facts", "citations"],
            "assess_refund": ["eligible", "amount"],
            "issue_refund": ["refund_id"],
            "draft_refund_notice": ["draft_id"],
            "record_refund_outcome": ["task_id"],
        },
    )
