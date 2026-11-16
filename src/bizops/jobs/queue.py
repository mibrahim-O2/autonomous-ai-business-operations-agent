"""A small durable job queue on SQLite.

Webhooks and schedules do not run tasks inline. They enqueue a job and return;
a worker claims due jobs and runs them. This decouples intake from execution:
a burst of webhooks cannot stall the HTTP server, a crash loses nothing because
jobs are rows, and a duplicate delivery is dropped by the unique dedupe key.

Claiming uses a conditional UPDATE, so two workers cannot take the same job.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from ..core.models import Task, new_id
from ..observability import get_logger
from ..storage import Database

log = get_logger("jobs")
MAX_ATTEMPTS = 3
STALE_AFTER = timedelta(minutes=10)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds")


class JobQueue:
    def __init__(self, db: Database):
        self.db = db

    def enqueue_task(
        self, task: Task, *, run_at: datetime | None = None, dedupe_key: str | None = None
    ) -> tuple[str, bool]:
        """Queue a task. Returns (job_id, created). A repeated dedupe key returns the original job."""
        if dedupe_key:
            rows = self.db.query(
                "SELECT job_id FROM jobs WHERE tenant_id=? AND dedupe_key=?", (task.tenant_id, dedupe_key)
            )
            if rows:
                return rows[0]["job_id"], False
        job_id, now = new_id("job"), _iso(datetime.now(UTC))
        self.db.execute(
            "INSERT INTO jobs(job_id, tenant_id, kind, payload_json, status, run_at, dedupe_key, created_at, "
            "updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                task.tenant_id,
                "task",
                task.model_dump_json(),
                "pending",
                _iso(run_at or datetime.now(UTC)),
                dedupe_key,
                now,
                now,
            ),
        )
        return job_id, True

    def claim_due(self, now: datetime | None = None) -> dict[str, Any] | None:
        """Atomically take the oldest due job, or return None."""
        current = now or datetime.now(UTC)
        moment = _iso(current)
        stale = _iso(current - STALE_AFTER)
        with self.db.transaction() as conn:
            # A job left 'running' by a worker that died is returned to the queue after a timeout.
            conn.execute("UPDATE jobs SET status='pending' WHERE status='running' AND updated_at<?", (stale,))
            row = conn.execute(
                "SELECT * FROM jobs WHERE status='pending' AND run_at<=? ORDER BY run_at LIMIT 1", (moment,)
            ).fetchone()
            if row is None:
                return None
            claimed = conn.execute(
                "UPDATE jobs SET status='running', attempts=attempts+1, updated_at=? "
                "WHERE job_id=? AND status='pending'",
                (moment, row["job_id"]),
            ).rowcount
        return dict(row) | {"attempts": row["attempts"] + 1} if claimed else None

    def finish(self, job_id: str, status: str, result: dict[str, Any], retry_in: timedelta | None = None) -> None:
        now = datetime.now(UTC)
        if retry_in is not None:
            self.db.execute(
                "UPDATE jobs SET status='pending', run_at=?, result_json=?, updated_at=? WHERE job_id=?",
                (_iso(now + retry_in), json.dumps(result, default=str), _iso(now), job_id),
            )
        else:
            self.db.execute(
                "UPDATE jobs SET status=?, result_json=?, updated_at=? WHERE job_id=?",
                (status, json.dumps(result, default=str), _iso(now), job_id),
            )

    def get(self, job_id: str) -> dict[str, Any] | None:
        rows = self.db.query("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        if not rows:
            return None
        row = dict(rows[0])
        row["result"] = json.loads(row["result_json"]) if row["result_json"] else None
        return row

    def counts(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self.db.query("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}


class Worker:
    """Runs queued tasks through the orchestrator."""

    def __init__(self, queue: JobQueue, orchestrator: Any):
        self.queue, self.orchestrator = queue, orchestrator

    async def run_once(self) -> dict[str, Any] | None:
        """Process one due job. Returns its outcome, or None if the queue is idle."""
        job = self.queue.claim_due()
        if job is None:
            return None
        try:
            task = Task.model_validate_json(job["payload_json"])
            state = await self.orchestrator.submit(task)
            result = {"run_id": state.run_id, "run_status": state.status.value}
            self.queue.finish(job["job_id"], "done", result)
        except Exception as exc:  # an unexpected fault in one job must not stop the worker
            log.exception("job %s failed", job["job_id"])
            result = {"error": f"{type(exc).__name__}: {exc}"}
            if job["attempts"] < MAX_ATTEMPTS:
                self.queue.finish(job["job_id"], "pending", result, retry_in=timedelta(seconds=2 ** job["attempts"]))
            else:
                self.queue.finish(job["job_id"], "dead", result)
        return {"job_id": job["job_id"], **result}

    async def drain(self, limit: int = 100) -> list[dict[str, Any]]:
        """Process due jobs until none remain (used by tests and the CLI)."""
        outcomes = []
        while len(outcomes) < limit and (outcome := await self.run_once()) is not None:
            outcomes.append(outcome)
        return outcomes

    async def run_forever(self, poll_seconds: float = 1.0) -> None:
        while True:
            if await self.run_once() is None:
                await asyncio.sleep(poll_seconds)
