"""Tamper-evident audit log.

The trace (events table) answers "what did the system do". The audit log
answers "who decided or changed what": approvals, run control, policy and key
changes, and every side effect performed on a business system.

Each record stores the hash of the previous record for the same tenant and its
own hash over (previous hash + its content). Editing or deleting a record
breaks the chain from that point on, and `verify` reports where. This detects
tampering by anyone who can write to the table but cannot recompute the chain
unnoticed; it does not stop someone who rewrites the whole chain, which needs
the head hash to be anchored outside the database (see docs/SECURITY-MODEL.md).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from ..storage import Database

GENESIS = "0" * 64


def _digest(prev_hash: str, record: dict[str, Any]) -> str:
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256((prev_hash + canonical).encode()).hexdigest()


class AuditLog:
    def __init__(self, db: Database):
        self.db = db

    def record(
        self, tenant_id: str, actor: str, action: str, target: str = "", actor_role: str = "system", **detail: Any
    ) -> dict[str, Any]:
        """Append one record. Reading the chain head and inserting happen in one transaction."""
        record = {
            "tenant_id": tenant_id,
            "at": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "actor": actor,
            "actor_role": actor_role,
            "action": action,
            "target": target,
            "detail": detail,
        }
        with self.db.transaction() as conn:
            last = conn.execute(
                "SELECT hash FROM audit_log WHERE tenant_id=? ORDER BY seq DESC LIMIT 1", (tenant_id,)
            ).fetchone()
            prev_hash = last["hash"] if last else GENESIS
            record_hash = _digest(prev_hash, record)
            conn.execute(
                "INSERT INTO audit_log(tenant_id, at, actor, actor_role, action, target, detail_json, prev_hash, hash) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    tenant_id,
                    record["at"],
                    actor,
                    actor_role,
                    action,
                    target,
                    json.dumps(detail, sort_keys=True, default=str),
                    prev_hash,
                    record_hash,
                ),
            )
        return {**record, "hash": record_hash}

    def list(
        self, tenant_id: str, limit: int = 100, action_prefix: str | None = None, target: str | None = None
    ) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM audit_log WHERE tenant_id=?", [tenant_id]
        if action_prefix:
            sql += " AND action LIKE ?"
            params.append(action_prefix.replace("%", "") + "%")
        if target:
            sql += " AND target=?"
            params.append(target)
        rows = self.db.query(sql + " ORDER BY seq DESC LIMIT ?", (*params, limit))
        return [
            {
                "seq": row["seq"],
                "at": row["at"],
                "actor": row["actor"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "target": row["target"],
                "detail": json.loads(row["detail_json"]),
                "hash": row["hash"],
            }
            for row in rows
        ]

    def verify(self, tenant_id: str) -> dict[str, Any]:
        """Recompute the chain. Returns whether it is intact and, if not, the first broken record."""
        rows = self.db.query("SELECT * FROM audit_log WHERE tenant_id=? ORDER BY seq", (tenant_id,))
        prev_hash = GENESIS
        for row in rows:
            record = {
                "tenant_id": row["tenant_id"],
                "at": row["at"],
                "actor": row["actor"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "target": row["target"],
                "detail": json.loads(row["detail_json"]),
            }
            if row["prev_hash"] != prev_hash or row["hash"] != _digest(prev_hash, record):
                return {"ok": False, "records": len(rows), "broken_at_seq": row["seq"], "head": prev_hash}
            prev_hash = row["hash"]
        return {"ok": True, "records": len(rows), "broken_at_seq": None, "head": prev_hash}
