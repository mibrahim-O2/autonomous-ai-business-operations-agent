"""Shared fixtures. Every test gets its own database in a temporary directory."""

from __future__ import annotations

import asyncio

import pytest

from bizops.core.config import Settings
from bizops.core.models import Task
from bizops.runtime import Runtime, build_runtime

LEAD = {
    "name": "Noor Abbasi",
    "email": "noor.abbasi@quillfield.example",
    "company": "Quillfield Partners",
    "message": "We have budget approved and would like a demo this week.",
}


def make_settings(tmp_path, **overrides) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path, log_level="WARNING", **overrides)


@pytest.fixture
def rt(tmp_path) -> Runtime:
    runtime = build_runtime(make_settings(tmp_path))
    asyncio.run(runtime.start())  # discover MCP tools and seed the demo tenant
    yield runtime
    runtime.close()


def lead_task(**overrides) -> Task:
    inputs = {**LEAD, **overrides.pop("inputs", {})}
    return Task(objective="Handle a new inbound lead", workflow="lead_intake", inputs=inputs, **overrides)


def run(coro):
    return asyncio.run(coro)


def started(settings) -> Runtime:
    runtime = build_runtime(settings)
    asyncio.run(runtime.start())
    return runtime


def approver(rt: Runtime, role: str = "admin", name: str = "test-approver"):
    from bizops.security import Principal, Role

    return Principal(name=name, tenant_id=rt.settings.default_tenant, role=Role(role), kind="system")


def approve_until_done(rt: Runtime, state, scope: str = "step_tool"):
    """Approve every request a run raises and resume it, until it stops asking. Returns the final state."""
    from bizops.core.models import RunStatus

    for _ in range(20):
        if state.status != RunStatus.WAITING_APPROVAL:
            return state
        rt.approvals.decide(approver(rt), state.pending_approval_id, True, "approved in test", scope=scope)
        state = asyncio.run(rt.orchestrator.resume(state.tenant_id, state.run_id))
    raise AssertionError("run kept asking for approval")


def submit_approved(rt: Runtime, task: Task):
    """Submit a task and approve whatever it asks for."""
    return approve_until_done(rt, asyncio.run(rt.orchestrator.submit(task)))
