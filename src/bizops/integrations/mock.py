"""Mock business systems.

Each adapter keeps real state in the database's key-value store, so tool calls
have observable effects: a sent email appears in the sent folder, a merged
contact disappears, a refund lowers the refundable balance. Nothing leaves the
machine.

`MockControl` injects faults (for example "fail the next two calendar calls"),
which is how retry, circuit-breaker, and recovery behaviour is demonstrated and
tested without a flaky real service.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..core.models import new_id
from ..storage import Database
from .base import IntegrationError, Integrations

SEED_PATH = Path(__file__).resolve().parents[3] / "data" / "demo" / "seed.json"


class MockControl:
    """Fault injection and a controllable clock for the mock systems."""

    def __init__(self, db: Database):
        self.db = db

    def set_fault(
        self, tenant_id: str, operation: str, times: int = 1, retryable: bool = True, message: str = "simulated outage"
    ) -> None:
        self.db.kv_put(
            tenant_id, "mock.faults", operation, {"times": times, "retryable": retryable, "message": message}
        )

    def check(self, tenant_id: str, operation: str) -> None:
        """Raise the configured fault for an operation, consuming one occurrence."""
        fault = self.db.kv_get(tenant_id, "mock.faults", operation)
        if not fault or fault["times"] <= 0:
            return
        fault["times"] -= 1
        self.db.kv_put(tenant_id, "mock.faults", operation, fault)
        raise IntegrationError(f"{operation}: {fault['message']}", retryable=fault["retryable"])

    def today(self, tenant_id: str) -> date:
        """The demo's reference date if one was seeded, otherwise the real date."""
        meta = self.db.kv_get(tenant_id, "mock.meta", "reference_date")
        return date.fromisoformat(meta) if meta else date.today()


class _Base:
    def __init__(self, db: Database, control: MockControl):
        self.db, self.control = db, control


