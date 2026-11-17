"""Human-in-the-loop control: the policy decides, a person approves or rejects, and the run obeys."""

import asyncio

import pytest
from conftest import approve_until_done, approver, run

from bizops.approvals import ApprovalPolicy, PolicyRule, load_policy, save_policy
from bizops.core.errors import Conflict, PermissionDenied
from bizops.core.models import RiskLevel, RunStatus, StepStatus, Task

REFUND = Task(
    objective="Handle a refund request for a paid invoice",
    workflow="refund_request",
    inputs={"invoice_id": "INV-1005", "amount": 320, "reason": "Service cancelled before it started"},
)
COLLECTIONS = Task(objective="Review overdue invoices and send payment reminders", workflow="invoice_collections")


def refunds(rt):
    return rt.db.kv_list("demo", "billing.refunds")


def test_high_risk_action_waits_for_a_person_and_nothing_happens_before(rt):
    state = run(rt.orchestrator.submit(REFUND))
    assert state.status == RunStatus.WAITING_APPROVAL
    step = next(s for s in state.plan.steps if s.action == "issue_refund")
    assert step.status == StepStatus.WAITING_APPROVAL
    assert refunds(rt) == {}  # the refund has not been issued
    [request] = rt.approvals.list("demo", status="pending")
    assert request["approval_id"] == state.pending_approval_id
    assert request["tool"] == "billing_issue_refund" and request["risk"] == "high"
    assert request["arguments"] == {
        "amount": 320.0,
        "invoice_id": "INV-1005",
        "reason": "Service cancelled before it started",
    }
    assert request["rule"] == "high-risk-needs-approval" and request["required_role"] == "approver"
    assert "run.waiting_approval" in [e["kind"] for e in rt.tracer.timeline("demo", state.run_id)]


def test_approval_executes_the_call_exactly_once(rt):
    state = run(rt.orchestrator.submit(REFUND))
    record = rt.approvals.decide(approver(rt, "approver"), state.pending_approval_id, True, "matches the record")
    assert record["status"] == "approved" and record["decided_by"] == "test-approver"
    done = run(rt.orchestrator.resume("demo", state.run_id))
    assert done.status == RunStatus.COMPLETED and done.final_report["outcome"] == "verified"
    assert len(refunds(rt)) == 1
    assert run(rt.orchestrator.resume("demo", state.run_id)).status == RunStatus.COMPLETED  # nothing re-runs
    assert len(refunds(rt)) == 1
    assert len(rt.db.kv_list("demo", "email.drafts")) == 1 and rt.db.kv_list("demo", "email.sent") == {}


def test_rejection_stops_the_action_and_hands_the_case_to_a_person(rt):
    state = run(rt.orchestrator.submit(REFUND))
    rt.approvals.decide(approver(rt), state.pending_approval_id, False, "customer used the service")
    done = run(rt.orchestrator.resume("demo", state.run_id))
    assert refunds(rt) == {}  # never issued
    assert done.status == RunStatus.COMPLETED and done.replans == 1
    assert [s.action for s in done.plan.steps if s.status == StepStatus.SUCCEEDED][-1] == "record_refund_outcome"
    # The rejected step was superseded by the recovery plan; the reason is kept with the failure.
    [failure] = done.working_memory["_failures"]
    assert (
        failure["action"] == "issue_refund"
        and "rejected by test-approver: customer used the service" in failure["error"]
    )
    [task] = rt.db.kv_list("demo", "ops.tasks").values()
    assert task["assignee"] == "finance" and "customer used the service" in task["detail"]
    actions = [e["action"] for e in rt.audit.list("demo")]
    assert "approval.rejected" in actions and "tool.denied" in actions and "tool.executed" in actions


def test_ineligible_request_is_refused_before_anyone_is_asked(rt):
    over = Task(objective=REFUND.objective, workflow="refund_request", inputs={**REFUND.inputs, "amount": 5000})
    state = run(rt.orchestrator.submit(over))
    assert rt.approvals.list("demo") == [] and refunds(rt) == {}
    assert state.status == RunStatus.COMPLETED and state.replans == 1
    assert "exceeds refundable balance 320" in state.working_memory["_failures"][0]["error"]


def test_approval_is_bound_to_the_exact_arguments(rt):
    state = run(rt.orchestrator.submit(REFUND))
    first = rt.approvals.get("demo", state.pending_approval_id)
    rt.approvals.decide(approver(rt), first["approval_id"], True)
    # Something changes the arguments between the request and the retry (here: the record is edited).
    step = next(s for s in state.plan.steps if s.action == "issue_refund")
    gate, spec = rt.gateway.gate, rt.gateway.spec("billing_issue_refund")
    from bizops.core.models import ToolCall
    from bizops.tools.gateway import GateDecision, fingerprint

    other = ToolCall(tool=spec.name, arguments={**first["arguments"], "amount": 300.0})
    verdict = gate.evaluate(state, step, "finance_ops", spec, other, fingerprint(spec.name, other.arguments))
    assert verdict.decision == GateDecision.REQUIRE_APPROVAL and verdict.approval_id != first["approval_id"]


