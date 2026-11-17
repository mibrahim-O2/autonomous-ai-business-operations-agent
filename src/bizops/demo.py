"""Demonstration scenarios shared by the command line and the dashboard."""

from __future__ import annotations

from typing import Any

DEMO_LEAD = {
    "name": "Noor Abbasi",
    "email": "noor.abbasi@quillfield.example",
    "company": "Quillfield Partners",
    "message": "We need help handling inbound enquiries. We have budget approved and would like a demo this week.",
}

# workflow name -> scenario shown in the dashboard and run by `bizops demo`
SCENARIOS: dict[str, dict[str, Any]] = {
    "lead_intake": {
        "key": "lead",
        "title": "Inbound lead to meeting proposal",
        "objective": "Handle a new inbound lead",
        "inputs": DEMO_LEAD,
        "summary": "A new enquiry arrives. The agents recall context, read the sales playbook, qualify the lead, "
        "draft a reply, and propose meeting slots.",
    },
    "invoice_collections": {
        "key": "collections",
        "title": "Overdue invoice collections",
        "objective": "Review overdue invoices and send payment reminders",
        "inputs": {},
        "summary": "The research agent retrieves the collections policy; the finance agent applies it to each "
        "overdue invoice and sends the reminders the policy allows.",
    },
    "refund_request": {
        "key": "refund",
        "title": "Refund request with human approval",
        "objective": "Handle a refund request for a paid invoice",
        "inputs": {"invoice_id": "INV-1005", "amount": 320, "reason": "Service cancelled before it started"},
        "summary": "The finance agent checks the request against the invoice record and the refund policy, then "
        "stops and waits for a person to approve the refund. Rejecting it hands the case to the finance team.",
    },
    "inbox_triage": {
        "key": "inbox",
        "title": "Inbox triage with untrusted content",
        "objective": "Triage the unread inbox",
        "inputs": {},
        "summary": "Unread email is read as untrusted data, classified, and answered with drafts. One message "
        "contains a prompt injection, which is ignored.",
    },
}
