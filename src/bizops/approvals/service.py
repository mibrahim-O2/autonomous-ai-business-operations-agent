"""Approval requests and the gate that enforces them.

Flow:
  1. An agent asks the tool gateway to run a tool. The gateway asks `PolicyGate`.
  2. If the policy requires approval, the gate stores a pending request and the
     gateway raises `ApprovalRequired`. The orchestrator suspends the run.
  3. A person with a sufficient role approves or rejects through `ApprovalService.decide`.
     The decision is a conditional update, so a request is decided exactly once.
  4. The run is resumed. The step runs again and reaches the gate again:
       approved -> the call is allowed and executes now, for the first time
       rejected -> the call is denied; the agent sees the refusal and the reason
       expired  -> treated as a refusal (fail closed)

The tool does not run before step 3. Approval is bound to the fingerprint of
the exact tool and arguments shown to the approver, so an approved request
cannot be used to run a different call.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from ..core.errors import Conflict, PermissionDenied
from ..core.models import PlanStep, RunState, ToolCall, new_id
from ..security.audit import AuditLog
from ..security.auth import Permission, Principal, Role
from ..security.injection import extract_refs
from ..storage import Database
from ..tools.base import ToolSpec
from ..tools.gateway import GateDecision, GateVerdict
from .policy import load_policy

PENDING, APPROVED, REJECTED, EXPIRED, CANCELLED = "pending", "approved", "rejected", "expired", "cancelled"


def _row(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["arguments"] = json.loads(data.pop("arguments_json"))
    data["signals"] = json.loads(data.pop("signals_json"))
    return data


class ApprovalService:
    def __init__(self, db: Database, audit: AuditLog, ttl_seconds: float = 86_400.0):
        self.db, self.audit, self.ttl_seconds = db, audit, ttl_seconds

    # Reading -----------------------------------------------------------------
    def get(self, tenant_id: str, approval_id: str) -> dict[str, Any] | None:
        self.expire_overdue(tenant_id)
        rows = self.db.query("SELECT * FROM approvals WHERE tenant_id=? AND approval_id=?", (tenant_id, approval_id))
        return _row(rows[0]) if rows else None

    def list(
        self, tenant_id: str, status: str | None = None, run_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        self.expire_overdue(tenant_id)
        sql, params = "SELECT * FROM approvals WHERE tenant_id=?", [tenant_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        if run_id:
            sql += " AND run_id=?"
            params.append(run_id)
        rows = self.db.query(sql + " ORDER BY requested_at DESC LIMIT ?", (*params, limit))
        return [_row(row) for row in rows]

    def counts(self, tenant_id: str) -> dict[str, int]:
        self.expire_overdue(tenant_id)
        rows = self.db.query(
            "SELECT status, COUNT(*) AS n FROM approvals WHERE tenant_id=? GROUP BY status", (tenant_id,)
        )
        return {row["status"]: row["n"] for row in rows}

    def is_pending(self, tenant_id: str, approval_id: str | None) -> bool:
        if not approval_id:
            return False
        record = self.get(tenant_id, approval_id)
        return bool(record and record["status"] == PENDING)

    # Writing -----------------------------------------------------------------
    def expire_overdue(self, tenant_id: str) -> int:
        """Mark unanswered requests past their deadline as expired. An expired request is a refusal."""
        now = datetime.now(UTC).isoformat(timespec="milliseconds")
        overdue = self.db.query(
            "SELECT approval_id FROM approvals WHERE tenant_id=? AND status=? AND expires_at<?",
            (tenant_id, PENDING, now),
        )
        for row in overdue:
            changed = self.db.execute(
                "UPDATE approvals SET status=?, decided_at=?, decided_by='system', "
                "comment='no decision before the deadline' WHERE approval_id=? AND status=?",
                (EXPIRED, now, row["approval_id"], PENDING),
            ).rowcount
            if changed:
                self.audit.record(tenant_id, "system", "approval.expired", row["approval_id"])
        return len(overdue)

    def request(
        self,
        state: RunState,
        step: PlanStep,
        agent: str,
        spec: ToolSpec,
        call: ToolCall,
        fingerprint: str,
        rule: str,
        reason: str,
        required_role: Role,
        signals: list[str],
    ) -> dict[str, Any]:
        approval_id = new_id("apr")
        now = datetime.now(UTC)
        self.db.execute(
            """INSERT INTO approvals(approval_id, tenant_id, run_id, step_id, step_action, agent, tool, arguments_json,
                                     fingerprint, risk, rule, reason, signals_json, required_role, status,
                                     requested_at, expires_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                approval_id,
                state.tenant_id,
                state.run_id,
                step.step_id,
                step.action,
                agent,
                spec.name,
                json.dumps(call.arguments, sort_keys=True, default=str),
                fingerprint,
                spec.risk.value,
                rule,
                reason,
                json.dumps(signals),
                required_role.value,
                PENDING,
                now.isoformat(timespec="milliseconds"),
                (now + timedelta(seconds=self.ttl_seconds)).isoformat(timespec="milliseconds"),
            ),
        )
        self.audit.record(
            state.tenant_id,
            f"agent:{agent}",
            "approval.requested",
            approval_id,
            actor_role="agent",
            run_id=state.run_id,
            tool=spec.name,
            risk=spec.risk.value,
            rule=rule,
            signals=signals,
        )
        return self.get(state.tenant_id, approval_id) or {}

    def decide(
        self, principal: Principal, approval_id: str, approve: bool, comment: str = "", scope: str = "call"
    ) -> dict[str, Any]:
        """Record a person's decision. Raises PermissionDenied or Conflict; never decides twice.

        `scope` "call" covers the exact call shown. "step_tool" also covers further calls to the
        same tool within the same step of the same run (for example a batch of reminders), which
        the approver chooses knowingly; it never extends to another tool, step, or run.
        """
        record = self.get(principal.tenant_id, approval_id)
        if record is None:
            raise Conflict("no such approval request")
        if scope not in ("call", "step_tool"):
            raise Conflict("scope must be 'call' or 'step_tool'")
        required = Role(record["required_role"])
        if not principal.can(Permission.APPROVAL_DECIDE) or not principal.role.at_least(required):
            self.audit.record(
                principal.tenant_id,
                principal.name,
                "approval.decision_refused",
                approval_id,
                actor_role=principal.role.value,
                required_role=required.value,
            )
            raise PermissionDenied(f"deciding this request needs the '{required.value}' role")
        now = datetime.now(UTC).isoformat(timespec="milliseconds")
        changed = self.db.execute(
            "UPDATE approvals SET status=?, decided_at=?, decided_by=?, decided_role=?, comment=?, scope=? "
            "WHERE tenant_id=? AND approval_id=? AND status=?",
            (
                APPROVED if approve else REJECTED,
                now,
                principal.name,
                principal.role.value,
                comment[:1000],
                scope if approve else "call",
                principal.tenant_id,
                approval_id,
                PENDING,
            ),
        ).rowcount
        if not changed:
            raise Conflict(f"this request is already {record['status']}")
        self.audit.record(
            principal.tenant_id,
            principal.name,
            "approval.approved" if approve else "approval.rejected",
            approval_id,
            actor_role=principal.role.value,
            run_id=record["run_id"],
            tool=record["tool"],
            fingerprint=record["fingerprint"],
            scope=scope if approve else "call",
            comment=comment[:1000],
        )
        return self.get(principal.tenant_id, approval_id) or {}

    def cancel_for_run(self, tenant_id: str, run_id: str, actor: str) -> int:
        """Withdraw pending requests of a run that was cancelled, so nobody approves a dead request."""
        now = datetime.now(UTC).isoformat(timespec="milliseconds")
        changed = self.db.execute(
            "UPDATE approvals SET status=?, decided_at=?, decided_by=?, comment='run was cancelled' "
            "WHERE tenant_id=? AND run_id=? AND status=?",
            (CANCELLED, now, actor, tenant_id, run_id, PENDING),
        ).rowcount
        return changed


