"""HTTP API.

Webhook intake:
    POST /webhooks/{source}
    Headers: X-Bizops-Signature: sha256=<hex HMAC of the raw body>
             X-Bizops-Event-Id: <unique id from the sender>   (used for de-duplication)

A webhook is authenticated by its signature, mapped to a task, queued, and
acknowledged with 202. Execution happens in the worker, so the sender gets a
fast response and a retry of the same event id does not start a second run.

Every other endpoint is authenticated. A bearer key identifies a principal with
one tenant and one role (see security/auth.py); the tenant used for a request
always comes from the key. With no key configured at all, the API serves only
requests from the same machine.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..core.errors import RunNotFound
from ..core.models import Task
from ..runtime import Runtime
from ..security import LOCAL_OPERATOR, Permission, Principal, Role
from .dashboard import build_dashboard_router

DASHBOARD_DIR = Path(__file__).resolve().parents[3] / "dashboard"
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

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
        # Pick up runs that were in flight when a previous process stopped.
        recovery = asyncio.create_task(rt.orchestrator.recover_interrupted())
        background.add(recovery)
        recovery.add_done_callback(background.discard)
        worker_task = asyncio.create_task(rt.worker.run_forever()) if run_worker else None
        try:
            yield
        finally:
            if worker_task:
                worker_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker_task

    background: set[asyncio.Task] = set()
    app = FastAPI(title="Autonomous AI Business Operations Agent", version=__version__, lifespan=lifespan)

    def authenticate(request: Request, authorization: str | None = Header(default=None)) -> Principal:
        """Work out who is calling.

        1. A bearer token is always checked first. `bzk_...` tokens are looked up in the key
           store and yield the tenant and role bound to that key. The BIZOPS_API_KEY value is a
           bootstrap administrator for the default tenant.
        2. With no token, and only while no key of any kind has been configured, requests from
           this machine are served as the local operator. This keeps single-user local use
           simple and stays closed to the network. Configuring any key switches it off.
        """
        token = (authorization or "").removeprefix("Bearer ").strip()
        configured = rt.settings.api_key.get_secret_value() if rt.settings.api_key else ""
        if token:
            principal = rt.keys.authenticate(token)
            if principal is None and configured and hmac.compare_digest(configured.encode(), token.encode()):
                principal = Principal("bootstrap-admin", rt.settings.default_tenant, Role.ADMIN, kind="bootstrap")
            if principal is None:
                raise HTTPException(401, "invalid API key")
            return principal
        if configured or rt.keys.any_active():
            raise HTTPException(401, "an API key is required")
        host = request.client.host if request.client else ""
        if host not in LOOPBACK_HOSTS:
            raise HTTPException(403, "access from other machines requires an API key")
        # Loopback-only access is still reachable by a web page open in the same browser. Two checks
        # close that door: the Host header must name this machine (defeats DNS rebinding), and a
        # state-changing request must come from the dashboard's own origin (defeats cross-site requests).
        served_host = request.headers.get("host", "")
        if served_host.rsplit(":", 1)[0].strip("[]") not in LOOPBACK_HOSTS | {"testserver"}:
            raise HTTPException(403, "unexpected Host header for local-only access")
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin.split("://", 1)[-1] != served_host:
            raise HTTPException(403, "cross-origin requests are not accepted")
        return LOCAL_OPERATOR(rt.settings.default_tenant)

    current_principal = Depends(authenticate)

    def require(permission: Permission) -> Any:
        """Dependency factory: authenticate, then insist on one permission."""

        def check(principal: Principal = current_principal) -> Principal:
            if not principal.can(permission):
                rt.audit.record(
                    principal.tenant_id,
                    principal.name,
                    "access.denied",
                    permission.value,
                    actor_role=principal.role.value,
                )
                raise HTTPException(403, f"the '{principal.role.value}' role does not include '{permission.value}'")
            return principal

        return check

    can_read = Depends(require(Permission.READ))
    app.include_router(build_dashboard_router(rt, require, background))

    @app.get("/config.js", include_in_schema=False)
    async def dashboard_config() -> Response:
        """Tells the static dashboard that a live backend is present (the Pages build ships a demo config)."""
        body = "window.BIZOPS_CONFIG = " + json.dumps({"mode": "live", "apiBase": "/api"}) + ";\n"
        return Response(body, media_type="application/javascript", headers={"Cache-Control": "no-store"})

    def webhook_binding(source: str) -> tuple[str, str] | None:
        """(secret, tenant) for a webhook source. A sender is bound to one tenant by its secret."""
        if rt.settings.webhook_secrets:
            try:
                table = json.loads(rt.settings.webhook_secrets.get_secret_value())
                entry = table.get(source) if isinstance(table, dict) else None
            except json.JSONDecodeError:
                entry = None
            if isinstance(entry, dict) and entry.get("secret") and entry.get("tenant"):
                return str(entry["secret"]), str(entry["tenant"])
        if rt.settings.webhook_secret:
            return rt.settings.webhook_secret.get_secret_value(), rt.settings.default_tenant
        return None

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
        binding = webhook_binding(source)
        if binding is None:
            raise HTTPException(503, "webhooks are disabled: set BIZOPS_WEBHOOK_SECRET")
        secret, tenant = binding
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
        # The tenant comes from the secret that signed the request, never from the body. A body that
        # names a different tenant is refused instead of being silently redirected.
        claimed = payload.get("tenant_id")
        if claimed and str(claimed) != tenant:
            rt.audit.record(tenant, f"webhook:{source}", "access.denied", "tenant", claimed_tenant=str(claimed))
            raise HTTPException(403, "this webhook sender is not permitted to act for that tenant")
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

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: str, principal: Principal = can_read) -> dict[str, Any]:
        job = rt.queue.get(job_id)
        if job is None or job["tenant_id"] != principal.tenant_id:
            raise HTTPException(404, "job not found")
        return {k: job[k] for k in ("job_id", "tenant_id", "status", "attempts", "result")}

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str, principal: Principal = can_read) -> dict[str, Any]:
        try:
            state = rt.db.load_run(principal.tenant_id, run_id)
        except RunNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        return {
            "run_id": state.run_id,
            "status": state.status.value,
            "error": state.error,
            "report": state.final_report,
        }

    @app.get("/runs/{run_id}/trace")
    async def get_trace(run_id: str, principal: Principal = can_read) -> list[dict[str, Any]]:
        return rt.tracer.timeline(principal.tenant_id, run_id)

    if DASHBOARD_DIR.exists():
        # Mounted last so that API routes take precedence over static files.
        app.mount("/", StaticFiles(directory=DASHBOARD_DIR, html=True), name="dashboard")
    return app
