"""HTTP API.

Webhook intake:
    POST /webhooks/{source}
    Headers: X-Bizops-Signature: sha256=<hex HMAC of the raw body>
             X-Bizops-Event-Id: <unique id from the sender>   (used for de-duplication)

A webhook is authenticated by its signature, mapped to a task, queued, and
acknowledged with 202. Execution happens in the worker, so the sender gets a
fast response and a retry of the same event id does not start a second run.

Run inspection endpoints require the API key from BIZOPS_API_KEY as a bearer
token. If no key is configured they are disabled, not open.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from ..core.errors import RunNotFound
from ..core.models import Task
from ..runtime import Runtime

# Which workflow each webhook event starts, and how its payload maps to task inputs.
EVENT_ROUTES: dict[str, dict[str, Any]] = {
    "lead.created": {
        "workflow": "lead_intake",
        "objective": "Handle a new inbound lead",
        "fields": ["name", "email", "company", "message"],
    },
    "invoices.review_requested": {
        "workflow": "invoice_collections",
        "objective": "Review overdue invoices and send payment reminders",
        "fields": [],
    },
    "email.received": {"workflow": "inbox_triage", "objective": "Triage the unread inbox", "fields": []},
}


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Constant-time check of an HMAC-SHA256 signature in the form 'sha256=<hex>'."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # Compare bytes: compare_digest rejects non-ASCII strings, and the header is attacker-controlled.
    return hmac.compare_digest(expected.encode(), header.removeprefix("sha256=").encode())


def create_app(rt: Runtime, *, run_worker: bool = True) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        await rt.start()
        worker_task = asyncio.create_task(rt.worker.run_forever()) if run_worker else None
        try:
            yield
        finally:
            if worker_task:
                worker_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker_task

    app = FastAPI(title="Autonomous AI Business Operations Agent", version="0.2.0", lifespan=lifespan)

    def require_api_key(authorization: str | None = Header(default=None)) -> None:
        configured = rt.settings.api_key.get_secret_value() if rt.settings.api_key else ""
        if not configured:
            raise HTTPException(503, "API access is disabled: set BIZOPS_API_KEY")
        supplied = (authorization or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(configured.encode(), supplied.encode()):
            raise HTTPException(401, "invalid or missing API key")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "tools": len(rt.gateway.all_specs()), "jobs": rt.queue.counts()}

    @app.post("/webhooks/{source}", status_code=202)
    async def webhook(
        source: str,
        request: Request,
        x_bizops_signature: str | None = Header(default=None),
        x_bizops_event_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        body = await request.body()
        secret = rt.settings.webhook_secret.get_secret_value() if rt.settings.webhook_secret else ""
        if not secret:
            raise HTTPException(503, "webhooks are disabled: set BIZOPS_WEBHOOK_SECRET")
        if not verify_signature(secret, body, x_bizops_signature):
            raise HTTPException(401, "invalid signature")
        if not x_bizops_event_id:
            raise HTTPException(400, "X-Bizops-Event-Id header is required")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "body is not valid JSON") from exc
        route = EVENT_ROUTES.get(str(payload.get("event")))
        if route is None:
            raise HTTPException(422, f"unsupported event '{payload.get('event')}'")
        data = payload.get("data") or {}
        tenant = str(payload.get("tenant_id") or rt.settings.default_tenant)
        dedupe = f"webhook:{source}:{x_bizops_event_id}"
        task = Task(
            tenant_id=tenant,
            objective=route["objective"],
            workflow=route["workflow"],
            inputs={field: data.get(field, "") for field in route["fields"]},
            source="webhook",
            requested_by=source,
            idempotency_key=dedupe,
        )
        job_id, created = rt.queue.enqueue_task(task, dedupe_key=dedupe)
        rt.tracer.event(
            tenant,
            None,
            "webhook.accepted" if created else "webhook.duplicate",
            source=source,
            webhook_event=payload.get("event"),
            job_id=job_id,
        )
        return {"job_id": job_id, "status": "queued" if created else "duplicate"}

    @app.get("/jobs/{job_id}", dependencies=[Depends(require_api_key)])
    async def get_job(job_id: str) -> dict[str, Any]:
        job = rt.queue.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return {k: job[k] for k in ("job_id", "tenant_id", "status", "attempts", "result")}

    @app.get("/runs/{run_id}", dependencies=[Depends(require_api_key)])
    async def get_run(run_id: str, tenant_id: str | None = None) -> dict[str, Any]:
        try:
            state = rt.db.load_run(tenant_id or rt.settings.default_tenant, run_id)
        except RunNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        return {
            "run_id": state.run_id,
            "status": state.status.value,
            "error": state.error,
            "report": state.final_report,
        }

    @app.get("/runs/{run_id}/trace", dependencies=[Depends(require_api_key)])
    async def get_trace(run_id: str, tenant_id: str | None = None) -> list[dict[str, Any]]:
        return rt.tracer.timeline(tenant_id or rt.settings.default_tenant, run_id)

    return app
