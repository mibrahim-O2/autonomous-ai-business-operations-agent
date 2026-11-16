"""Lead intake workflow: recall context, qualify, draft a reply, propose meeting slots.

Inputs: name, email, company, message.
"""

from __future__ import annotations

from typing import Any

from ..core.models import Task, ToolCall
from .base import Workflow, call, last_result
from .research import research_policy

HOT_WORDS = ("urgent", "asap", "this week", "budget", "ready to", "contract", "demo")
COLD_WORDS = ("student", "homework", "just browsing", "job", "internship", "unsubscribe")


def score_lead(inputs: dict[str, Any], known: bool) -> tuple[int, str, list[str]]:
    """Transparent scoring rules. Each rule that fires is returned as a reason."""
    message = str(inputs.get("message", "")).lower()
    score, reasons = 40, ["base score 40"]
    if inputs.get("company"):
        score += 15
        reasons.append("company provided (+15)")
    hits = [w for w in HOT_WORDS if w in message]
    if hits:
        score += min(30, 10 * len(hits))
        reasons.append(f"buying signals {hits} (+{min(30, 10 * len(hits))})")
    cold = [w for w in COLD_WORDS if w in message]
    if cold:
        score -= 40
        reasons.append(f"disqualifying signals {cold} (-40)")
    if known:
        score += 10
        reasons.append("already known to the organisation (+10)")
    score = max(0, min(100, score))
    tier = "hot" if score >= 70 else "warm" if score >= 45 else "cold"
    return score, tier, reasons


def _plan(task: Task, _analysis: dict[str, Any]) -> list[dict[str, Any]]:
    lead = {key: f"$task.inputs.{key}" for key in ("name", "email", "company", "message")}
    return [
        {
            "agent": "research",
            "action": "recall_context",
            "description": "Check memory for prior knowledge of this person or company.",
            "inputs": {"query": f"{task.inputs.get('company', '')} {task.inputs.get('name', '')}".strip()},
        },
        {
            "agent": "research",
            "action": "research_policy",
            "description": "Retrieve the sales playbook's meeting length for introductory meetings.",
            "inputs": {
                "question": "sales playbook introductory meeting length minutes",
                "doc_types": ["playbook"],
                "fallback_query": "meetings business days",
                "optional": True,
                "extract": {"meeting_minutes": r"meetings are (\d+) minutes"},
            },
        },
        {
            "agent": "sales_ops",
            "action": "qualify_lead",
            "description": "Look the lead up in the CRM, score it, and store the result.",
            "inputs": {**lead, "known": "$steps.recall_context.known"},
            "depends_on": ["recall_context"],
        },
        {
            "agent": "communications",
            "action": "draft_reply",
            "description": "Draft a first reply appropriate to the lead's tier.",
            "inputs": {"email": lead["email"], "name": lead["name"], "tier": "$steps.qualify_lead.tier"},
            "depends_on": ["qualify_lead"],
        },
        {
            "agent": "scheduling",
            "action": "propose_slots",
            "description": "Offer meeting slots if the lead is hot or warm.",
            "inputs": {"tier": "$steps.qualify_lead.tier", "duration": "$steps.research_policy.facts.meeting_minutes"},
            "depends_on": ["qualify_lead", "research_policy"],
        },
    ]


