"""End-to-end behaviour of the orchestration engine on the lead intake workflow."""

from conftest import lead_task, make_settings, run, started

from bizops.cli import main as cli_main
from bizops.core.models import PlanStep, RunState, RunStatus, StepStatus, Task
from bizops.llm import LLMResponse
from bizops.orchestrator import resolve_refs
from bizops.tools import GateDecision, GateVerdict

EMAIL = "noor.abbasi@quillfield.example"
STEPS = ["recall_context", "research_policy", "qualify_lead", "draft_reply", "propose_slots"]


def test_lead_intake_runs_to_a_verified_report(rt):
    state = run(rt.orchestrator.submit(lead_task()))
    assert state.status == RunStatus.COMPLETED and state.final_report["outcome"] == "verified"
    assert [s.action for s in state.plan.steps] == STEPS
    assert all(s.status == StepStatus.SUCCEEDED for s in state.plan.steps)
    results = state.final_report["results"]
    assert results["qualify_lead"]["tier"] == "hot" and results["qualify_lead"]["score"] == 85
    assert len(results["propose_slots"]["slots"]) == 4 and results["draft_reply"]["sent"] is False
    # Real effects in the demo environment, not just descriptions of them.
    contact = rt.db.kv_get("demo", "crm.contacts", EMAIL)
    assert contact["lead_tier"] == "hot" and len(contact["notes"]) == 1
    drafts = list(rt.db.kv_list("demo", "email.drafts").values())
    assert len(drafts) == 1 and drafts[0]["status"] == "draft" and "Hello Noor" in drafts[0]["body"]
    assert state.usage.calls >= 6 and state.steps_executed == 5


def test_trace_records_the_whole_execution(rt):
    state = run(rt.orchestrator.submit(lead_task()))
    timeline = rt.tracer.timeline("demo", state.run_id)
    kinds = [e["kind"] for e in timeline]
    for expected in (
        "run.created",
        "run.analyzed",
        "plan.created",
        "step.started",
        "llm.call",
        "tool.call",
        "run.completed",
    ):
        assert expected in kinds
    tool_spans = [e for e in timeline if e["kind"] == "tool.call"]
    assert all(e["duration_ms"] is not None and e["parent_span_id"] for e in tool_spans)
    assert {e["tool"] for e in tool_spans} >= {"crm_find_contact", "crm_upsert_contact", "email_create_draft"}


def test_memory_from_one_run_informs_the_next(rt):
    first = run(rt.orchestrator.submit(lead_task()))
    assert first.final_report["results"]["recall_context"]["known"] is False
    second = run(rt.orchestrator.submit(lead_task(inputs={"message": "Following up on my earlier enquiry."})))
    recall = second.final_report["results"]["recall_context"]
    assert recall["known"] is True and any("Quillfield" in text for text in recall["prior_knowledge"])
    assert any("already known" in reason for reason in second.final_report["results"]["qualify_lead"]["reasons"])
    assert {m["kind"] for m in rt.db.list_memory("demo")} == {"semantic", "episodic"}


def test_cold_lead_gets_no_meeting_offer(rt):
    state = run(rt.orchestrator.submit(lead_task(inputs={"message": "I am a student doing homework", "company": ""})))
    results = state.final_report["results"]
    assert results["qualify_lead"]["tier"] == "cold" and results["propose_slots"] == {"slots": [], "offered": False}
    assert results["recall_context"]["retrieval_used"] is True  # a name was still available to search on


def test_agent_skips_retrieval_when_there_is_nothing_to_search(rt):
    state = run(rt.orchestrator.submit(lead_task(inputs={"name": "", "company": ""})))
    assert state.final_report["results"]["recall_context"]["retrieval_used"] is False
    assert "memory_search" not in [t.tool for t in state.plan.steps[0].tool_calls]


def test_retryable_failure_is_retried_and_succeeds(rt):
    rt.mock_control.set_fault("demo", "calendar.find_slots", times=1, retryable=True, message="calendar timed out")
    state = run(rt.orchestrator.submit(lead_task()))
    slots_step = next(s for s in state.plan.steps if s.action == "propose_slots")
    assert state.status == RunStatus.COMPLETED and slots_step.attempts == 2 and state.replans == 0
    assert "step.retry_scheduled" in [e["kind"] for e in rt.tracer.timeline("demo", state.run_id)]


