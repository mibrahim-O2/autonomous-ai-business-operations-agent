"""In-process tools.

These built-in tools give the first workflow something real to act on: they
read and write demo business records held in the database's key-value store.
The integration layer exposes richer versions of the same operations through
MCP; the tool names are kept stable so agents do not change.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel, Field

from ..core.errors import ToolError
from ..core.models import RiskLevel, new_id
from ..memory.store import MemoryStore
from .base import ToolContext, ToolSpec


class FindContact(BaseModel):
    email: str = Field(min_length=3)


class UpsertContact(BaseModel):
    email: str = Field(min_length=3, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    name: str = ""
    company: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)


class AddNote(BaseModel):
    email: str
    note: str = Field(min_length=1, max_length=2000)


class CreateDraft(BaseModel):
    to: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=5000)


class FindSlots(BaseModel):
    days_ahead: int = Field(default=5, ge=1, le=30)
    duration_minutes: int = Field(default=30, ge=15, le=240)
    start_date: date | None = None


class MemorySearch(BaseModel):
    query: str = Field(min_length=2)
    limit: int = Field(default=5, ge=1, le=20)


class LocalToolExecutor:
    """Registers the built-in tools and dispatches calls to them."""

    def __init__(self, memory: MemoryStore):
        self.memory = memory
        self._specs: dict[str, ToolSpec] = {}
        for spec in (
            ToolSpec(
                "crm_find_contact",
                "Look up a CRM contact by email address.",
                RiskLevel.READ,
                FindContact,
                handler=self._find_contact,
            ),
            ToolSpec(
                "crm_upsert_contact",
                "Create or update a CRM contact. Returns the stored record.",
                RiskLevel.LOW,
                UpsertContact,
                handler=self._upsert_contact,
            ),
            ToolSpec(
                "crm_add_note",
                "Append an internal note to a CRM contact.",
                RiskLevel.LOW,
                AddNote,
                handler=self._add_note,
            ),
            ToolSpec(
                "email_create_draft",
                "Save an email draft. The draft is NOT sent.",
                RiskLevel.LOW,
                CreateDraft,
                handler=self._create_draft,
            ),
            ToolSpec(
                "calendar_find_slots",
                "List free meeting slots on upcoming business days.",
                RiskLevel.READ,
                FindSlots,
                handler=self._find_slots,
            ),
            ToolSpec(
                "memory_search",
                "Search long-term memory (facts and past runs) for relevant knowledge.",
                RiskLevel.READ,
                MemorySearch,
                handler=self._memory_search,
            ),
        ):
            self._specs[spec.name] = spec

    def register(self, spec: ToolSpec) -> None:
        self._specs[spec.name] = spec

    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    async def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
        spec = self._specs[name]
        assert spec.handler is not None
        parsed = spec.input_model.model_validate(arguments) if spec.input_model else arguments
        return await spec.handler(parsed, ctx)

    # Handlers ----------------------------------------------------------------
    async def _find_contact(self, args: FindContact, ctx: ToolContext) -> dict[str, Any]:
        contact = ctx.db.kv_get(ctx.tenant_id, "crm.contacts", args.email.lower())
        return {"found": contact is not None, "contact": contact}

    async def _upsert_contact(self, args: UpsertContact, ctx: ToolContext) -> dict[str, Any]:
        key = args.email.lower()
        existing = ctx.db.kv_get(ctx.tenant_id, "crm.contacts", key) or {}
        contact = {
            **existing,
            "email": key,
            **{k: v for k, v in {"name": args.name, "company": args.company}.items() if v},
            **args.fields,
        }
        contact.setdefault("notes", [])
        ctx.db.kv_put(ctx.tenant_id, "crm.contacts", key, contact)
        return {"created": not existing, "contact": contact}

    async def _add_note(self, args: AddNote, ctx: ToolContext) -> dict[str, Any]:
        key = args.email.lower()
        contact = ctx.db.kv_get(ctx.tenant_id, "crm.contacts", key)
        if contact is None:
            raise ToolError(f"no CRM contact with email {key}")
        contact.setdefault("notes", []).append({"by": ctx.actor, "run_id": ctx.run_id, "text": args.note})
        ctx.db.kv_put(ctx.tenant_id, "crm.contacts", key, contact)
        return {"notes": len(contact["notes"])}

    async def _create_draft(self, args: CreateDraft, ctx: ToolContext) -> dict[str, Any]:
        draft_id = new_id("draft")
        ctx.db.kv_put(
            ctx.tenant_id,
            "email.drafts",
            draft_id,
            {
                "draft_id": draft_id,
                "to": args.to,
                "subject": args.subject,
                "body": args.body,
                "status": "draft",
                "run_id": ctx.run_id,
            },
        )
        return {"draft_id": draft_id, "status": "draft"}

    async def _find_slots(self, args: FindSlots, ctx: ToolContext) -> dict[str, Any]:
        booked = {e["start"] for e in ctx.db.kv_list(ctx.tenant_id, "calendar.events").values()}
        day, slots = args.start_date or date.today(), []
        while len(slots) < 4 and (day - (args.start_date or date.today())).days <= args.days_ahead:
            day += timedelta(days=1)
            if day.weekday() >= 5:
                continue
            for hour in (10, 14):
                start = f"{day.isoformat()}T{hour:02d}:00"
                if start not in booked and len(slots) < 4:
                    slots.append({"start": start, "duration_minutes": args.duration_minutes})
        return {"slots": slots}

    async def _memory_search(self, args: MemorySearch, ctx: ToolContext) -> dict[str, Any]:
        hits = self.memory.search(ctx.tenant_id, args.query, limit=args.limit)
        return {"results": [{"kind": h.kind, "text": h.text, "score": h.score} for h in hits]}
