import pytest
from conftest import lead_task, make_settings, run

from bizops.agents.base import parse_final
from bizops.core.config import Settings
from bizops.core.errors import ConfigurationError
from bizops.core.models import PlanStep, RiskLevel, RunState, ToolCall
from bizops.llm import LLMMessage, ScriptedLLM, ToolSchema, build_llm
from bizops.tools import ApprovalRequired, GateDecision, GateVerdict


def _state_step():
    return RunState(tenant_id="demo", task=lead_task()), PlanStep(
        agent="sales_ops", action="qualify_lead", description="x"
    )


ALLOWED = ["crm_find_contact", "crm_upsert_contact", "crm_add_note"]


def test_gateway_rejects_unknown_forbidden_and_invalid_calls(rt):
    state, step = _state_step()
    unknown = run(rt.gateway.execute(state, step, "sales_ops", ALLOWED, ToolCall(tool="wire_money")))
    forbidden = run(rt.gateway.execute(state, step, "sales_ops", ALLOWED, ToolCall(tool="email_create_draft")))
    invalid = run(
        rt.gateway.execute(
            state, step, "sales_ops", ALLOWED, ToolCall(tool="crm_upsert_contact", arguments={"name": 42})
        )
    )
    assert not unknown.ok and "unknown tool" in unknown.error
    assert not forbidden.ok and "not permitted" in forbidden.error
    assert not invalid.ok and "invalid arguments" in invalid.error
    assert step.tool_calls == []  # nothing was executed
    assert "email" in invalid.error and len(rt.db.kv_list("demo", "crm.contacts")) == 5  # seed data untouched


def test_gateway_replays_completed_calls_instead_of_repeating_them(rt):
    state, step = _state_step()
    upsert = ToolCall(tool="crm_upsert_contact", arguments={"email": "a@b.example", "name": "A"})
    run(rt.gateway.execute(state, step, "sales_ops", ALLOWED, upsert))
    note = {"email": "a@b.example", "note": "hello"}
    first = run(rt.gateway.execute(state, step, "sales_ops", ALLOWED, ToolCall(tool="crm_add_note", arguments=note)))
    again = run(rt.gateway.execute(state, step, "sales_ops", ALLOWED, ToolCall(tool="crm_add_note", arguments=note)))
    assert first.ok and not first.replayed and again.replayed
    assert len(rt.db.kv_get("demo", "crm.contacts", "a@b.example")["notes"]) == 1
    # The same call in a different step is a new action and does execute.
    other = PlanStep(agent="sales_ops", action="log_followup_task", description="y")
    run(rt.gateway.execute(state, other, "sales_ops", ALLOWED, ToolCall(tool="crm_add_note", arguments=note)))
    assert len(rt.db.kv_get("demo", "crm.contacts", "a@b.example")["notes"]) == 2


def test_tool_failure_becomes_a_structured_result(rt):
    state, step = _state_step()
    result = run(
        rt.gateway.execute(
            state,
            step,
            "sales_ops",
            ALLOWED,
            ToolCall(tool="crm_add_note", arguments={"email": "ghost@x.example", "note": "n"}),
        )
    )
    assert not result.ok and "no CRM contact" in result.error and result.fingerprint


def test_gate_can_deny_or_require_approval(rt):
    class Gate:
        def evaluate(self, state, step, agent, spec, call, fingerprint):
            if spec.risk == RiskLevel.READ:
                return GateVerdict(decision=GateDecision.ALLOW)
            if call.arguments["email"].startswith("blocked"):
                return GateVerdict(decision=GateDecision.DENY, reason="blocked address")
            return GateVerdict(
                decision=GateDecision.REQUIRE_APPROVAL, reason="writes need approval", approval_id="apr_1"
            )

    rt.gateway.gate = Gate()
    state, step = _state_step()
    denied = run(
        rt.gateway.execute(
            state,
            step,
            "sales_ops",
            ALLOWED,
            ToolCall(tool="crm_upsert_contact", arguments={"email": "blocked@x.example"}),
        )
    )
    assert not denied.ok and "denied" in denied.error
    with pytest.raises(ApprovalRequired) as caught:
        run(
            rt.gateway.execute(
                state,
                step,
                "sales_ops",
                ALLOWED,
                ToolCall(tool="crm_upsert_contact", arguments={"email": "ok@x.example"}),
            )
        )
    assert caught.value.verdict.approval_id == "apr_1" and caught.value.risk == RiskLevel.LOW
    assert rt.db.kv_get("demo", "crm.contacts", "ok@x.example") is None  # nothing written while approval is pending


def test_scripted_model_cannot_call_tools_it_was_not_offered():
    llm = ScriptedLLM()
    llm.register("act:x", lambda envelope, results: [ToolCall(tool="secret_tool")])
    messages = [LLMMessage(role="user", content='{"kind": "act", "action": "x"}')]
    offered = [ToolSchema(name="other", description="", input_schema={})]
    response = run(llm.complete(messages, offered))
    assert response.tool_calls == [] and "tool_not_offered" in response.text
    assert response.usage.calls == 1 and response.usage.cost_usd == 0


def test_scripted_model_reports_unknown_actions():
    response = run(ScriptedLLM().complete([LLMMessage(role="user", content='{"kind": "act", "action": "nope"}')]))
    assert parse_final(response.text)["error"] == "unsupported_action"


def test_parse_final_handles_fences_and_plain_text():
    assert parse_final('```json\n{"ok": true, "summary": "s"}\n```')["summary"] == "s"
    assert parse_final("just words") == {"ok": True, "summary": "just words", "data": {}}
    assert parse_final("")["ok"] is False


def test_provider_factory_validates_configuration(tmp_path):
    assert build_llm(make_settings(tmp_path)).name == "scripted"
    with pytest.raises(ConfigurationError, match="ANTHROPIC_API_KEY"):
        build_llm(make_settings(tmp_path, llm_provider="anthropic"))
    with pytest.raises(ConfigurationError, match="OPENAI_API_KEY"):
        build_llm(make_settings(tmp_path, llm_provider="openai"))
    with pytest.raises(ConfigurationError, match="Unknown"):
        build_llm(make_settings(tmp_path, llm_provider="mystery"))


def test_secrets_are_not_exposed_in_settings_repr(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, ANTHROPIC_API_KEY="sk-super-secret")
    assert "sk-super-secret" not in repr(settings) and "sk-super-secret" not in settings.model_dump_json()
