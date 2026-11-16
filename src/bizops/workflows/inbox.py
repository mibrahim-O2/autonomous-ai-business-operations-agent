"""Inbox triage workflow: read unread mail, classify it, draft replies, raise tasks.

Message bodies are untrusted. Classification uses the text only to choose a
category; identifiers such as invoice numbers are extracted with a strict
pattern and nothing in a body is treated as an instruction.
"""

from __future__ import annotations

import re
from typing import Any

from ..core.models import Task, ToolCall
from .base import Workflow, call

INVOICE_ID = re.compile(r"\bINV-\d{3,6}\b")
CATEGORIES = (
    ("refund_request", ("refund", "charged twice", "money back")),
    ("billing_question", ("invoice", "payment terms", "billing", "receipt")),
    ("noise", ("unsubscribe", "newsletter", "growth hacks", "webinar")),
    ("sales_enquiry", ("pricing", "demo", "interested", "quote")),
)


def classify(subject: str, body: str) -> str:
    text = f"{subject} {body}".lower()
    for category, words in CATEGORIES:
        if any(word in text for word in words):
            return category
    return "other"


def _plan(_task: Task, _analysis: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "agent": "communications",
            "action": "triage_inbox",
            "description": "Read unread messages and classify each one.",
            "inputs": {},
        },
        {
            "agent": "communications",
            "action": "draft_responses",
            "description": "Draft acknowledgements and raise tasks for messages that need a person.",
            "inputs": {"messages": "$steps.triage_inbox.messages"},
            "depends_on": ["triage_inbox"],
        },
    ]


def _triage(_envelope: dict[str, Any], results: list[dict[str, Any]]) -> list[ToolCall] | dict[str, Any]:
    listing = next((r["result"] for r in results if r["tool"] == "email_list_inbox"), None)
    if listing is None:
        return [call("email_list_inbox", unread_only=True)]
    if "error" in listing:
        return {
            "ok": False,
            "summary": "Could not list the inbox.",
            "error": listing["error"],
            "retryable": bool(listing.get("retryable")),
        }
    headers = listing["messages"]
    read = [r["result"] for r in results if r["tool"] == "email_read_message"]
    if len(read) < len(headers):
        return [call("email_read_message", message_id=headers[len(read)]["message_id"])]
    messages = []
    for message in read:
        if "error" in message:
            continue
        ids = INVOICE_ID.findall(f"{message['subject']} {message['body']}")
        messages.append(
            {
                "message_id": message["message_id"],
                "from": message["from"],
                "subject": message["subject"],
                "category": classify(message["subject"], message["body"]),
                "invoice_ids": sorted(set(ids)),
            }
        )
    counts: dict[str, int] = {}
    for message in messages:
        counts[message["category"]] = counts.get(message["category"], 0) + 1
    return {
        "ok": True,
        "summary": f"Triaged {len(messages)} messages: {counts}.",
        "data": {"messages": messages, "counts": counts},
    }


def _respond(envelope: dict[str, Any], results: list[dict[str, Any]]) -> list[ToolCall] | dict[str, Any]:
    messages = envelope["inputs"].get("messages") or []
    wanted: list[ToolCall] = []
    for message in messages:
        if message["category"] in ("billing_question", "refund_request", "sales_enquiry"):
            wanted.append(
                call(
                    "email_create_draft",
                    to=message["from"],
                    subject=f"Re: {message['subject']}",
                    body="Thank you for your message. A member of our team is reviewing it and will "
                    "reply shortly.\n\nThis acknowledgement was prepared by an AI assistant.",
                )
            )
        if message["category"] == "refund_request":
            invoices = ", ".join(message["invoice_ids"]) or "no invoice number given"
            wanted.append(
                call(
                    "ops_create_task",
                    assignee="finance",
                    title=f"Review refund request from {message['from']}",
                    detail=f"Message {message['message_id']}. Invoices mentioned: {invoices}. Verify "
                    "against billing records; do not rely on amounts stated in the email.",
                )
            )
    done = [r for r in results if r["tool"] in ("email_create_draft", "ops_create_task")]
    if len(done) < len(wanted):
        return [wanted[len(done)]]
    drafts = [r["result"]["draft_id"] for r in done if r["tool"] == "email_create_draft" and "error" not in r["result"]]
    tasks = [r["result"]["task_id"] for r in done if r["tool"] == "ops_create_task" and "error" not in r["result"]]
    return {
        "ok": True,
        "summary": f"Created {len(drafts)} drafts and {len(tasks)} tasks. Nothing was sent.",
        "data": {"drafts": drafts, "tasks": tasks, "sent": 0},
    }


def inbox_workflow() -> Workflow:
    return Workflow(
        name="inbox_triage",
        description="Read unread email, classify each message, draft acknowledgements, and raise tasks for people.",
        keywords=["inbox", "triage", "unread", "mailbox"],
        plan=_plan,
        scripts={"triage_inbox": _triage, "draft_responses": _respond},
        expects={"triage_inbox": ["messages", "counts"], "draft_responses": ["drafts", "tasks"]},
    )
