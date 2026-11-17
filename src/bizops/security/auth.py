"""Authentication and role-based access control.

An API key identifies a principal: a name, exactly one tenant, and exactly one
role. The tenant always comes from the key, never from a request parameter, so
a caller cannot read or act on another tenant's data by asking for it.

Key format:  bzk_<key id>_<secret>
Only a SHA-256 hash of the secret is stored. The secret is 32 random bytes, so
a fast hash is appropriate (a slow password hash protects low-entropy secrets,
which these are not). The plaintext is shown once, when the key is created.

Roles are ordered; each includes everything below it:

    viewer    read runs, traces, tools, knowledge, memory, approvals, audit
    operator  + start workflows, pause, resume, cancel, manage knowledge and memory
    approver  + decide approval requests
    admin     + enable and disable agents, change the approval policy, manage keys
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from ..core.models import new_id
from ..storage import Database


class Role(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    APPROVER = "approver"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        return list(Role).index(self)

    def at_least(self, other: Role) -> bool:
        return self.rank >= other.rank


class Permission(StrEnum):
    READ = "read"
    RUN_START = "run.start"
    RUN_CONTROL = "run.control"
    KNOWLEDGE_WRITE = "knowledge.write"
    MEMORY_WRITE = "memory.write"
    APPROVAL_DECIDE = "approval.decide"
    AGENT_MANAGE = "agent.manage"
    POLICY_WRITE = "policy.write"
    KEYS_MANAGE = "keys.manage"


# The lowest role that holds each permission.
MINIMUM_ROLE: dict[Permission, Role] = {
    Permission.READ: Role.VIEWER,
    Permission.RUN_START: Role.OPERATOR,
    Permission.RUN_CONTROL: Role.OPERATOR,
    Permission.KNOWLEDGE_WRITE: Role.OPERATOR,
    Permission.MEMORY_WRITE: Role.OPERATOR,
    Permission.APPROVAL_DECIDE: Role.APPROVER,
    Permission.AGENT_MANAGE: Role.ADMIN,
    Permission.POLICY_WRITE: Role.ADMIN,
    Permission.KEYS_MANAGE: Role.ADMIN,
}


def permissions_for(role: Role) -> list[str]:
    return [p.value for p, minimum in MINIMUM_ROLE.items() if role.at_least(minimum)]


@dataclass(frozen=True)
class Principal:
    """Who is making a request."""

    name: str
    tenant_id: str
    role: Role
    key_id: str = ""
    kind: str = "api_key"  # api_key | bootstrap | local | system

    def can(self, permission: Permission) -> bool:
        return self.role.at_least(MINIMUM_ROLE[permission])

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tenant_id": self.tenant_id,
            "role": self.role.value,
            "kind": self.kind,
            "permissions": permissions_for(self.role),
        }


def LOCAL_OPERATOR(tenant_id: str) -> Principal:  # noqa: N802 - reads as a constant at call sites
    """The person at the keyboard when the server is used from the same machine with no key configured."""
    return Principal(name="local-operator", tenant_id=tenant_id, role=Role.ADMIN, kind="local")


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class ApiKeyStore:
    def __init__(self, db: Database):
        self.db = db

    def create(self, tenant_id: str, name: str, role: Role, created_by: str = "cli") -> tuple[str, str]:
        """Create a key. Returns (key_id, plaintext token). The token cannot be recovered later."""
        key_id, secret = new_id("key").removeprefix("key_"), secrets.token_urlsafe(32)
        self.db.execute(
            "INSERT INTO api_keys(key_id, tenant_id, name, role, secret_hash, created_at, created_by) "
            "VALUES(?,?,?,?,?,?,?)",
            (key_id, tenant_id, name, Role(role).value, _hash(secret), datetime.now(UTC).isoformat(), created_by),
        )
        return key_id, f"bzk_{key_id}_{secret}"

    def authenticate(self, token: str) -> Principal | None:
        """Resolve a bearer token to a principal, or None. Constant-time on the secret."""
        parts = token.split("_", 2)
        if len(parts) != 3 or parts[0] != "bzk":
            return None
        rows = self.db.query("SELECT * FROM api_keys WHERE key_id=?", (parts[1],))
        # Compare against a dummy hash when the key id is unknown, so timing does not reveal which ids exist.
        stored = rows[0]["secret_hash"] if rows else "0" * 64
        matches = hmac.compare_digest(stored.encode(), _hash(parts[2]).encode())
        if not rows or not matches or rows[0]["revoked_at"]:
            return None
        self.db.execute("UPDATE api_keys SET last_used_at=? WHERE key_id=?", (datetime.now(UTC).isoformat(), parts[1]))
        row = rows[0]
        return Principal(name=row["name"], tenant_id=row["tenant_id"], role=Role(row["role"]), key_id=row["key_id"])

    def any_active(self) -> bool:
        return bool(self.db.query("SELECT 1 FROM api_keys WHERE revoked_at IS NULL LIMIT 1"))

    def revoke(self, tenant_id: str, key_id: str) -> bool:
        cursor = self.db.execute(
            "UPDATE api_keys SET revoked_at=? WHERE tenant_id=? AND key_id=? AND revoked_at IS NULL",
            (datetime.now(UTC).isoformat(), tenant_id, key_id),
        )
        return cursor.rowcount > 0

    def list(self, tenant_id: str) -> list[dict[str, Any]]:
        """Key metadata for a tenant. Never includes the secret or its hash."""
        rows = self.db.query(
            "SELECT key_id, tenant_id, name, role, created_at, created_by, last_used_at, revoked_at "
            "FROM api_keys WHERE tenant_id=? ORDER BY created_at",
            (tenant_id,),
        )
        return [dict(row) for row in rows]