class PolicyGate:
    """The `ApprovalGate` used in production: evaluates the tenant's policy and tracks decisions."""

    def __init__(self, db: Database, approvals: ApprovalService):
        self.db, self.approvals = db, approvals

    def evaluate(
        self, state: RunState, step: PlanStep, agent: str, spec: ToolSpec, call: ToolCall, fingerprint: str
    ) -> GateVerdict:
        if not spec.side_effect:
            return GateVerdict(decision=GateDecision.ALLOW, reason="read-only")

        # 1. Has a person already decided on this exact call, or on this tool for this step?
        self.approvals.expire_overdue(state.tenant_id)
        rows = self.db.query(
            "SELECT * FROM approvals WHERE tenant_id=? AND run_id=? AND step_id=? AND tool=? "
            "ORDER BY requested_at DESC",
            (state.tenant_id, state.run_id, step.step_id, spec.name),
        )
        exact = next((r for r in rows if r["fingerprint"] == fingerprint), None)
        if exact is not None:
            if exact["status"] == APPROVED:
                return GateVerdict(
                    decision=GateDecision.ALLOW,
                    reason=f"approved by {exact['decided_by']}",
                    approval_id=exact["approval_id"],
                )
            if exact["status"] == PENDING:
                return GateVerdict(
                    decision=GateDecision.REQUIRE_APPROVAL, reason=exact["reason"], approval_id=exact["approval_id"]
                )
            detail = f": {exact['comment']}" if exact["comment"] else ""
            return GateVerdict(
                decision=GateDecision.DENY,
                reason=f"{exact['status']} by {exact['decided_by'] or 'system'}{detail}",
                approval_id=exact["approval_id"],
            )

        # 2. Ask the policy.
        decision, rule, role, reason = load_policy(self.db, state.tenant_id).evaluate(
            spec.name, spec.risk, state.tainted, call.arguments
        )
        if decision == "allow":
            return GateVerdict(decision=GateDecision.ALLOW, reason=f"policy rule '{rule}'")
        if decision == "deny":
            return GateVerdict(decision=GateDecision.DENY, reason=f"policy rule '{rule}': {reason}")

        # A step-wide approval for this tool covers this call too, provided the same rule and role apply.
        blanket = next(
            (
                r
                for r in rows
                if r["status"] == APPROVED and r["scope"] == "step_tool" and Role(r["decided_role"]).at_least(role)
            ),
            None,
        )
        if blanket is not None:
            return GateVerdict(
                decision=GateDecision.ALLOW,
                reason=f"covered by step-wide approval from {blanket['decided_by']}",
                approval_id=blanket["approval_id"],
            )

        # 3. Ask a person. Point out anything the approver should look at twice.
        signals: list[str] = []
        if state.tainted:
            signals.append("run_read_untrusted_content")
        if state.injection_findings:
            signals.append("injection_attempt_suspected_in_this_run")
        if extract_refs(call.arguments) & set(state.untrusted_refs):
            signals.append("address_or_link_seen_only_in_untrusted_content")
        record = self.approvals.request(state, step, agent, spec, call, fingerprint, rule, reason, role, signals)
        return GateVerdict(decision=GateDecision.REQUIRE_APPROVAL, reason=reason, approval_id=record["approval_id"])
