"""Adapter contracts for the four business systems the agents operate.

A real integration implements one of these protocols. Everything above the
adapter (tools, agents, policies) is unaware of which implementation is in use.
Every method takes the tenant id first: adapters are the last place where
data isolation can be enforced, so they must scope every read and write.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..core.errors import ToolError


class IntegrationError(ToolError):
    """A business system rejected or failed a request. `retryable` marks transient faults."""


class CRMAdapter(Protocol):
    def find_contact(self, tenant_id: str, email: str) -> dict[str, Any] | None: ...
    def search_contacts(self, tenant_id: str, query: str, limit: int = 10) -> list[dict[str, Any]]: ...
    def upsert_contact(self, tenant_id: str, email: str, fields: dict[str, Any]) -> tuple[dict[str, Any], bool]: ...
    def add_note(self, tenant_id: str, email: str, note: str, author: str) -> int: ...
    def merge_contacts(self, tenant_id: str, primary_email: str, duplicate_email: str) -> dict[str, Any]: ...


class EmailAdapter(Protocol):
    def list_inbox(self, tenant_id: str, unread_only: bool = True) -> list[dict[str, Any]]: ...
    def read_message(self, tenant_id: str, message_id: str) -> dict[str, Any]: ...
    def create_draft(self, tenant_id: str, to: str, subject: str, body: str) -> dict[str, Any]: ...
    def send(self, tenant_id: str, to: str, subject: str, body: str, draft_id: str | None = None) -> dict[str, Any]: ...


class CalendarAdapter(Protocol):
    def find_slots(
        self, tenant_id: str, start_date: str | None, days_ahead: int, duration_minutes: int
    ) -> list[dict[str, Any]]: ...
    def create_event(
        self, tenant_id: str, title: str, start: str, duration_minutes: int, attendees: list[str]
    ) -> dict[str, Any]: ...
    def cancel_event(self, tenant_id: str, event_id: str) -> dict[str, Any]: ...


class BillingAdapter(Protocol):
    def list_invoices(self, tenant_id: str, status: str | None = None) -> list[dict[str, Any]]: ...
    def get_invoice(self, tenant_id: str, invoice_id: str) -> dict[str, Any]: ...
    def record_reminder(self, tenant_id: str, invoice_id: str) -> dict[str, Any]: ...
    def issue_refund(self, tenant_id: str, invoice_id: str, amount: float, reason: str) -> dict[str, Any]: ...


@dataclass
class Integrations:
    """The set of adapters in use. Swap any field to connect a real system."""

    crm: CRMAdapter
    email: EmailAdapter
    calendar: CalendarAdapter
    billing: BillingAdapter