def test_permanent_failure_triggers_replanning(rt):
    rt.mock_control.set_fault(
        "demo", "email.create_draft", times=99, retryable=False, message="mail server rejected it"
    )
    state = run(rt.orchestrator.submit(lead_task()))
    assert state.status == RunStatus.COMPLETED and state.replans == 1 and state.plan.version == 2
    actions = [s.action for s in state.plan.steps]
    assert actions == ["recall_context", "research_policy", "qualify_lead", "log_followup_task"]
    assert state.plan.steps[2].result["tier"] == "hot"  # completed work was carried over, not redone
    assert state.steps_executed == 5  # recall, policy, qualify, failed draft, follow-up
    notes = rt.db.kv_get("demo", "crm.contacts", EMAIL)["notes"]
    assert any("Manual follow-up needed" in n["text"] and "mail server" in n["text"] for n in notes)
    assert state.working_memory["_failures"][0]["action"] == "draft_reply"


def test_run_fails_cleanly_when_recovery_is_impossible(tmp_path):
    rt = started(make_settings(tmp_path, max_replans=0))
    rt.mock_control.set_fault("demo", "email.create_draft", times=99, retryable=False)
    state = run(rt.orchestrator.submit(lead_task()))
    assert state.status == RunStatus.FAILED and "draft_reply" in state.error
    assert rt.db.load_run("demo", state.run_id).status == RunStatus.FAILED  # failure is persisted
    rt.close()


def test_step_budget_stops_a_run(tmp_path):
    rt = started(make_settings(tmp_path, max_steps=2))
    state = run(rt.orchestrator.submit(lead_task()))
    # A five-step plan cannot be planned within a two-step limit; the template fallback is rejected too.
    assert state.status == RunStatus.FAILED and "limit" in state.error
    rt.close()


def test_pause_resume_and_cancel(rt):
    state = RunState(tenant_id="demo", task=lead_task())
    rt.db.save_run(state)
    rt.orchestrator.request_pause("demo", state.run_id)
    paused = run(rt.orchestrator.drive("demo", state.run_id))
    assert paused.status == RunStatus.PAUSED and paused.plan is not None and paused.steps_executed == 0
    resumed = run(rt.orchestrator.resume("demo", state.run_id))
    assert resumed.status == RunStatus.COMPLETED and resumed.final_report["outcome"] == "verified"
    assert run(rt.orchestrator.resume("demo", state.run_id)).status == RunStatus.COMPLETED  # terminal: no-op

    other = RunState(tenant_id="demo", task=lead_task())
    rt.db.save_run(other)
    rt.orchestrator.request_pause("demo", other.run_id)
    run(rt.orchestrator.drive("demo", other.run_id))
    assert rt.orchestrator.cancel("demo", other.run_id).status == RunStatus.CANCELLED
    assert run(rt.orchestrator.resume("demo", other.run_id)).status == RunStatus.CANCELLED


def test_approval_gate_suspends_and_resume_does_not_repeat_side_effects(rt):
    """The approval hook: a run stops before a gated call and continues after the gate allows it."""
    approved: set[str] = set()

    class Gate:
        def evaluate(self, state, step, agent, spec, call, fingerprint):
            if spec.name == "crm_add_note" and fingerprint not in approved:
                self.last = fingerprint
                return GateVerdict(
                    decision=GateDecision.REQUIRE_APPROVAL, reason="notes need review", approval_id="apr_x"
                )
            return GateVerdict(decision=GateDecision.ALLOW)

    gate = Gate()
    rt.gateway.gate = gate
    waiting = run(rt.orchestrator.submit(lead_task()))
    assert waiting.status == RunStatus.WAITING_APPROVAL and waiting.pending_approval_id == "apr_x"
    qualify = waiting.plan.steps[2]
    assert qualify.status == StepStatus.WAITING_APPROVAL and qualify.attempts == 1
    contact = rt.db.kv_get("demo", "crm.contacts", EMAIL)
    assert contact["notes"] == []  # the upsert ran; the gated note did not

    approved.add(gate.last)
    finished = run(rt.orchestrator.resume("demo", waiting.run_id))
    assert finished.status == RunStatus.COMPLETED and finished.final_report["outcome"] == "verified"
    qualify = finished.plan.steps[2]
    replayed = [t.tool for t in qualify.tool_calls]
    assert replayed.count("crm_upsert_contact") == 1  # executed once, replayed on the second pass
    assert len(rt.db.kv_get("demo", "crm.contacts", EMAIL)["notes"]) == 1