def _replan(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    if envelope["failed_action"] == "log_followup_task":
        return []  # the recovery step itself failed; nothing further can be automated
    return [
        {
            "agent": "sales_ops",
            "action": "log_followup_task",
            "description": "Record that a person must finish this lead manually.",
            "inputs": {
                "email": "$task.inputs.email",
                "name": "$task.inputs.name",
                "company": "$task.inputs.company",
                "reason": f"{envelope['failed_action']} {envelope['failed_status']}: {envelope['failure']}",
            },
        }
    ]


def _recall(envelope: dict[str, Any], results: list[dict[str, Any]]) -> list[ToolCall] | dict[str, Any]:
    query = envelope["inputs"].get("query") or ""
    if len(query) < 2:  # nothing to search for; the agent decides retrieval is unnecessary
        return {
            "ok": True,
            "summary": "No company or name given; skipped retrieval.",
            "data": {"known": False, "prior_knowledge": [], "retrieval_used": False},
        }
    found = last_result(results, "memory_search")
    if found is None:
        return [call("memory_search", query=query)]
    hits = found.get("results", [])
    return {
        "ok": True,
        "summary": f"Found {len(hits)} relevant memories.",
        "data": {"known": bool(hits), "prior_knowledge": [h["text"] for h in hits], "retrieval_used": True},
    }


def _qualify(envelope: dict[str, Any], results: list[dict[str, Any]]) -> list[ToolCall] | dict[str, Any]:
    inputs = envelope["inputs"]
    lookup = last_result(results, "crm_find_contact")
    if lookup is None:
        return [call("crm_find_contact", email=inputs.get("email", ""))]
    if "error" in lookup:
        return {
            "ok": False,
            "summary": "CRM lookup failed.",
            "error": lookup["error"],
            "retryable": bool(lookup.get("retryable")),
        }
    known = bool(inputs.get("known")) or lookup["found"]
    score, tier, reasons = score_lead(inputs, known)
    saved = last_result(results, "crm_upsert_contact")
    if saved is None:
        return [
            call(
                "crm_upsert_contact",
                email=inputs.get("email", ""),
                name=inputs.get("name", ""),
                company=inputs.get("company", ""),
                fields={"lead_score": score, "lead_tier": tier, "source": "inbound"},
            )
        ]
    if "error" in saved:
        return {
            "ok": False,
            "summary": "Could not save the lead to the CRM.",
            "error": saved["error"],
            "retryable": bool(saved.get("retryable")),
        }
    if last_result(results, "crm_add_note") is None:
        return [
            call("crm_add_note", email=inputs["email"], note=f"Qualified as {tier} ({score}): " + "; ".join(reasons))
        ]
    company = inputs.get("company") or "unknown company"
    return {
        "ok": True,
        "summary": f"Lead qualified as {tier} with score {score}.",
        "data": {
            "tier": tier,
            "score": score,
            "reasons": reasons,
            "is_new_contact": saved["created"],
            "contact_email": saved["contact"]["email"],
        },
        "learned": [f"{inputs.get('name') or inputs['email']} of {company} enquired and was qualified as {tier}."],
    }


def _draft(envelope: dict[str, Any], results: list[dict[str, Any]]) -> list[ToolCall] | dict[str, Any]:
    inputs = envelope["inputs"]
    saved = last_result(results, "email_create_draft")
    if saved is None:
        first_name = str(inputs.get("name") or "there").split(" ")[0]
        bodies = {
            "hot": "Thank you for getting in touch. I would like to arrange a short call this week to understand "
            "what you need. I will send some times shortly.",
            "warm": "Thank you for your enquiry. Could you tell me a little more about your timeline and what you "
            "are hoping to achieve? I am happy to arrange a call.",
            "cold": "Thank you for your message. Based on what you have shared, we may not be the right fit at "
            "the moment, but please reply if your needs change.",
        }
        tier = inputs.get("tier", "warm")
        text = bodies.get(tier, bodies["warm"])
        body = f"Hello {first_name},\n\n{text}\n\nThis message was prepared by an AI assistant."
        return [call("email_create_draft", to=inputs.get("email", ""), subject="Re: your enquiry", body=body)]
    if "error" in saved:
        return {"ok": False, "summary": "Could not save the draft.", "error": saved["error"]}
    return {
        "ok": True,
        "summary": "Reply drafted and saved (not sent).",
        "data": {"draft_id": saved["draft_id"], "sent": False},
    }


def _slots(envelope: dict[str, Any], results: list[dict[str, Any]]) -> list[ToolCall] | dict[str, Any]:
    if envelope["inputs"].get("tier") == "cold":
        return {"ok": True, "summary": "Cold lead; no meeting offered.", "data": {"slots": [], "offered": False}}
    found = last_result(results, "calendar_find_slots")
    if found is None:
        return [
            call("calendar_find_slots", days_ahead=7, duration_minutes=int(envelope["inputs"].get("duration") or 30))
        ]
    if "error" in found:
        return {
            "ok": False,
            "summary": "Calendar lookup failed.",
            "error": found["error"],
            "retryable": bool(found.get("retryable")),
        }
    return {
        "ok": True,
        "summary": f"Proposed {len(found['slots'])} slots.",
        "data": {"slots": found["slots"], "offered": True},
    }


def _followup(envelope: dict[str, Any], results: list[dict[str, Any]]) -> list[ToolCall] | dict[str, Any]:
    inputs = envelope["inputs"]
    if last_result(results, "crm_upsert_contact") is None:
        return [
            call(
                "crm_upsert_contact",
                email=inputs.get("email", ""),
                name=inputs.get("name", ""),
                company=inputs.get("company", ""),
                fields={"needs_manual_followup": True},
            )
        ]
    if "error" in last_result(results, "crm_upsert_contact"):
        error = last_result(results, "crm_upsert_contact")["error"]
        return {"ok": False, "summary": "Could not record the follow-up.", "error": error}
    noted = last_result(results, "crm_add_note")
    if noted is None:
        return [
            call("crm_add_note", email=inputs["email"], note=f"Manual follow-up needed. {inputs.get('reason', '')}")
        ]
    if "error" in noted:
        return {"ok": False, "summary": "Could not record the follow-up.", "error": noted["error"]}
    return {"ok": True, "summary": "Flagged for manual follow-up.", "data": {"flagged": True}}


def lead_intake_workflow() -> Workflow:
    return Workflow(
        name="lead_intake",
        description="Handle a new inbound sales lead: recall context, qualify, draft a reply, propose meeting slots.",
        keywords=["lead", "enquiry", "inquiry", "prospect", "inbound"],
        plan=_plan,
        replan=_replan,
        scripts={
            "recall_context": _recall,
            "research_policy": research_policy,
            "qualify_lead": _qualify,
            "draft_reply": _draft,
            "propose_slots": _slots,
            "log_followup_task": _followup,
        },
        expects={
            "recall_context": ["known"],
            "qualify_lead": ["tier", "score"],
            "draft_reply": ["draft_id"],
            "propose_slots": ["slots"],
            "log_followup_task": ["flagged"],
        },
    )
