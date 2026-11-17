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
from ..approvals import DEFAULT_POLICY, ApprovalPolicy, load_policy, save_policy
from ..core.errors import Conflict, PermissionDenied, RunNotFound
from ..core.models import TERMINAL_RUN_STATES, RunState, RunStatus, Task
from ..demo import SCENARIOS
from ..integrations.mock import MockBilling, MockCalendar, MockCRM, MockEmail
from ..llm.scripted import ScriptedLLM
from ..runtime import Runtime
from ..security import Permission, Principal, Role, permissions_for

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


class Decision(BaseModel):
    approve: bool
    comment: str = Field(default="", max_length=1000)
    scope: str = Field(default="call", pattern="^(call|step_tool)$")


class NewKey(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    role: Role


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
        "generation": state.generation,
        "injection_findings": state.injection_findings,
        "untrusted_refs": state.untrusted_refs,
    }


def build_dashboard_router(rt: Runtime, require: Any, background: set[asyncio.Task]) -> APIRouter:
    """`require(permission)` returns a dependency that authenticates the caller and checks the permission.

    The tenant always comes from the authenticated principal. No endpoint accepts a tenant parameter.
    """
    router = APIRouter(prefix="/api")
    can_read = Depends(require(Permission.READ))
    can_run_start = Depends(require(Permission.RUN_START))
    can_run_control = Depends(require(Permission.RUN_CONTROL))
    can_knowledge_write = Depends(require(Permission.KNOWLEDGE_WRITE))
    can_memory_write = Depends(require(Permission.MEMORY_WRITE))
    can_approval_decide = Depends(require(Permission.APPROVAL_DECIDE))
    can_agent_manage = Depends(require(Permission.AGENT_MANAGE))
    can_policy_write = Depends(require(Permission.POLICY_WRITE))
    can_keys_manage = Depends(require(Permission.KEYS_MANAGE))

    def audit(principal: Principal, action: str, target: str = "", **detail: Any) -> None:
        rt.audit.record(principal.tenant_id, principal.name, action, target, actor_role=principal.role.value, **detail)

    def in_background(coro: Any) -> None:
        job = asyncio.create_task(coro)
        background.add(job)  # keep a reference so the task is not garbage-collected mid-run
        job.add_done_callback(background.discard)

    def live_model() -> bool:
        return not isinstance(rt.llm, ScriptedLLM)

    @router.get("/meta")
    async def meta(principal: Principal = can_read) -> dict[str, Any]:
        return {
            "mode": "live",
            "version": __version__,
            "tenant": principal.tenant_id,
            "llm": {"provider": rt.llm.name, "model": rt.llm.model, "live_model": live_model()},
            "auth": "local_only" if principal.kind == "local" else "api_key",
            "principal": principal.describe(),
            "pending_approvals": rt.approvals.counts(principal.tenant_id).get("pending", 0),
        }

    @router.get("/overview")
    async def overview(principal: Principal = can_read) -> dict[str, Any]:
        runs = rt.db.load_runs(principal.tenant_id, 500)
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
            for e in rt.db.recent_events(principal.tenant_id, ACTIVITY_KINDS, 15)
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
    async def agents(principal: Principal = can_read) -> list[dict[str, Any]]:
        all_tools = rt.gateway.all_specs()
        recent = Counter(
            e["payload"].get("agent") for e in rt.db.list_events_by_kind(principal.tenant_id, "step.execute", 500)
        )
        out = []
        for spec in rt.registry.specs():
            tools = [t for t in all_tools if rt.gateway.is_allowed(t.name, spec.allowed_tools)]
            out.append(
                {
                    "name": spec.name,
                    "role": spec.role,
                    "enabled": rt.registry.is_enabled(rt.db, principal.tenant_id, spec.name),
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
    async def set_agent_enabled(
        name: str, body: AgentToggle, principal: Principal = can_agent_manage
    ) -> dict[str, Any]:
        try:
            rt.registry.set_enabled(rt.db, principal.tenant_id, name, body.enabled)
        except KeyError as exc:
            raise HTTPException(404, f"no agent named '{name}'") from exc
        rt.tracer.event(principal.tenant_id, None, "agent.enabled_changed", agent=name, enabled=body.enabled)
        audit(principal, "agent.enabled" if body.enabled else "agent.disabled", name)
        return {"name": name, "enabled": rt.registry.is_enabled(rt.db, principal.tenant_id, name)}

    # Workflows ---------------------------------------------------------------
    @router.get("/workflows")
    async def workflows(principal: Principal = can_read) -> list[dict[str, Any]]:
        runs = rt.db.load_runs(principal.tenant_id, 500)
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
    async def start_workflow(name: str, body: StartWorkflow, principal: Principal = can_run_start) -> dict[str, Any]:
        """Create a run and execute it in the background. The dashboard polls the run for progress."""
        if rt.workflows.get(name) is None:
            raise HTTPException(404, f"no workflow named '{name}'")
        scenario = SCENARIOS.get(name, {})
        objective = body.objective or scenario.get("objective") or rt.workflows.get(name).description
        inputs = body.inputs if body.inputs is not None else scenario.get("inputs", {})
        task = Task(
            tenant_id=principal.tenant_id,
            objective=objective,
            workflow=name,
            inputs=inputs,
            source="api",
            requested_by=principal.name,
        )
        state, _ = rt.orchestrator.create(task)
        audit(principal, "run.started", state.run_id, workflow=name)
        in_background(rt.orchestrator.drive(state.tenant_id, state.run_id))
        return {"run_id": state.run_id, "status": state.status.value}

    # Runs --------------------------------------------------------------------
    @router.get("/runs")
    async def runs(
        status: str | None = None,
        workflow: str | None = None,
        q: str | None = None,
        limit: int = 100,
        principal: Principal = can_read,
    ) -> list[dict[str, Any]]:
        out = []
        for run in rt.db.load_runs(principal.tenant_id, 500):
            summary = run_summary(run)
            if status and summary["status"] != status:
                continue
            if workflow and summary["workflow"] != workflow:
                continue
            if q and q.lower() not in f"{summary['objective']} {summary['run_id']}".lower():
                continue
            out.append(summary)
        return out[: max(1, min(limit, 500))]

    def _load(tenant_id: str, run_id: str) -> RunState:
        try:
            return rt.db.load_run(tenant_id, run_id)
        except RunNotFound as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/runs/{run_id}")
    async def run(run_id: str, principal: Principal = can_read) -> dict[str, Any]:
        state = _load(principal.tenant_id, run_id)
        lease = rt.db.get_lease(principal.tenant_id, run_id)
        return run_detail(state) | {
            "terminal": state.status in TERMINAL_RUN_STATES,
            "approvals": rt.approvals.list(principal.tenant_id, run_id=run_id),
            "controls": run_controls(state, principal),
            "being_executed": lease["lease_owner"] is not None,
        }

    def run_controls(state: RunState, principal: Principal) -> dict[str, bool]:
        """Which control actions make sense for this run and this caller right now."""
        allowed = principal.can(Permission.RUN_CONTROL)
        waiting_on_person = state.status == RunStatus.WAITING_APPROVAL and rt.approvals.is_pending(
            principal.tenant_id, state.pending_approval_id
        )
        return {
            "pause": allowed and state.status in (RunStatus.RUNNING, RunStatus.PENDING),
            "resume": allowed
            and state.status in (RunStatus.PAUSED, RunStatus.WAITING_APPROVAL, RunStatus.RUNNING, RunStatus.PENDING)
            and not waiting_on_person,
            "cancel": allowed and state.status not in TERMINAL_RUN_STATES,
        }

    @router.post("/runs/{run_id}/pause")
    async def pause_run(run_id: str, principal: Principal = can_run_control) -> dict[str, Any]:
        """Ask a run to stop at its next step boundary. The response reports the state as it is now."""
        state = _load(principal.tenant_id, run_id)
        if state.status in TERMINAL_RUN_STATES:
            raise HTTPException(409, f"run is already {state.status.value}")
        rt.orchestrator.request_pause(principal.tenant_id, run_id)
        audit(principal, "run.pause_requested", run_id)
        return {"run_id": run_id, "status": state.status.value, "pause_requested": True}

    @router.post("/runs/{run_id}/resume", status_code=202)
    async def resume_run(run_id: str, principal: Principal = can_run_control) -> dict[str, Any]:
        state = _load(principal.tenant_id, run_id)
        if state.status in TERMINAL_RUN_STATES:
            raise HTTPException(409, f"run is already {state.status.value}")
        if not run_controls(state, principal)["resume"]:
            raise HTTPException(409, "run is waiting for an approval decision and cannot be resumed until it is made")
        audit(principal, "run.resume_requested", run_id)
        in_background(rt.orchestrator.resume(principal.tenant_id, run_id))
        return {"run_id": run_id, "status": state.status.value, "resume_requested": True}

    @router.post("/runs/{run_id}/cancel")
    async def cancel_run(run_id: str, principal: Principal = can_run_control) -> dict[str, Any]:
        state = _load(principal.tenant_id, run_id)
        if state.status in TERMINAL_RUN_STATES:
            raise HTTPException(409, f"run is already {state.status.value}")
        state = rt.orchestrator.cancel(principal.tenant_id, run_id)
        audit(principal, "run.cancel_requested", run_id, status_after=state.status.value)
        # A run that a worker is executing stops at its next step boundary; report what is true now.
        return {
            "run_id": run_id,
            "status": state.status.value,
            "cancelled": state.status == RunStatus.CANCELLED,
            "cancel_requested": True,
        }

    @router.get("/runs/{run_id}/timeline")
    async def timeline(run_id: str, principal: Principal = can_read) -> list[dict[str, Any]]:
        _load(principal.tenant_id, run_id)
        return rt.tracer.timeline(principal.tenant_id, run_id)

    # Tools and MCP -----------------------------------------------------------
    @router.get("/tools")
    async def tools(principal: Principal = can_read) -> dict[str, Any]:
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
    async def tool_calls(limit: int = 100, principal: Principal = can_read) -> list[dict[str, Any]]:
        events = rt.db.list_events_by_kind(principal.tenant_id, "tool.call", max(1, min(limit, 500)))
        rejected = rt.db.list_events_by_kind(principal.tenant_id, "tool.rejected", 50)
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
    async def documents(principal: Principal = can_read) -> list[dict[str, Any]]:
        return rt.knowledge.list_documents(principal.tenant_id)

    @router.post("/knowledge/documents", status_code=201)
    async def ingest(body: IngestDocument, principal: Principal = can_knowledge_write) -> dict[str, Any]:
        result = rt.knowledge.ingest_text(
            principal.tenant_id,
            body.title,
            body.text,
            doc_type=body.doc_type,
            source=f"dashboard:{body.title}",
            trusted=body.trusted,
            tags=body.tags,
        )
        rt.tracer.event(
            principal.tenant_id,
            None,
            "knowledge.ingested",
            title=body.title,
            status=result["status"],
            chunks=result["chunks"],
            trusted=body.trusted,
        )
        audit(principal, "knowledge.ingested", body.title, trusted=body.trusted, status=result["status"])
        return result

    @router.delete("/knowledge/documents/{doc_id}")
    async def delete_document(doc_id: str, principal: Principal = can_knowledge_write) -> dict[str, Any]:
        if not rt.knowledge.delete_document(principal.tenant_id, doc_id):
            raise HTTPException(404, "document not found")
        audit(principal, "knowledge.deleted", doc_id)
        return {"deleted": doc_id}

    @router.post("/knowledge/search")
    async def search(body: SearchRequest, principal: Principal = can_read) -> dict[str, Any]:
        hits = rt.knowledge.search(
            principal.tenant_id,
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
    async def memory(
        kind: str | None = None, q: str | None = None, principal: Principal = can_read
    ) -> list[dict[str, Any]]:
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
                for h in rt.memory.search(principal.tenant_id, q, kind=kind, limit=50)
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
            for m in reversed(rt.db.list_memory(principal.tenant_id, kind))
        ]

    @router.delete("/memory/{item_id}")
    async def delete_memory(item_id: str, principal: Principal = can_memory_write) -> dict[str, Any]:
        if not rt.db.delete_memory(principal.tenant_id, item_id):
            raise HTTPException(404, "memory item not found")
        rt.tracer.event(principal.tenant_id, None, "memory.deleted", item_id=item_id)
        audit(principal, "memory.deleted", item_id)
        return {"deleted": item_id}

    # Settings ----------------------------------------------------------------
    @router.get("/settings")
    async def settings(principal: Principal = can_read) -> dict[str, Any]:
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
                "dashboard_access": "local_only" if principal.kind == "local" else "api_key",
                "webhooks_enabled": bool(s.webhook_secret or s.webhook_secrets),
                "tenant": principal.tenant_id,
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

    # Approvals ---------------------------------------------------------------
    def approval_view(record: dict[str, Any], principal: Principal) -> dict[str, Any]:
        """An approval request with the context a person needs to decide it."""
        view = dict(record)
        view["can_decide"] = (
            record["status"] == "pending"
            and principal.can(Permission.APPROVAL_DECIDE)
            and principal.role.at_least(Role(record["required_role"]))
        )
        try:
            state = rt.db.load_run(principal.tenant_id, record["run_id"])
        except RunNotFound:
            view["run"] = None
            return view
        step = next((s for s in (state.plan.steps if state.plan else []) if s.step_id == record["step_id"]), None)
        view["run"] = {
            "run_id": state.run_id,
            "status": state.status.value,
            "objective": state.task.objective,
            "workflow": state.analysis.get("workflow") or state.task.workflow,
            "requested_by": state.task.requested_by,
            "source": state.task.source,
            "tainted": state.tainted,
            "injection_findings": state.injection_findings,
            "step_description": step.description if step else "",
            "step_index": step.index if step else None,
            "steps_total": len(state.plan.steps) if state.plan else 0,
            "earlier_calls_in_step": [{"tool": t.tool, "ok": t.ok} for t in (step.tool_calls if step else [])],
        }
        spec = rt.gateway.spec(record["tool"])
        view["tool_description"] = spec.description if spec else ""
        return view

    @router.get("/approvals")
    async def approvals(
        status: str | None = None, run_id: str | None = None, limit: int = 100, principal: Principal = can_read
    ) -> dict[str, Any]:
        if status not in (None, "pending", "approved", "rejected", "expired", "cancelled"):
            raise HTTPException(422, "unknown status")
        records = rt.approvals.list(principal.tenant_id, status=status, run_id=run_id, limit=max(1, min(limit, 500)))
        return {
            "counts": rt.approvals.counts(principal.tenant_id),
            "items": [approval_view(r, principal) for r in records],
        }

    @router.get("/approvals/{approval_id}")
    async def approval(approval_id: str, principal: Principal = can_read) -> dict[str, Any]:
        record = rt.approvals.get(principal.tenant_id, approval_id)
        if record is None:
            raise HTTPException(404, "approval request not found")
        return approval_view(record, principal)

    @router.post("/approvals/{approval_id}/decision")
    async def decide(approval_id: str, body: Decision, principal: Principal = can_approval_decide) -> dict[str, Any]:
        """Record the decision, then continue the run. The response is the stored record, so the
        dashboard shows a decision only once the backend has accepted it."""
        if rt.approvals.get(principal.tenant_id, approval_id) is None:
            raise HTTPException(404, "approval request not found")
        try:
            record = rt.approvals.decide(principal, approval_id, body.approve, body.comment, body.scope)
        except PermissionDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        rt.tracer.event(
            principal.tenant_id,
            record["run_id"],
            "approval.decided",
            approval_id=approval_id,
            decision=record["status"],
            by=principal.name,
            tool=record["tool"],
        )
        # Approved: the call executes now. Rejected: the agent is told and adapts or the run stops.
        in_background(rt.orchestrator.resume(principal.tenant_id, record["run_id"]))
        return approval_view(record, principal) | {"run_resume_requested": True}

    # Policy ------------------------------------------------------------------
    @router.get("/policy")
    async def get_policy(principal: Principal = can_read) -> dict[str, Any]:
        policy = load_policy(rt.db, principal.tenant_id)
        return {
            "policy": policy.model_dump(mode="json"),
            "is_default": policy.model_dump(exclude={"version"}) == DEFAULT_POLICY.model_dump(exclude={"version"}),
            "editable": principal.can(Permission.POLICY_WRITE),
            "tools": [
                {
                    "name": spec.name,
                    "risk": spec.risk.value,
                    # What the policy decides for this tool on a clean run and on a tainted one,
                    # ignoring amount conditions (shown with arguments left empty).
                    "clean_run": policy.evaluate(spec.name, spec.risk, False, {})[0] if spec.side_effect else "allow",
                    "tainted_run": policy.evaluate(spec.name, spec.risk, True, {})[0] if spec.side_effect else "allow",
                }
                for spec in rt.gateway.all_specs()
            ],
        }

    @router.put("/policy")
    async def put_policy(body: ApprovalPolicy, principal: Principal = can_policy_write) -> dict[str, Any]:
        before = load_policy(rt.db, principal.tenant_id)
        saved = save_policy(rt.db, principal.tenant_id, body)
        audit(
            principal,
            "policy.changed",
            "approval",
            version=saved.version,
            rules_before=[r.name for r in before.rules],
            rules_after=[r.name for r in saved.rules],
            default_decision=saved.default_decision,
        )
        return {"policy": saved.model_dump(mode="json")}

    @router.post("/policy/reset")
    async def reset_policy(principal: Principal = can_policy_write) -> dict[str, Any]:
        saved = save_policy(rt.db, principal.tenant_id, DEFAULT_POLICY)
        audit(principal, "policy.reset", "approval", version=saved.version)
        return {"policy": saved.model_dump(mode="json")}

    # Audit -------------------------------------------------------------------
    @router.get("/audit")
    async def audit_log(
        limit: int = 100, action: str | None = None, target: str | None = None, principal: Principal = can_read
    ) -> list[dict[str, Any]]:
        return rt.audit.list(principal.tenant_id, max(1, min(limit, 500)), action_prefix=action, target=target)

    @router.get("/audit/verify")
    async def audit_verify(principal: Principal = can_read) -> dict[str, Any]:
        return rt.audit.verify(principal.tenant_id)

    # Access ------------------------------------------------------------------
    @router.get("/me")
    async def me(principal: Principal = can_read) -> dict[str, Any]:
        return principal.describe()

    @router.get("/keys")
    async def list_keys(principal: Principal = can_keys_manage) -> list[dict[str, Any]]:
        return rt.keys.list(principal.tenant_id)

    @router.post("/keys", status_code=201)
    async def create_key(body: NewKey, principal: Principal = can_keys_manage) -> dict[str, Any]:
        """Create a key for the caller's tenant. The token is in this response only and cannot be shown again."""
        key_id, token = rt.keys.create(principal.tenant_id, body.name, body.role, created_by=principal.name)
        audit(principal, "key.created", key_id, name=body.name, role=body.role.value)
        return {"key_id": key_id, "name": body.name, "role": body.role.value, "token": token}

    @router.delete("/keys/{key_id}")
    async def revoke_key(key_id: str, principal: Principal = can_keys_manage) -> dict[str, Any]:
        if not rt.keys.revoke(principal.tenant_id, key_id):
            raise HTTPException(404, "key not found or already revoked")
        audit(principal, "key.revoked", key_id)
        return {"revoked": key_id}

    @router.get("/security")
    async def security(principal: Principal = can_read) -> dict[str, Any]:
        """Security and reliability posture, for the dashboard. Contains no secret values."""
        s = rt.settings
        runs = rt.db.load_runs(principal.tenant_id, 500)
        findings = [
            {"run_id": r.run_id, "objective": r.task.objective, **f} for r in runs for f in r.injection_findings
        ]
        retries = rt.db.list_events_by_kind(principal.tenant_id, "step.retry_scheduled", 500)
        return {
            "principal": principal.describe(),
            "roles": {role.value: permissions_for(role) for role in Role},
            "access": {
                "mode": "local_only" if principal.kind == "local" else "api_key",
                "bootstrap_key_configured": bool(s.api_key),
                "active_keys": sum(1 for k in rt.keys.list(principal.tenant_id) if not k["revoked_at"]),
                "webhooks_enabled": bool(s.webhook_secret or s.webhook_secrets),
            },
            "approvals": rt.approvals.counts(principal.tenant_id),
            "audit": rt.audit.verify(principal.tenant_id),
            "untrusted_content": {
                "tainted_runs": sum(1 for r in runs if r.tainted),
                "runs_with_suspected_injection": len({f["run_id"] for f in findings}),
                "findings": findings[:25],
            },
            "reliability": {
                "step_timeout_seconds": s.step_timeout_seconds,
                "tool_timeout_seconds": s.tool_timeout_seconds,
                "retry_base_seconds": s.retry_base_seconds,
                "retry_max_seconds": s.retry_max_seconds,
                "run_lease_seconds": s.run_lease_seconds,
                "approval_ttl_seconds": s.approval_ttl_seconds,
                "llm_max_retries": s.llm_max_retries,
                "step_retries_recorded": len(retries),
                "stale_workers_stopped": len(
                    rt.db.list_events_by_kind(principal.tenant_id, "run.stale_worker_stopped", 500)
                ),
                "runs_recovered": len(rt.db.list_events_by_kind(principal.tenant_id, "run.recovery_started", 500)),
                "interrupted_runs_now": len(rt.db.list_interrupted_runs(tenant_id=principal.tenant_id)),
                "model_retry_stats": getattr(rt.llm, "stats", None),
            },
        }

    return router