class MockCRM(_Base):
    NS = "crm.contacts"

    def find_contact(self, tenant_id: str, email: str) -> dict[str, Any] | None:
        self.control.check(tenant_id, "crm.find_contact")
        return self.db.kv_get(tenant_id, self.NS, email.lower())

    def search_contacts(self, tenant_id: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
        self.control.check(tenant_id, "crm.search_contacts")
        needle = query.lower()
        contacts = [c for c in self.db.kv_list(tenant_id, self.NS).values() if not c.get("archived")]
        hits = [c for c in contacts if needle in json.dumps(c).lower()] if needle else contacts
        return hits[:limit]

    def upsert_contact(self, tenant_id: str, email: str, fields: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        self.control.check(tenant_id, "crm.upsert_contact")
        key = email.lower()
        existing = self.db.kv_get(tenant_id, self.NS, key)
        contact = {
            "notes": [],
            **(existing or {}),
            **{k: v for k, v in fields.items() if v not in (None, "")},
            "email": key,
        }
        self.db.kv_put(tenant_id, self.NS, key, contact)
        return contact, existing is None

    def add_note(self, tenant_id: str, email: str, note: str, author: str) -> int:
        self.control.check(tenant_id, "crm.add_note")
        contact = self.db.kv_get(tenant_id, self.NS, email.lower())
        if contact is None:
            raise IntegrationError(f"no CRM contact with email {email.lower()}")
        contact.setdefault("notes", []).append({"by": author, "text": note})
        self.db.kv_put(tenant_id, self.NS, email.lower(), contact)
        return len(contact["notes"])

    def merge_contacts(self, tenant_id: str, primary_email: str, duplicate_email: str) -> dict[str, Any]:
        """Irreversible: the duplicate is archived and its notes move to the primary."""
        self.control.check(tenant_id, "crm.merge_contacts")
        primary = self.db.kv_get(tenant_id, self.NS, primary_email.lower())
        duplicate = self.db.kv_get(tenant_id, self.NS, duplicate_email.lower())
        if primary is None or duplicate is None or primary_email.lower() == duplicate_email.lower():
            raise IntegrationError("both contacts must exist and be different")
        if duplicate.get("archived"):
            raise IntegrationError(f"{duplicate_email} is already archived")
        for key, value in duplicate.items():
            if key not in ("email", "notes", "archived", "merged_into") and not primary.get(key) and value:
                primary[key] = value
        primary.setdefault("notes", []).extend(duplicate.get("notes", []))
        duplicate.update(archived=True, merged_into=primary["email"])
        self.db.kv_put(tenant_id, self.NS, primary["email"], primary)
        self.db.kv_put(tenant_id, self.NS, duplicate["email"], duplicate)
        return primary


class MockEmail(_Base):
    def list_inbox(self, tenant_id: str, unread_only: bool = True) -> list[dict[str, Any]]:
        self.control.check(tenant_id, "email.list_inbox")
        messages = self.db.kv_list(tenant_id, "email.inbox").values()
        return [
            {k: m[k] for k in ("message_id", "from", "subject", "received_at", "read")}
            for m in messages
            if not (unread_only and m.get("read"))
        ]

    def read_message(self, tenant_id: str, message_id: str) -> dict[str, Any]:
        self.control.check(tenant_id, "email.read_message")
        message = self.db.kv_get(tenant_id, "email.inbox", message_id)
        if message is None:
            raise IntegrationError(f"no message {message_id}")
        message["read"] = True
        self.db.kv_put(tenant_id, "email.inbox", message_id, message)
        return message

    def create_draft(self, tenant_id: str, to: str, subject: str, body: str) -> dict[str, Any]:
        self.control.check(tenant_id, "email.create_draft")
        draft = {"draft_id": new_id("draft"), "to": to, "subject": subject, "body": body, "status": "draft"}
        self.db.kv_put(tenant_id, "email.drafts", draft["draft_id"], draft)
        return draft

    def send(self, tenant_id: str, to: str, subject: str, body: str, draft_id: str | None = None) -> dict[str, Any]:
        """External effect: in a real system this message would leave the organisation."""
        self.control.check(tenant_id, "email.send")
        sent = {"sent_id": new_id("sent"), "to": to, "subject": subject, "body": body, "status": "sent"}
        self.db.kv_put(tenant_id, "email.sent", sent["sent_id"], sent)
        if draft_id and (draft := self.db.kv_get(tenant_id, "email.drafts", draft_id)):
            draft["status"] = "sent"
            self.db.kv_put(tenant_id, "email.drafts", draft_id, draft)
        return {"sent_id": sent["sent_id"], "status": "sent", "to": to}


class MockCalendar(_Base):
    def find_slots(
        self, tenant_id: str, start_date: str | None, days_ahead: int, duration_minutes: int
    ) -> list[dict[str, Any]]:
        self.control.check(tenant_id, "calendar.find_slots")
        booked = {
            e["start"] for e in self.db.kv_list(tenant_id, "calendar.events").values() if e.get("status") != "cancelled"
        }
        first = date.fromisoformat(start_date) if start_date else self.control.today(tenant_id)
        day, slots = first, []
        while len(slots) < 4 and (day - first).days < days_ahead:
            day += timedelta(days=1)
            if day.weekday() >= 5:
                continue
            for hour in (10, 14):
                start = f"{day.isoformat()}T{hour:02d}:00"
                if start not in booked and len(slots) < 4:
                    slots.append({"start": start, "duration_minutes": duration_minutes})
        return slots

    def create_event(
        self, tenant_id: str, title: str, start: str, duration_minutes: int, attendees: list[str]
    ) -> dict[str, Any]:
        self.control.check(tenant_id, "calendar.create_event")
        for event in self.db.kv_list(tenant_id, "calendar.events").values():
            if event["start"] == start and event.get("status") != "cancelled":
                raise IntegrationError(f"slot {start} is already booked")
        event = {
            "event_id": new_id("evt"),
            "title": title,
            "start": start,
            "duration_minutes": duration_minutes,
            "attendees": attendees,
            "status": "confirmed",
        }
        self.db.kv_put(tenant_id, "calendar.events", event["event_id"], event)
        return event

    def cancel_event(self, tenant_id: str, event_id: str) -> dict[str, Any]:
        self.control.check(tenant_id, "calendar.cancel_event")
        event = self.db.kv_get(tenant_id, "calendar.events", event_id)
        if event is None:
            raise IntegrationError(f"no event {event_id}")
        event["status"] = "cancelled"
        self.db.kv_put(tenant_id, "calendar.events", event_id, event)
        return event


class MockBilling(_Base):
    NS = "billing.invoices"

    def list_invoices(self, tenant_id: str, status: str | None = None) -> list[dict[str, Any]]:
        self.control.check(tenant_id, "billing.list_invoices")
        invoices = list(self.db.kv_list(tenant_id, self.NS).values())
        return [i for i in invoices if status is None or i["status"] == status]

    def get_invoice(self, tenant_id: str, invoice_id: str) -> dict[str, Any]:
        self.control.check(tenant_id, "billing.get_invoice")
        invoice = self.db.kv_get(tenant_id, self.NS, invoice_id)
        if invoice is None:
            raise IntegrationError(f"no invoice {invoice_id}")
        return invoice

    def record_reminder(self, tenant_id: str, invoice_id: str) -> dict[str, Any]:
        self.control.check(tenant_id, "billing.record_reminder")
        invoice = self.get_invoice(tenant_id, invoice_id)
        invoice["reminders_sent"] = invoice.get("reminders_sent", 0) + 1
        self.db.kv_put(tenant_id, self.NS, invoice_id, invoice)
        return invoice

    def issue_refund(self, tenant_id: str, invoice_id: str, amount: float, reason: str) -> dict[str, Any]:
        """Financial and irreversible. Enforces the invariants a real billing system would."""
        self.control.check(tenant_id, "billing.issue_refund")
        invoice = self.get_invoice(tenant_id, invoice_id)
        if invoice["status"] != "paid":
            raise IntegrationError(f"invoice {invoice_id} is {invoice['status']}; only paid invoices can be refunded")
        refunded = invoice.get("refunded", 0.0)
        if amount <= 0 or amount > invoice["amount"] - refunded + 1e-9:
            raise IntegrationError(f"refund {amount} exceeds refundable balance {invoice['amount'] - refunded}")
        invoice["refunded"] = round(refunded + amount, 2)
        self.db.kv_put(tenant_id, self.NS, invoice_id, invoice)
        refund = {"refund_id": new_id("rf"), "invoice_id": invoice_id, "amount": amount, "reason": reason}
        self.db.kv_put(tenant_id, "billing.refunds", refund["refund_id"], refund)
        return refund


def build_mock_integrations(db: Database) -> tuple[Integrations, MockControl]:
    control = MockControl(db)
    return Integrations(
        MockCRM(db, control), MockEmail(db, control), MockCalendar(db, control), MockBilling(db, control)
    ), control


def seed_demo_data(db: Database, tenant_id: str, path: Path = SEED_PATH, force: bool = False) -> bool:
    """Load the fictional data set for a tenant. Does nothing if the tenant is already seeded."""
    if db.kv_get(tenant_id, "mock.meta", "seeded") and not force:
        return False
    seed = json.loads(path.read_text(encoding="utf-8"))
    for contact in seed["contacts"]:
        db.kv_put(tenant_id, "crm.contacts", contact["email"], {"notes": [], **contact})
    for invoice in seed["invoices"]:
        db.kv_put(tenant_id, "billing.invoices", invoice["invoice_id"], invoice)
    for message in seed["inbox"]:
        db.kv_put(tenant_id, "email.inbox", message["message_id"], {"read": False, **message})
    for event in seed["events"]:
        db.kv_put(tenant_id, "calendar.events", event["event_id"], {"status": "confirmed", **event})
    db.kv_put(tenant_id, "mock.meta", "reference_date", seed["reference_date"])
    db.kv_put(tenant_id, "mock.meta", "seeded", True)
    return True
