"""In-process tools: memory, knowledge retrieval, and internal task creation.

Tools that touch business systems (CRM, email, calendar, billing) are served by
the MCP server. What stays local are tools over the platform's own stores.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..core.models import RiskLevel, new_id
from ..knowledge import KnowledgeBase
from ..memory.store import MemoryStore
from .base import ToolContext, ToolSpec


class MemorySearch(BaseModel):
    query: str = Field(min_length=2)
    limit: int = Field(default=5, ge=1, le=20)


class KnowledgeSearch(BaseModel):
    query: str = Field(min_length=2)
    limit: int = Field(default=4, ge=1, le=10)
    doc_types: list[str] = Field(default_factory=list, description="Restrict to these document types, e.g. policy")
    trusted_only: bool = Field(default=False, description="Exclude documents that originated outside the organisation")


class CreateTask(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    detail: str = Field(default="", max_length=2000)
    assignee: str = "operations"


class LocalToolExecutor:
    def __init__(self, memory: MemoryStore, knowledge: KnowledgeBase):
        self.memory, self.knowledge = memory, knowledge
        self._specs: dict[str, ToolSpec] = {}
        for spec in (
            ToolSpec(
                "memory_search",
                "Search long-term memory (facts learned and past runs).",
                RiskLevel.READ,
                MemorySearch,
                handler=self._memory_search,
            ),
            ToolSpec(
                "kb_search",
                "Search the document knowledge base (policies, playbooks). Returns passages with citations.",
                RiskLevel.READ,
                KnowledgeSearch,
                handler=self._kb_search,
            ),
            ToolSpec(
                "ops_create_task",
                "Create an internal task for a person to follow up.",
                RiskLevel.LOW,
                CreateTask,
                handler=self._create_task,
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

    async def _memory_search(self, args: MemorySearch, ctx: ToolContext) -> dict[str, Any]:
        hits = self.memory.search(ctx.tenant_id, args.query, limit=args.limit)
        return {"results": [{"kind": h.kind, "text": h.text, "score": h.score} for h in hits]}

    async def _kb_search(self, args: KnowledgeSearch, ctx: ToolContext) -> dict[str, Any]:
        hits = self.knowledge.search(
            ctx.tenant_id,
            args.query,
            limit=args.limit,
            doc_types=args.doc_types or None,
            trusted_only=args.trusted_only,
        )
        results = [
            {"citation": h.citation, "text": h.text, "score": h.score, "doc_type": h.doc_type, "trusted": h.trusted}
            for h in hits
        ]
        # The gateway reads this marker: a result that includes outside material taints the run.
        return {"results": results, "_untrusted": any(not h.trusted for h in hits)}

    async def _create_task(self, args: CreateTask, ctx: ToolContext) -> dict[str, Any]:
        # The idempotency key makes a replayed call return the same task instead of a second one.
        key = ctx.idempotency_key or new_id("k")
        existing = ctx.db.idempotency_get(ctx.tenant_id, key)
        if existing is not None:
            return existing
        task = {
            "task_id": new_id("optask"),
            "title": args.title,
            "detail": args.detail,
            "assignee": args.assignee,
            "status": "open",
            "run_id": ctx.run_id,
        }
        ctx.db.kv_put(ctx.tenant_id, "ops.tasks", task["task_id"], task)
        ctx.db.idempotency_put(ctx.tenant_id, key, "ops.create_task", task)
        return task
