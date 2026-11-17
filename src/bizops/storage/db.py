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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..core.errors import RunNotFound, StaleLease
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
    # 3: run leases with fencing, and fencing high-water marks for target systems
    """
    ALTER TABLE runs ADD COLUMN lease_owner TEXT;
    ALTER TABLE runs ADD COLUMN lease_expires_at TEXT;
    ALTER TABLE runs ADD COLUMN generation INTEGER NOT NULL DEFAULT 0;

    CREATE TABLE fence_marks (
        tenant_id TEXT NOT NULL,
        scope TEXT NOT NULL,
        token INTEGER NOT NULL,
        PRIMARY KEY (tenant_id, scope)
    );
    """,
    # 4: human approvals, tenant- and role-bound API keys, tamper-evident audit log
    """
    CREATE TABLE approvals (
        approval_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        step_id TEXT NOT NULL,
        step_action TEXT NOT NULL,
        agent TEXT NOT NULL,
        tool TEXT NOT NULL,
        arguments_json TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        risk TEXT NOT NULL,
        rule TEXT NOT NULL,
        reason TEXT NOT NULL,
        signals_json TEXT NOT NULL DEFAULT '[]',
        required_role TEXT NOT NULL,
        status TEXT NOT NULL,
        scope TEXT NOT NULL DEFAULT 'call',
        requested_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        decided_at TEXT,
        decided_by TEXT,
        decided_role TEXT,
        comment TEXT
    );
    CREATE INDEX idx_approvals_tenant ON approvals(tenant_id, status, requested_at);
    CREATE INDEX idx_approvals_run ON approvals(tenant_id, run_id, step_id);

    CREATE TABLE api_keys (
        key_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        name TEXT NOT NULL,
        role TEXT NOT NULL,
        secret_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        created_by TEXT NOT NULL,
        last_used_at TEXT,
        revoked_at TEXT
    );

    CREATE TABLE audit_log (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id TEXT NOT NULL,
        at TEXT NOT NULL,
        actor TEXT NOT NULL,
        actor_role TEXT NOT NULL,
        action TEXT NOT NULL,
        target TEXT NOT NULL,
        detail_json TEXT NOT NULL,
        prev_hash TEXT NOT NULL,
        hash TEXT NOT NULL
    );
    CREATE INDEX idx_audit_tenant ON audit_log(tenant_id, seq);
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
    def save_run(self, state: RunState, fence: tuple[str, int] | None = None, lease_seconds: float = 60.0) -> None:
        """Checkpoint: write the full run state. Called after every transition.

        With `fence` (owner, generation) the write is conditional: it succeeds only
        while that worker still holds the run under that generation, and it renews
        the lease in the same statement. A worker whose claim was superseded gets
        `StaleLease` and nothing is written. Without `fence` the row is inserted
        (run creation) and an existing row is left untouched.
        """
        state.updated_at = datetime.now(UTC)
        if fence is None:
            self.execute(
                """INSERT INTO runs(run_id, tenant_id, status, objective, idempotency_key, state_json,
                                    created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
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
            return
        owner, generation = fence
        expires = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat(timespec="milliseconds")
        cursor = self.execute(
            """UPDATE runs SET status=?, state_json=?, updated_at=?, lease_expires_at=?
               WHERE tenant_id=? AND run_id=? AND lease_owner=? AND generation=?""",
            (
                state.status.value,
                state.model_dump_json(),
                state.updated_at.isoformat(),
                expires,
                state.tenant_id,
                state.run_id,
                owner,
                generation,
            ),
        )
        if cursor.rowcount == 0:
            raise StaleLease(f"worker {owner} (generation {generation}) no longer owns run {state.run_id}")

    # Leases and fencing ------------------------------------------------------
    def claim_run(
        self, tenant_id: str, run_id: str, owner: str, lease_seconds: float, now: datetime | None = None
    ) -> int | None:
        """Atomically take ownership of a run. Returns the new generation, or None if it is held.

        One UPDATE inside an immediate transaction both tests "unowned or expired"
        and writes the new owner, so two workers cannot both succeed. The generation
        is incremented on every successful claim and serves as the fencing token.
        """
        moment = now or datetime.now(UTC)
        expires = (moment + timedelta(seconds=lease_seconds)).isoformat(timespec="milliseconds")
        with self.transaction() as conn:
            claimed = conn.execute(
                """UPDATE runs SET lease_owner=?, lease_expires_at=?, generation=generation+1
                   WHERE tenant_id=? AND run_id=? AND (lease_owner IS NULL OR lease_expires_at<?)""",
                (owner, expires, tenant_id, run_id, moment.isoformat(timespec="milliseconds")),
            ).rowcount
            if not claimed:
                exists = conn.execute(
                    "SELECT 1 FROM runs WHERE tenant_id=? AND run_id=?", (tenant_id, run_id)
                ).fetchone()
                if exists is None:
                    raise RunNotFound(f"run {run_id} not found for tenant {tenant_id}")
                return None
            row = conn.execute("SELECT generation FROM runs WHERE tenant_id=? AND run_id=?", (tenant_id, run_id))
            return int(row.fetchone()["generation"])

    def renew_lease(self, tenant_id: str, run_id: str, owner: str, generation: int, lease_seconds: float) -> bool:
        """Heartbeat. Returns False when the lease has been lost."""
        expires = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat(timespec="milliseconds")
        return (
            self.execute(
                "UPDATE runs SET lease_expires_at=? WHERE tenant_id=? AND run_id=? AND lease_owner=? AND generation=?",
                (expires, tenant_id, run_id, owner, generation),
            ).rowcount
            > 0
        )

    def release_lease(self, tenant_id: str, run_id: str, owner: str, generation: int) -> None:
        """Give the run up. A no-op if this worker no longer owns it. The generation is kept."""
        self.execute(
            "UPDATE runs SET lease_owner=NULL, lease_expires_at=NULL "
            "WHERE tenant_id=? AND run_id=? AND lease_owner=? AND generation=?",
            (tenant_id, run_id, owner, generation),
        )

    def lease_is_current(self, tenant_id: str, run_id: str, owner: str, generation: int) -> bool:
        rows = self.query(
            "SELECT 1 FROM runs WHERE tenant_id=? AND run_id=? AND lease_owner=? AND generation=?",
            (tenant_id, run_id, owner, generation),
        )
        return bool(rows)

    def get_lease(self, tenant_id: str, run_id: str) -> dict[str, Any]:
        rows = self.query(
            "SELECT lease_owner, lease_expires_at, generation FROM runs WHERE tenant_id=? AND run_id=?",
            (tenant_id, run_id),
        )
        if not rows:
            raise RunNotFound(f"run {run_id} not found for tenant {tenant_id}")
        return dict(rows[0])

    def list_interrupted_runs(self, now: datetime | None = None) -> list[tuple[str, str]]:
        """Runs marked running that no live worker holds: (tenant_id, run_id). Their worker died or stalled."""
        moment = (now or datetime.now(UTC)).isoformat(timespec="milliseconds")
        rows = self.query(
            "SELECT tenant_id, run_id FROM runs WHERE status='running' "
            "AND (lease_owner IS NULL OR lease_expires_at<?) ORDER BY updated_at",
            (moment,),
        )
        return [(row["tenant_id"], row["run_id"]) for row in rows]

    def run_once(
        self, tenant_id: str, key: str, operation: str, action: Any, *, fence_scope: str = "", fence_token: int = 0
    ) -> tuple[Any, bool]:
        """Perform a side effect at most once per idempotency key, with fencing. Returns (result, replayed).

        Everything happens in one transaction:
          1. Fencing: the highest token seen for the scope is kept. A caller with a
             lower token is a superseded worker and is refused with `StaleLease`.
          2. Idempotency: if the key already has a result, that result is returned
             and the action does not run.
          3. Otherwise the action runs and its result is stored under the key.
        If the action fails, or the process dies part-way, the transaction rolls
        back, so neither a partial effect nor a reservation is left behind. This
        is exact for systems that live in this database (the mock integrations).
        A real external system must be given the key and token so that it can
        apply the same two rules itself.
        """
        with self.transaction() as conn:
            if fence_scope and fence_token:
                row = conn.execute(
                    "SELECT token FROM fence_marks WHERE tenant_id=? AND scope=?", (tenant_id, fence_scope)
                ).fetchone()
                if row is not None and fence_token < row["token"]:
                    raise StaleLease(f"stale fencing token {fence_token} for {fence_scope}; current is {row['token']}")
                conn.execute(
                    "INSERT INTO fence_marks(tenant_id, scope, token) VALUES(?,?,?) "
                    "ON CONFLICT(tenant_id, scope) DO UPDATE SET token=excluded.token",
                    (tenant_id, fence_scope, fence_token),
                )
            if key:
                stored = conn.execute(
                    "SELECT result_json FROM idempotency WHERE tenant_id=? AND key=?", (tenant_id, key)
                ).fetchone()
                if stored is not None:
                    return json.loads(stored["result_json"]), True
            result = action()
            if key:
                conn.execute(
                    "INSERT INTO idempotency(tenant_id, key, operation, result_json, created_at) VALUES(?,?,?,?,?)",
                    (tenant_id, key, operation, json.dumps(result, default=str), _now()),
                )
            return result, False

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
