"""Approval policy: which tool calls run on their own, which wait for a person, and which never run.

A policy is an ordered list of rules. The first rule whose conditions all match
decides. Conditions left unset match anything. If no rule matches, the policy's
default applies. Policies are data: they are stored per tenant, validated on
write, and can be changed from the dashboard by an administrator without a
deployment.

Matching looks only at facts the system itself established (the tool's declared
risk, whether the run has read untrusted content, argument values), never at
anything the model says about its own intentions.
"""

from __future__ import annotations

import fnmatch
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..core.models import RiskLevel
from ..security.auth import Role
from ..storage import Database

Decision = Literal["allow", "require_approval", "deny"]
KV_NAMESPACE, KV_KEY = "policy", "approval"


class PolicyRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    description: str = Field(default="", max_length=300)
    # Conditions. All that are set must hold.
    tools: list[str] = Field(default_factory=list, max_length=30)  # shell-style patterns; empty matches every tool
    min_risk: RiskLevel | None = None
    tainted: bool | None = None  # True: only runs that read untrusted content; False: only runs that did not
    amount_argument: str | None = Field(default=None, max_length=60)  # numeric argument to compare
    amount_over: float | None = None
    # Outcome.
    decision: Decision
    approver_role: Role = Role.APPROVER

    def matches(self, tool: str, risk: RiskLevel, tainted: bool, arguments: dict[str, Any]) -> bool:
        if self.tools and not any(fnmatch.fnmatchcase(tool, pattern) for pattern in self.tools):
            return False
        if self.min_risk is not None and risk.rank < self.min_risk.rank:
            return False
        if self.tainted is not None and self.tainted != tainted:
            return False
        if self.amount_argument is not None and self.amount_over is not None:
            try:
                if float(arguments.get(self.amount_argument)) <= self.amount_over:
                    return False
            except (TypeError, ValueError):
                # An amount that cannot be read is not evidence that the amount is small.
                return True
        return True


class ApprovalPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    rules: list[PolicyRule] = Field(default_factory=list, max_length=50)
    default_decision: Decision = "allow"

    def evaluate(
        self, tool: str, risk: RiskLevel, tainted: bool, arguments: dict[str, Any]
    ) -> tuple[Decision, str, Role, str]:
        """Returns (decision, rule name, role required to approve, human-readable reason)."""
        for rule in self.rules:
            if rule.matches(tool, risk, tainted, arguments):
                return rule.decision, rule.name, rule.approver_role, rule.description or rule.name
        return self.default_decision, "default", Role.APPROVER, "no rule matched; policy default"


DEFAULT_POLICY = ApprovalPolicy(
    rules=[
        PolicyRule(
            name="large-refund-needs-admin",
            description="A refund above 500 must be approved by an administrator.",
            tools=["billing_issue_refund"],
            amount_argument="amount",
            amount_over=500,
            decision="require_approval",
            approver_role=Role.ADMIN,
        ),
        PolicyRule(
            name="high-risk-needs-approval",
            description="External, financial, or irreversible actions need a person's approval.",
            min_risk=RiskLevel.HIGH,
            decision="require_approval",
        ),
        PolicyRule(
            name="untrusted-content-tightens-control",
            description="This run has read content written outside the organisation, so actions that are "
            "awkward to undo need approval.",
            min_risk=RiskLevel.MEDIUM,
            tainted=True,
            decision="require_approval",
        ),
    ],
    default_decision="allow",
)


def load_policy(db: Database, tenant_id: str) -> ApprovalPolicy:
    stored = db.kv_get(tenant_id, KV_NAMESPACE, KV_KEY)
    return ApprovalPolicy.model_validate(stored) if stored else DEFAULT_POLICY.model_copy(deep=True)


def save_policy(db: Database, tenant_id: str, policy: ApprovalPolicy) -> ApprovalPolicy:
    current = load_policy(db, tenant_id)
    policy = policy.model_copy(update={"version": current.version + 1})
    db.kv_put(tenant_id, KV_NAMESPACE, KV_KEY, policy.model_dump(mode="json"))
    return policy