def test_roles_and_single_decision(rt):
    big = Task(objective=REFUND.objective, workflow="refund_request", inputs={"invoice_id": "INV-1005", "amount": 320})
    save_policy(
        rt.db,
        "demo",
        ApprovalPolicy(
            rules=[
                PolicyRule(
                    name="refund-admin",
                    tools=["billing_issue_refund"],
                    amount_argument="amount",
                    amount_over=100,
                    decision="require_approval",
                    approver_role="admin",
                )
            ]
        ),
    )
    state = run(rt.orchestrator.submit(big))
    approval_id = state.pending_approval_id
    for role in ("viewer", "operator", "approver"):
        with pytest.raises(PermissionDenied):
            rt.approvals.decide(approver(rt, role), approval_id, True)
    assert rt.approvals.get("demo", approval_id)["status"] == "pending"
    rt.approvals.decide(approver(rt, "admin"), approval_id, True)
    with pytest.raises(Conflict):
        rt.approvals.decide(approver(rt, "admin"), approval_id, False)  # decided exactly once
    assert [e["action"] for e in rt.audit.list("demo")].count("approval.decision_refused") == 3


def test_simultaneous_decisions_have_one_winner(rt):
    state = run(rt.orchestrator.submit(REFUND))

    def decide(approve):
        try:
            return rt.approvals.decide(approver(rt, name=f"p-{approve}"), state.pending_approval_id, approve)["status"]
        except Conflict:
            return "conflict"

    async def both():
        return await asyncio.gather(asyncio.to_thread(decide, True), asyncio.to_thread(decide, False))

    outcomes = sorted(run(both()))
    assert outcomes.count("conflict") == 1 and len(set(outcomes)) == 2


def test_unanswered_request_expires_as_a_refusal(rt):
    state = run(rt.orchestrator.submit(REFUND))
    rt.db.execute("UPDATE approvals SET expires_at='2000-01-01T00:00:00.000+00:00'")
    assert rt.approvals.get("demo", state.pending_approval_id)["status"] == "expired"
    done = run(rt.orchestrator.resume("demo", state.run_id))
    assert refunds(rt) == {} and done.status == RunStatus.COMPLETED and done.replans == 1
    assert "approval.expired" in [e["action"] for e in rt.audit.list("demo")]


def test_resume_is_a_no_op_while_a_decision_is_pending(rt):
    state = run(rt.orchestrator.submit(REFUND))
    calls = rt.llm.calls[:]
    again = run(rt.orchestrator.resume("demo", state.run_id))
    assert again.status == RunStatus.WAITING_APPROVAL and rt.llm.calls == calls  # no model call spent
    assert "run.resume_blocked" in [e["kind"] for e in rt.tracer.timeline("demo", state.run_id)]


def test_cancelling_a_waiting_run_withdraws_its_requests(rt):
    state = run(rt.orchestrator.submit(REFUND))
    assert rt.orchestrator.cancel("demo", state.run_id).status == RunStatus.CANCELLED
    assert rt.approvals.get("demo", state.pending_approval_id)["status"] == "cancelled"
    with pytest.raises(Conflict):
        rt.approvals.decide(approver(rt), state.pending_approval_id, True)
    assert refunds(rt) == {}


def test_each_reminder_needs_its_own_approval_unless_the_step_is_approved(rt):
    state = run(rt.orchestrator.submit(COLLECTIONS))
    first = state.pending_approval_id
    rt.approvals.decide(approver(rt), first, True)  # this call only
    state = run(rt.orchestrator.resume("demo", state.run_id))
    assert state.status == RunStatus.WAITING_APPROVAL and state.pending_approval_id != first
    assert len(rt.db.kv_list("demo", "email.sent")) == 1  # the approved one went out; the next waits
    done = approve_until_done(rt, state, scope="call")
    assert done.status == RunStatus.COMPLETED and len(rt.db.kv_list("demo", "email.sent")) == 2
    assert len(rt.approvals.list("demo", status="approved")) == 2


def test_step_wide_approval_covers_the_batch_with_one_decision(rt):
    state = run(rt.orchestrator.submit(COLLECTIONS))
    rt.approvals.decide(approver(rt), state.pending_approval_id, True, scope="step_tool")
    done = run(rt.orchestrator.resume("demo", state.run_id))
    assert done.status == RunStatus.COMPLETED and len(rt.db.kv_list("demo", "email.sent")) == 2
    assert len(rt.approvals.list("demo")) == 1


def test_policy_rules_decide_and_can_deny(rt):
    policy = load_policy(rt.db, "demo")
    assert policy.evaluate("email_send", RiskLevel.HIGH, False, {})[0] == "require_approval"
    assert policy.evaluate("calendar_create_event", RiskLevel.MEDIUM, False, {})[0] == "allow"
    assert policy.evaluate("calendar_create_event", RiskLevel.MEDIUM, True, {})[0] == "require_approval"
    decision, rule, role, _ = policy.evaluate("billing_issue_refund", RiskLevel.HIGH, False, {"amount": 900})
    assert (decision, rule, role.value) == ("require_approval", "large-refund-needs-admin", "admin")
    # An unreadable amount is not treated as a small one.
    assert (
        policy.evaluate("billing_issue_refund", RiskLevel.HIGH, False, {"amount": "lots"})[1]
        == "large-refund-needs-admin"
    )

    save_policy(
        rt.db,
        "demo",
        ApprovalPolicy(rules=[PolicyRule(name="no-refunds", tools=["billing_issue_refund"], decision="deny")]),
    )
    state = run(rt.orchestrator.submit(REFUND))
    assert rt.approvals.list("demo") == [] and refunds(rt) == {}
    assert state.status == RunStatus.COMPLETED and state.replans == 1  # denied, then handed to a person
    assert load_policy(rt.db, "demo").version == 2


def test_read_only_tools_never_ask(rt):
    save_policy(rt.db, "demo", ApprovalPolicy(rules=[], default_decision="require_approval"))
    state = run(rt.orchestrator.submit(Task(objective="What is our refund policy?")))
    assert state.status == RunStatus.COMPLETED and rt.approvals.list("demo") == []
