"""Shared fixtures. Every test gets its own database in a temporary directory."""

from __future__ import annotations

import asyncio

import pytest

from bizops.core.config import Settings
from bizops.core.models import Task
from bizops.runtime import Runtime, build_runtime

LEAD = {
    "name": "Amelia Hart",
    "email": "amelia.hart@harborline.example",
    "company": "Harborline Realty",
    "message": "We have budget approved and would like a demo this week.",
}


def make_settings(tmp_path, **overrides) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path, log_level="WARNING", **overrides)


@pytest.fixture
def rt(tmp_path) -> Runtime:
    runtime = build_runtime(make_settings(tmp_path))
    yield runtime
    runtime.close()


def lead_task(**overrides) -> Task:
    inputs = {**LEAD, **overrides.pop("inputs", {})}
    return Task(objective="Handle a new inbound lead", workflow="lead_intake", inputs=inputs, **overrides)


def run(coro):
    return asyncio.run(coro)
