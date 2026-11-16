"""Span tracing persisted to the events table.

A span is a timed unit of work (a run, a step, a model call, a tool call). Spans
nest through parent ids, which is what lets a dashboard draw an execution
timeline. Point events (state transitions, decisions) share the same table.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from ..core.models import new_id
from ..storage import Database
from .logging import get_logger

_current_span: ContextVar[str | None] = ContextVar("bizops_current_span", default=None)
log = get_logger("trace")


class Tracer:
    def __init__(self, db: Database):
        self.db = db

    def event(self, tenant_id: str, run_id: str | None, kind: str, **payload: Any) -> None:
        """Record a point-in-time event under the current span."""
        self.db.add_event(tenant_id, run_id, kind, payload, parent_span_id=_current_span.get())
        log.debug(kind, extra={"run_id": run_id, "tenant_id": tenant_id, **payload})

    @contextmanager
    def span(self, tenant_id: str, run_id: str | None, kind: str, **attributes: Any) -> Iterator[dict[str, Any]]:
        """Time a block. The yielded dict can be filled with result attributes."""
        span_id = new_id("span")
        parent = _current_span.get()
        token = _current_span.set(span_id)
        started = time.perf_counter()
        result: dict[str, Any] = {}
        status = "ok"
        try:
            yield result
        except BaseException as exc:
            status = "error"
            result["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            _current_span.reset(token)
            duration = round((time.perf_counter() - started) * 1000, 2)
            self.db.add_event(
                tenant_id,
                run_id,
                kind,
                {**attributes, **result, "status": status},
                span_id=span_id,
                parent_span_id=parent,
                duration_ms=duration,
            )

    def timeline(self, tenant_id: str, run_id: str) -> list[dict[str, Any]]:
        return [
            {
                "ts": e["ts"],
                "kind": e["kind"],
                "span_id": e["span_id"],
                "parent_span_id": e["parent_span_id"],
                "duration_ms": e["duration_ms"],
                **e["payload"],
            }
            for e in self.db.list_events(tenant_id, run_id)
        ]
