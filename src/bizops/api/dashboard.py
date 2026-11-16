"""Dashboard API: the contract between the web dashboard and the runtime.

Every response is built from live runtime state (the run store, the tool
gateway, the agent registry, the knowledge base). Nothing here is canned.

The same JSON shapes are exported by `scripts/export_demo_snapshot.py` for the
read-only GitHub Pages demonstration, so the static demo and the live
application share one contract.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import __version__
from ..core.errors import RunNotFound
from ..core.models import TERMINAL_RUN_STATES, RunState, RunStatus, Task
from ..demo import SCENARIOS
from ..integrations.mock import MockBilling, MockCalendar, MockCRM, MockEmail
from ..llm.scripted import ScriptedLLM
from ..runtime import Runtime

ACTIVE = {RunStatus.RUNNING, RunStatus.PAUSED, RunStatus.WAITING_APPROVAL, RunStatus.PENDING}
ACTIVITY_KINDS = [
    "run.created",
    "run.completed",
    "run.failed",
    "run.waiting_approval",
    "run.paused",
    "run.cancelled",
    "plan.replanned",
    "step.handoff",
    "webhook.accepted",
]


class AgentToggle(BaseModel):
    enabled: bool


class StartWorkflow(BaseModel):
    inputs: dict[str, Any] | None = None
    objective: str | None = Field(default=None, max_length=4000)


class IngestDocument(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=200_000)
    doc_type: str = Field(default="document", max_length=40)
    trusted: bool = True
    tags: list[str] = Field(default_factory=list, max_length=20)


class SearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    limit: int = Field(default=5, ge=1, le=20)
    doc_types: list[str] = Field(default_factory=list)
    trusted_only: bool = False
    mode: str = Field(default="hybrid", pattern="^(hybrid|lexical|vector)$")


def run_summary(state: RunState) -> dict[str, Any]:
    steps = state.plan.steps if state.plan else []
    return {
        "run_id": state.run_id,
        "status": state.status.value,
        "objective": state.task.objective,
        "workflow": state.analysis.get("workflow") or state.task.workflow,
        "source": state.task.source,
        "requested_by": state.task.requested_by,
        "created_at": state.created_at.isoformat(),
        "updated_at": state.updated_at.isoformat(),
        "duration_ms": round((state.updated_at - state.created_at).total_seconds() * 1000),
        "steps_total": len(steps),
        "steps_done": sum(s.status.value == "succeeded" for s in steps),
        "replans": state.replans,
        "tainted": state.tainted,
        "outcome": (state.final_report or {}).get("outcome"),
        "error": state.error,
        "usage": state.usage.model_dump(),
    }


def run_detail(state: RunState) -> dict[str, Any]:
    steps = []
    for step in state.plan.steps if state.plan else []:
        steps.append(
            {
                "step_id": step.step_id,
                "index": step.index,
                "agent": step.agent,
                "action": step.action,
                "description": step.description,
                "status": step.status.value,
                "attempts": step.attempts,
                "summary": (step.result or {}).get("_summary", ""),
                "error": step.error,
                "result": {k: v for k, v in (step.result or {}).items() if not k.startswith("_")},
                "depends_on": step.depends_on,
                "started_at": step.started_at.isoformat() if step.started_at else None,
                "finished_at": step.finished_at.isoformat() if step.finished_at else None,
                "tool_calls": [
                    {
                        "tool": t.tool,
                        "ok": t.ok,
                        "error": t.error,
                        "duration_ms": t.duration_ms,
                        "untrusted": t.untrusted,
                        "replayed": t.replayed,
                        "retryable": t.retryable,
                        "output": t.output,
                    }
                    for t in step.tool_calls
                ],
            }
        )
    return run_summary(state) | {
        "inputs": state.task.inputs,
        "analysis": state.analysis,
        "plan": {"version": state.plan.version, "rationale": state.plan.rationale} if state.plan else None,
        "steps": steps,
        "working_memory": state.working_memory,
        "final_report": state.final_report,
        "pending_approval_id": state.pending_approval_id,
    }


def build_dashboard_router(rt: Runtime, access: Any, background: set[asyncio.Task]) -> APIRouter:
    router = APIRouter(prefix="/api", dependencies=[Depends(access)])

    def tenant() -> str:
        return rt.settings.default_tenant

    def live_model() -> bool:
        return not isinstance(rt.llm, ScriptedLLM)

    @router.get("/meta")
    async def meta() -> dict[str, Any]:
        return {
            "mode": "live",
            "version": __version__,
            "tenant": tenant(),
            "llm": {"provider": rt.llm.name, "model": rt.llm.model, "live_model": live_model()},
            "auth": "api_key" if rt.settings.api_key else "local_only",
        }

    @router.get("/overview")
    async def overview() -> dict[str, Any]:
        runs = rt.db.load_runs(tenant(), 500)
        by_status = Counter(r.status.value for r in runs)
        finished = by_status["completed"] + by_status["failed"]
        by_workflow: dict[str, Counter[str]] = {}
        by_day: dict[str, Counter[str]] = {}
        tokens_in = tokens_out = calls = 0
        cost = 0.0
        for run in runs:
            workflow = run.analysis.get("workflow") or run.task.workflow or "unclassified"
            by_workflow.setdefault(workflow, Counter())[run.status.value] += 1
            by_day.setdefault(run.created_at.date().isoformat(), Counter())[run.status.value] += 1
            tokens_in += run.usage.input_tokens
            tokens_out += run.usage.output_tokens
            calls += run.usage.calls
            cost += run.usage.cost_usd
        activity = [
            {
                "ts": e["ts"],
                "kind": e["kind"],
                "run_id": e["run_id"],
                "detail": {
                    k: v
                    for k, v in e["payload"].items()
                    if k
                    in ("objective", "outcome", "error", "step", "tool", "reason", "failed_step", "to_agent", "source")
                },
            }
            for e in rt.db.recent_events(tenant(), ACTIVITY_KINDS, 15)
        ]
        return {
            "totals": {
                "runs": len(runs),
                "completed": by_status["completed"],
                "failed": by_status["failed"],
                "cancelled": by_status["cancelled"],
                "pending_approvals": by_status["waiting_approval"],
                "active": sum(r.status in ACTIVE for r in runs),
                "success_rate": round(by_status["completed"] / finished, 4) if finished else None,
            },
            "by_status": dict(by_status),
            "by_workflow": [
                {"workflow": k, **dict(v), "total": sum(v.values())} for k, v in sorted(by_workflow.items())
            ],
            "by_day": [{"day": k, **dict(v), "total": sum(v.values())} for k, v in sorted(by_day.items())][-14:],
            "usage": {
                "input_tokens": tokens_in,
                "output_tokens": tokens_out,
                "model_calls": calls,
                "cost_usd": round(cost, 6),
                "provider": rt.llm.name,
                "model": rt.llm.model,
            },
            "recent_runs": [run_summary(r) for r in runs[:6]],
            "activity": activity,
        }

    # Agents ------------------------------------------------------------------
    @router.get("/agents")
    async def agents() -> list[dict[str, Any]]:
        all_tools = rt.gateway.all_specs()
        recent = Counter(e["payload"].get("agent") for e in rt.db.list_events_by_kind(tenant(), "step.execute", 500))
        out = []
        for spec in rt.registry.specs():
            tools = [t for t in all_tools if rt.gateway.is_allowed(t.name, spec.allowed_tools)]
            out.append(
                {
                    "name": spec.name,
                    "role": spec.role,
                    "enabled": rt.registry.is_enabled(rt.db, tenant(), spec.name),
                    "prompt_version": spec.prompt_version,
                    "system_prompt": spec.system_prompt,
                    "actions": spec.actions,
                    "allow_list": spec.allowed_tools,
                    "max_turns": spec.max_turns,
                    "max_tools_offered": spec.max_tools_offered,
                    "tools": [{"name": t.name, "risk": t.risk.value, "source": t.source} for t in tools],
                    "steps_executed": recent.get(spec.name, 0),
                }
            )
        return out

    @router.post("/agents/{name}/enabled")
    async def set_agent_enabled(name: str, body: AgentToggle) -> dict[str, Any]:
        try:
            rt.registry.set_enabled(rt.db, tenant(), name, body.enabled)
        except KeyError as exc:
            raise HTTPException(404, f"no agent named '{name}'") from exc
        rt.tracer.event(tenant(), None, "agent.enabled_changed", agent=name, enabled=body.enabled)
        return {"name": name, "enabled": rt.registry.is_enabled(rt.db, tenant(), name)}

    # Workflows ---------------------------------------------------------------
    @router.get("/workflows")
    async def workflows() -> list[dict[str, Any]]:
        runs = rt.db.load_runs(tenant(), 500)
        counts: dict[str, Counter[str]] = {}
        for run in runs:
            counts.setdefault(run.analysis.get("workflow") or "", Counter())[run.status.value] += 1
        out = []
        for name in rt.workflows.names():
            workflow = rt.workflows.get(name)
            assert workflow is not None
            scenario = SCENARIOS.get(name)
            template = workflow.plan(
                Task(objective=workflow.description, inputs=(scenario or {}).get("inputs", {})), {}
            )
            out.append(
                {
                    "name": name,
                    "description": workflow.description,
                    "version": workflow.version,
                    "keywords": workflow.keywords,
                    "has_recovery_plan": workflow.replan is not None,
                    "steps": [
                        {
                            "agent": s["agent"],
                            "action": s["action"],
                            "description": s.get("description", ""),
                            "depends_on": s.get("depends_on", []),
                        }
                        for s in template
                    ],
                    "expected_outputs": workflow.expects,
                    "scenario": {k: scenario[k] for k in ("title", "summary", "objective", "inputs")}
                    if scenario
                    else None,
                    "runs": dict(counts.get(name, {})),
                }
            )
        return out

    @router.post("/workflows/{name}/start", status_code=202)
    async def start_workflow(name: str, body: StartWorkflow) -> dict[str, Any]:
        """Create a run and execute it in the background. The dashboard polls the run for progress."""
        if rt.workflows.get(name) is None:
            raise HTTPException(404, f"no workflow named '{name}'")
        scenario = SCENARIOS.get(name, {})
        objective = body.objective or scenario.get("objective") or rt.workflows.get(name).description
        inputs = body.inputs if body.inputs is not None else scenario.get("inputs", {})
        task = Task(
            tenant_id=tenant(),
            objective=objective,
            workflow=name,
            inputs=inputs,
            source="api",
            requested_by="dashboard",
        )
        state, _ = rt.orchestrator.create(task)
        job = asyncio.create_task(rt.orchestrator.drive(state))
        background.add(job)  # keep a reference so the task is not garbage-collected mid-run
        job.add_done_callback(background.discard)
        return {"run_id": state.run_id, "status": state.status.value}

    # Runs --------------------------------------------------------------------
    @router.get("/runs")
    async def runs(
        status: str | None = None, workflow: str | None = None, q: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        out = []
        for run in rt.db.load_runs(tenant(), 500):
            summary = run_summary(run)
            if status and summary["status"] != status:
                continue
            if workflow and summary["workflow"] != workflow:
                continue
            if q and q.lower() not in f"{summary['objective']} {summary['run_id']}".lower():
                continue
            out.append(summary)
        return out[: max(1, min(limit, 500))]

    def _load(run_id: str) -> RunState:
        try:
            return rt.db.load_run(tenant(), run_id)
        except RunNotFound as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/runs/{run_id}")
    async def run(run_id: str) -> dict[str, Any]:
        state = _load(run_id)
        return run_detail(state) | {"terminal": state.status in TERMINAL_RUN_STATES}

    @router.get("/runs/{run_id}/timeline")
    async def timeline(run_id: str) -> list[dict[str, Any]]:
        _load(run_id)
        return rt.tracer.timeline(tenant(), run_id)

    # Tools and MCP -----------------------------------------------------------
    @router.get("/tools")
    async def tools() -> dict[str, Any]:
        specs = rt.gateway.all_specs()
        agent_specs = rt.registry.specs()
        servers: dict[str, dict[str, Any]] = {}
        for spec in specs:
            servers.setdefault(spec.source, {"source": spec.source, "tools": 0})["tools"] += 1
        mcp_key = f"mcp:{rt.mcp.name}"
        if mcp_key in servers:
            servers[mcp_key] |= {
                "transport": "in-process",
                "protocol": "Model Context Protocol",
                "prompts": rt.mcp.server_info.get("prompts", []),
                "resource_templates": rt.mcp.server_info.get("resource_templates", []),
            }
        if "local" in servers:
            servers["local"] |= {
                "transport": "in-process",
                "protocol": "direct",
                "prompts": [],
                "resource_templates": [],
            }
        return {
            "servers": list(servers.values()),
            "tools": [
                {
                    "name": s.name,
                    "description": s.description,
                    "risk": s.risk.value,
                    "source": s.source,
                    "side_effect": s.side_effect,
                    "returns_untrusted": s.returns_untrusted,
                    "parameters": sorted((s.schema().input_schema.get("properties") or {}).keys()),
                    "required": s.schema().input_schema.get("required", []),
                    "agents": [a.name for a in agent_specs if rt.gateway.is_allowed(s.name, a.allowed_tools)],
                }
                for s in sorted(specs, key=lambda x: (x.source, x.name))
            ],
        }

    @router.get("/tool-calls")
    async def tool_calls(limit: int = 100) -> list[dict[str, Any]]:
        events = rt.db.list_events_by_kind(tenant(), "tool.call", max(1, min(limit, 500)))
        rejected = rt.db.list_events_by_kind(tenant(), "tool.rejected", 50)
        rows = [
            {
                "ts": e["ts"],
                "run_id": e["run_id"],
                "tool": e["payload"].get("tool"),
                "agent": e["payload"].get("agent"),
                "risk": e["payload"].get("risk"),
                "ok": bool(e["payload"].get("ok")),
                "error": e["payload"].get("error"),
                "duration_ms": e["duration_ms"],
                "outcome": "executed",
            }
            for e in events
        ]
        rows += [
            {
                "ts": e["ts"],
                "run_id": e["run_id"],
                "tool": e["payload"].get("tool"),
                "agent": e["payload"].get("agent"),
                "risk": None,
                "ok": False,
                "error": e["payload"].get("error"),
                "duration_ms": None,
                "outcome": "rejected",
            }
            for e in rejected
        ]
        return sorted(rows, key=lambda r: r["ts"], reverse=True)[:limit]

    # Knowledge ---------------------------------------------------------------
    @router.get("/knowledge/documents")
    async def documents() -> list[dict[str, Any]]:
        return rt.knowledge.list_documents(tenant())

    @router.post("/knowledge/documents", status_code=201)
    async def ingest(body: IngestDocument) -> dict[str, Any]:
        result = rt.knowledge.ingest_text(
            tenant(),
            body.title,
            body.text,
            doc_type=body.doc_type,
            source=f"dashboard:{body.title}",
            trusted=body.trusted,
            tags=body.tags,
        )
        rt.tracer.event(
            tenant(),
            None,
            "knowledge.ingested",
            title=body.title,
            status=result["status"],
            chunks=result["chunks"],
            trusted=body.trusted,
        )
        return result

    @router.delete("/knowledge/documents/{doc_id}")
    async def delete_document(doc_id: str) -> dict[str, Any]:
        if not rt.knowledge.delete_document(tenant(), doc_id):
            raise HTTPException(404, "document not found")
        return {"deleted": doc_id}

    @router.post("/knowledge/search")
    async def search(body: SearchRequest) -> dict[str, Any]:
        hits = rt.knowledge.search(
            tenant(),
            body.query,
            limit=body.limit,
            doc_types=body.doc_types or None,
            trusted_only=body.trusted_only,
            mode=body.mode,
        )
        return {
            "query": body.query,
            "mode": body.mode,
            "embedder": rt.knowledge.embedder.name,
            "results": [h.to_dict() for h in hits],
        }

    # Memory ------------------------------------------------------------------
    @router.get("/memory")
    async def memory(kind: str | None = None, q: str | None = None) -> list[dict[str, Any]]:
        if kind not in (None, "semantic", "episodic"):
            raise HTTPException(422, "kind must be semantic or episodic")
        if q:
            return [
                {
                    "item_id": h.item_id,
                    "kind": h.kind,
                    "text": h.text,
                    "metadata": h.metadata,
                    "score": h.score,
                    "created_at": None,
                }
                for h in rt.memory.search(tenant(), q, kind=kind, limit=50)
            ]
        return [
            {
                "item_id": m["item_id"],
                "kind": m["kind"],
                "text": m["text"],
                "metadata": m["metadata"],
                "score": None,
                "created_at": m["created_at"],
            }
            for m in reversed(rt.db.list_memory(tenant(), kind))
        ]

    @router.delete("/memory/{item_id}")
    async def delete_memory(item_id: str) -> dict[str, Any]:
        if not rt.db.delete_memory(tenant(), item_id):
            raise HTTPException(404, "memory item not found")
        rt.tracer.event(tenant(), None, "memory.deleted", item_id=item_id)
        return {"deleted": item_id}

    # Settings ----------------------------------------------------------------
    @router.get("/settings")
    async def settings() -> dict[str, Any]:
        s, i = rt.settings, rt.integrations
        mocks = {"crm": MockCRM, "email": MockEmail, "calendar": MockCalendar, "billing": MockBilling}
        return {
            "model": {
                "provider": rt.llm.name,
                "model": rt.llm.model,
                "live_model": live_model(),
                "configured_provider": s.llm_provider,
                # Booleans only. Key values never leave the server.
                "openai_key_configured": bool(s.openai_api_key),
                "anthropic_key_configured": bool(s.anthropic_api_key),
                "openai_base_url_configured": bool(s.openai_base_url),
            },
            "limits": {
                "max_steps": s.max_steps,
                "max_replans": s.max_replans,
                "max_run_cost_usd": s.max_run_cost_usd,
                "context_token_budget": s.context_token_budget,
            },
            "integrations": [
                {
                    "name": name,
                    "adapter": type(getattr(i, name)).__name__,
                    "mode": "mock" if isinstance(getattr(i, name), cls) else "real",
                }
                for name, cls in mocks.items()
            ],
            "security": {
                "dashboard_access": "api_key" if s.api_key else "local_only",
                "webhooks_enabled": bool(s.webhook_secret),
                "tenant": tenant(),
                "demo_data_seeded": s.seed_demo_data,
            },
            "environment": {
                "version": __version__,
                "data_dir": str(s.data_dir),
                "log_format": s.log_format,
                "embedder": rt.knowledge.embedder.name,
                "jobs": rt.queue.counts(),
            },
        }

    return router