def test_run_survives_a_process_restart(tmp_path):
    """State lives in the database: a second runtime can finish what the first one started."""
    settings = make_settings(tmp_path)
    first = started(settings)
    state = RunState(tenant_id="demo", task=lead_task())
    first.db.save_run(state)
    first.orchestrator.request_pause("demo", state.run_id)
    run(first.orchestrator.drive("demo", state.run_id))
    first.close()

    second = started(settings)
    finished = run(second.orchestrator.resume("demo", state.run_id))
    assert finished.status == RunStatus.COMPLETED
    second.close()


def test_idempotency_key_prevents_duplicate_runs(rt):
    first = run(rt.orchestrator.submit(lead_task(idempotency_key="lead-123")))
    again = run(rt.orchestrator.submit(lead_task(idempotency_key="lead-123")))
    assert again.run_id == first.run_id and len(rt.db.list_runs("demo")) == 1
    assert len(rt.db.kv_list("demo", "email.drafts")) == 1


def test_tenants_do_not_see_each_others_data(rt):
    run(rt.orchestrator.submit(lead_task(tenant_id="acme")))
    other = run(rt.orchestrator.submit(lead_task(tenant_id="globex")))
    assert other.final_report["results"]["recall_context"]["known"] is False
    assert len(rt.db.kv_list("acme", "crm.contacts")) == 1 and len(rt.db.kv_list("globex", "crm.contacts")) == 1
    assert other.final_report["results"]["research_policy"]["grounded"] is False  # no documents for this tenant
    assert rt.db.list_runs("nobody") == []


def test_unmatched_objective_uses_the_general_workflow(rt):
    rt.memory.add_fact("demo", "Refund policy: refunds over 500 USD need a manager")
    state = run(rt.orchestrator.submit(Task(objective="What is our refund policy?")))
    assert state.analysis["workflow"] == "general" and state.status == RunStatus.COMPLETED
    assert "Refund policy" in state.final_report["results"]["recall_context"]["prior_knowledge"][0]


def test_planner_repairs_then_falls_back_when_the_model_returns_an_invalid_plan(rt):
    scripted = rt.llm

    class BadPlanner:
        name, model = "bad", "bad-1"

        async def complete(self, messages, tools=None):
            envelope = messages[1].content if len(messages) > 1 else ""
            if '"kind": "plan"' in envelope:
                return LLMResponse(text='{"steps": [{"agent": "sales_ops", "action": "launch_rocket"}]}')
            return await scripted.complete(messages, tools)

    rt.orchestrator.ctx.llm = BadPlanner()
    state = run(rt.orchestrator.submit(lead_task()))
    kinds = [e["kind"] for e in rt.tracer.timeline("demo", state.run_id)]
    assert kinds.count("plan.invalid") == 2 and "plan.fallback" in kinds
    assert state.status == RunStatus.COMPLETED and len(state.plan.steps) == 5


