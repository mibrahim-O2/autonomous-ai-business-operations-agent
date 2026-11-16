"""SQLite persistence with forward-only migrations.

Design notes:
  * Every table carries tenant_id and every query filters on it. Data isolation
    between tenants is enforced here, not left to callers.
  * Migrations are an ordered list; `PRAGMA user_version` records how many have
    run. Later tasks append migrations instead of editing earlier ones.
  * SQLite is the default because the whole platform then runs from one file.
    The methods below are the only place SQL appears, so moving to PostgreSQL
    means reimplementing this class, not the callers.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core.errors import RunNotFound
from ..core.models import RunState

MIGRATIONS: list[str] = [
    # 1: runs, events, memory, key-value store
    """
    CREATE TABLE runs (
        run_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        status TEXT NOT NULL,
        objective TEXT NOT NULL,
        idempotency_key TEXT,
        control TEXT NOT NULL DEFAULT '',
        state_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX idx_runs_tenant ON runs(tenant_id, created_at);
    CREATE UNIQUE INDEX idx_runs_idem ON runs(tenant_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

    CREATE TABLE events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id TEXT NOT NULL,
        run_id TEXT,
        ts TEXT NOT NULL,
        kind TEXT NOT NULL,
        span_id TEXT,
        parent_span_id TEXT,
        duration_ms REAL,
        payload_json TEXT NOT NULL
    );
    CREATE INDEX idx_events_run ON events(tenant_id, run_id, event_id);

    CREATE TABLE memory_items (
        item_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        text TEXT NOT NULL,
        metadata_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_memory_tenant ON memory_items(tenant_id, kind);

    CREATE TABLE kv (
        tenant_id TEXT NOT NULL,
        namespace TEXT NOT NULL,
        key TEXT NOT NULL,
        value_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (tenant_id, namespace, key)
    );
    """,
    # 2: knowledge base, job queue, idempotency records
    """
    CREATE TABLE documents (
        doc_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        title TEXT NOT NULL,
        doc_type TEXT NOT NULL,
        source TEXT NOT NULL,
        trusted INTEGER NOT NULL DEFAULT 1,
        content_hash TEXT NOT NULL,
        metadata_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_documents_tenant ON documents(tenant_id, doc_type);

    CREATE TABLE chunks (
        chunk_id TEXT PRIMARY KEY,
        doc_id TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
        tenant_id TEXT NOT NULL,
        position INTEGER NOT NULL,
        section TEXT NOT NULL,
        text TEXT NOT NULL,
        embedding BLOB NOT NULL
    );
    CREATE INDEX idx_chunks_tenant ON chunks(tenant_id, doc_id);

    CREATE TABLE jobs (
        job_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL,
        run_at TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        dedupe_key TEXT,
        result_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX idx_jobs_due ON jobs(status, run_at);
    CREATE UNIQUE INDEX idx_jobs_dedupe ON jobs(tenant_id, dedupe_key) WHERE dedupe_key IS NOT NULL;

    CREATE TABLE idempotency (
        tenant_id TEXT NOT NULL,
        key TEXT NOT NULL,
        operation TEXT NOT NULL,
        result_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (tenant_id, key)
    );
    """,
]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Database:
    """Thread-safe wrapper around one SQLite connection."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.migrate(MIGRATIONS)

    # Infrastructure ----------------------------------------------------------
    def migrate(self, migrations: list[str]) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for index in range(version, len(migrations)):
                self._conn.executescript("BEGIN;" + migrations[index] + f"PRAGMA user_version={index + 1};COMMIT;")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # Runs --------------------------------------------------------------------
    def save_run(self, state: RunState) -> None:
        """Checkpoint: write the full run state. Called after every transition."""
        state.updated_at = datetime.now(UTC)
        self.execute(
            """INSERT INTO runs(run_id, tenant_id, status, objective, idempotency_key, state_json,
                                created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(run_id) DO UPDATE SET status=excluded.status, state_json=excluded.state_json,
                                                 updated_at=excluded.updated_at""",
            (
                state.run_id,
                state.tenant_id,
                state.status.value,
                state.task.objective,
                state.task.idempotency_key,
                state.model_dump_json(),
                state.created_at.isoformat(),
                state.updated_at.isoformat(),
            ),
        )

    def load_run(self, tenant_id: str, run_id: str) -> RunState:
        rows = self.query("SELECT state_json FROM runs WHERE tenant_id=? AND run_id=?", (tenant_id, run_id))
        if not rows:
            raise RunNotFound(f"run {run_id} not found for tenant {tenant_id}")
        return RunState.model_validate_json(rows[0]["state_json"])

    def find_run_by_idempotency_key(self, tenant_id: str, key: str) -> RunState | None:
        rows = self.query("SELECT state_json FROM runs WHERE tenant_id=? AND idempotency_key=?", (tenant_id, key))
        return RunState.model_validate_json(rows[0]["state_json"]) if rows else None

    def list_runs(self, tenant_id: str, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT run_id, status, objective, created_at, updated_at FROM runs WHERE tenant_id=?"
        params: list[Any] = [tenant_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in self.query(sql, tuple(params))]

    def set_control(self, tenant_id: str, run_id: str, signal: str) -> None:
        """Record an out-of-band signal ('pause', 'cancel', or '') that the run loop polls."""
        cursor = self.execute("UPDATE runs SET control=? WHERE tenant_id=? AND run_id=?", (signal, tenant_id, run_id))
        if cursor.rowcount == 0:
            raise RunNotFound(f"run {run_id} not found for tenant {tenant_id}")

    def get_control(self, tenant_id: str, run_id: str) -> str:
        rows = self.query("SELECT control FROM runs WHERE tenant_id=? AND run_id=?", (tenant_id, run_id))
        return rows[0]["control"] if rows else ""

    # Events ------------------------------------------------------------------
    def add_event(
        self,
        tenant_id: str,
        run_id: str | None,
        kind: str,
        payload: dict[str, Any],
        *,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        duration_ms: float | None = None,
    ) -> None:
        self.execute(
            "INSERT INTO events(tenant_id, run_id, ts, kind, span_id, parent_span_id, duration_ms, payload_json) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (tenant_id, run_id, _now(), kind, span_id, parent_span_id, duration_ms, json.dumps(payload, default=str)),
        )

    def list_events(self, tenant_id: str, run_id: str) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM events WHERE tenant_id=? AND run_id=? ORDER BY event_id", (tenant_id, run_id))
        return [dict(row) | {"payload": json.loads(row["payload_json"])} for row in rows]

    def list_events_by_kind(self, tenant_id: str, kind: str, limit: int = 100) -> list[dict[str, Any]]:
        """Most recent events of one kind across all of a tenant's runs (newest first)."""
        rows = self.query(
            "SELECT * FROM events WHERE tenant_id=? AND kind=? ORDER BY event_id DESC LIMIT ?", (tenant_id, kind, limit)
        )
        return [dict(row) | {"payload": json.loads(row["payload_json"])} for row in rows]

    def recent_events(self, tenant_id: str, kinds: list[str], limit: int = 30) -> list[dict[str, Any]]:
        marks = ",".join("?" for _ in kinds)
        rows = self.query(
            f"SELECT * FROM events WHERE tenant_id=? AND kind IN ({marks}) ORDER BY event_id DESC LIMIT ?",
            (tenant_id, *kinds, limit),
        )
        return [dict(row) | {"payload": json.loads(row["payload_json"])} for row in rows]

    def load_runs(self, tenant_id: str, limit: int = 200) -> list[RunState]:
        rows = self.query(
            "SELECT state_json FROM runs WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?", (tenant_id, limit)
        )
        return [RunState.model_validate_json(row["state_json"]) for row in rows]

    # Memory ------------------------------------------------------------------
    def delete_memory(self, tenant_id: str, item_id: str) -> bool:
        return (
            self.execute("DELETE FROM memory_items WHERE tenant_id=? AND item_id=?", (tenant_id, item_id)).rowcount > 0
        )

    def add_memory(self, item_id: str, tenant_id: str, kind: str, text: str, metadata: dict[str, Any]) -> None:
        self.execute(
            "INSERT OR REPLACE INTO memory_items(item_id, tenant_id, kind, text, metadata_json, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (item_id, tenant_id, kind, text, json.dumps(metadata, default=str), _now()),
        )

    def list_memory(self, tenant_id: str, kind: str | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM memory_items WHERE tenant_id=?", [tenant_id]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        rows = self.query(sql + " ORDER BY created_at", tuple(params))
        return [dict(row) | {"metadata": json.loads(row["metadata_json"])} for row in rows]

    # Key-value store (backs mock integrations and small registries) -----------
    def kv_put(self, tenant_id: str, namespace: str, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv(tenant_id, namespace, key, value_json, updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(tenant_id, namespace, key) DO UPDATE SET value_json=excluded.value_json, "
            "updated_at=excluded.updated_at",
            (tenant_id, namespace, key, json.dumps(value, default=str), _now()),
        )

    def kv_get(self, tenant_id: str, namespace: str, key: str) -> Any | None:
        rows = self.query(
            "SELECT value_json FROM kv WHERE tenant_id=? AND namespace=? AND key=?", (tenant_id, namespace, key)
        )
        return json.loads(rows[0]["value_json"]) if rows else None

    def kv_list(self, tenant_id: str, namespace: str) -> dict[str, Any]:
        rows = self.query(
            "SELECT key, value_json FROM kv WHERE tenant_id=? AND namespace=? ORDER BY key", (tenant_id, namespace)
        )
        return {row["key"]: json.loads(row["value_json"]) for row in rows}

    def kv_delete(self, tenant_id: str, namespace: str, key: str) -> bool:
        return (
            self.execute(
                "DELETE FROM kv WHERE tenant_id=? AND namespace=? AND key=?", (tenant_id, namespace, key)
            ).rowcount
            > 0
        )

    # Idempotency (side-effecting integration calls) ----------------------------
    def idempotency_get(self, tenant_id: str, key: str) -> Any | None:
        rows = self.query("SELECT result_json FROM idempotency WHERE tenant_id=? AND key=?", (tenant_id, key))
        return json.loads(rows[0]["result_json"]) if rows else None

    def idempotency_put(self, tenant_id: str, key: str, operation: str, result: Any) -> None:
        self.execute(
            "INSERT OR IGNORE INTO idempotency(tenant_id, key, operation, result_json, created_at) VALUES(?,?,?,?,?)",
            (tenant_id, key, operation, json.dumps(result, default=str), _now()),
        )