def test_handoff_moves_a_step_to_a_capable_agent(rt):
    from bizops.agents import AgentSpec

    rt.registry.register(
        AgentSpec(name="intake", role="front desk", system_prompt="x", allowed_tools=[], actions=["qualify_lead"])
    )
    rt.llm.register(
        "act:qualify_lead",
        (
            lambda original: (
                lambda env, res: (
                    {
                        "ok": False,
                        "summary": "not mine",
                        "handoff_to": "sales_ops",
                        "handoff_reason": "needs CRM access",
                    }
                    if env["agent"] == "intake"
                    else original(env, res)
                )
            )
        )(rt.workflows.get("lead_intake").scripts["qualify_lead"]),
    )
    workflow = rt.workflows.get("lead_intake")
    original_plan = workflow.plan
    workflow.plan = lambda task, analysis: [
        {**s, "agent": "intake"} if s["action"] == "qualify_lead" else s for s in original_plan(task, analysis)
    ]
    state = run(rt.orchestrator.submit(lead_task()))
    assert state.status == RunStatus.COMPLETED and state.plan.steps[2].agent == "sales_ops"
    assert "step.handoff" in [e["kind"] for e in rt.tracer.timeline("demo", state.run_id)]


def test_reference_resolution():
    state = RunState(tenant_id="demo", task=lead_task())
    state.working_memory["qualify_lead"] = {"tier": "hot", "nested": {"score": 85}}
    value = {
        "a": "$task.inputs.email",
        "b": ["$steps.qualify_lead.nested.score"],
        "c": "$steps.missing.x",
        "d": "$5 fee",
    }
    resolved = resolve_refs(value, state)
    assert resolved == {"a": EMAIL, "b": [85], "c": None, "d": "$5 fee"}
    assert PlanStep(agent="a", action="b", description="c").status == StepStatus.PENDING


def test_cli_demo_and_inspection_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BIZOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BIZOPS_LOG_LEVEL", "WARNING")
    from bizops.core.config import get_settings

    get_settings.cache_clear()
    assert cli_main(["demo"]) == 0
    out = capsys.readouterr().out
    assert "status=completed" in out and "Outcome: verified" in out
    run_id = out.split()[1]
    assert cli_main(["status", run_id]) == 0 and cli_main(["trace", run_id]) == 0
    assert cli_main(["runs"]) == 0 and cli_main(["tools"]) == 0 and cli_main(["agents"]) == 0
    assert "crm_upsert_contact" in capsys.readouterr().out
    assert cli_main(["demo", "collections"]) == 0 and cli_main(["kb", "search", "refund approval"]) == 0
    assert "Refund Policy" in capsys.readouterr().out
    assert cli_main(["status", "run_missing"]) == 1
    get_settings.cache_clear()


def test_unusable_input_fails_cleanly_without_side_effects(rt):
    """A lead with no email cannot be stored; the run must fail with a reason, not crash or write junk."""
    state = run(rt.orchestrator.submit(lead_task(inputs={"email": ""})))
    assert state.status == RunStatus.FAILED and "not a valid email" in state.error
    assert state.replans == 1  # recovery was attempted and also could not proceed
    assert len(rt.db.kv_list("demo", "crm.contacts")) == 5 and rt.db.kv_list("demo", "email.drafts") == {}


def test_model_outage_is_retried_then_recovered(rt):
    scripted, failures = rt.llm, {"n": 0}

    class Flaky:
        name, model = "flaky", "flaky-1"

        async def complete(self, messages, tools=None):
            if '"action": "draft_reply"' in messages[1].content and failures["n"] < 1:
                failures["n"] += 1
                raise ConnectionError("provider unavailable")
            return await scripted.complete(messages, tools)

    rt.orchestrator.ctx.llm = Flaky()
    state = run(rt.orchestrator.submit(lead_task()))
    draft = next(s for s in state.plan.steps if s.action == "draft_reply")
    assert state.status == RunStatus.COMPLETED and draft.attempts == 2 and state.replans == 0


def test_idempotency_race_is_resolved_by_the_unique_index(rt, monkeypatch):
    """If two submissions pass the lookup at once, the database constraint decides and no second run starts."""
    first = run(rt.orchestrator.submit(lead_task(idempotency_key="race-1")))
    real_find, calls = rt.db.find_run_by_idempotency_key, {"n": 0}

    def find_after_miss(tenant_id, key):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_find(tenant_id, key)  # first lookup misses, as in a race

    monkeypatch.setattr(rt.db, "find_run_by_idempotency_key", find_after_miss)
    second = run(rt.orchestrator.submit(lead_task(idempotency_key="race-1")))
    assert second.run_id == first.run_id and len(rt.db.list_runs("demo")) == 1
