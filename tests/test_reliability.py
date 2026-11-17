"""Timeouts, retries with backoff, recovery of interrupted runs, and hardened model providers."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import approver, lead_task, make_settings, run, started
from fastapi.testclient import TestClient

from bizops.api import create_app
from bizops.core.errors import LLMError
from bizops.core.models import RunStatus, Task, ToolCall
from bizops.llm import LLMMessage, ResilientLLM, classify_provider_error
from bizops.llm.base import LLMResponse
from bizops.llm.providers import AnthropicProvider, OpenAICompatibleProvider

REFUND = Task(
    objective="Handle a refund request for a paid invoice",
    workflow="refund_request",
    inputs={"invoice_id": "INV-1005", "amount": 320},
)


def fast(tmp_path, **overrides):
    return started(make_settings(tmp_path, retry_base_seconds=0.01, retry_max_seconds=0.02, **overrides))


def kinds(rt, run_id):
    return [e["kind"] for e in rt.tracer.timeline("demo", run_id)]


# Tool timeouts -----------------------------------------------------------------
@pytest.mark.parametrize("lands", [False, True], ids=["timed-out-before-effect", "effect-landed-then-timed-out"])
def test_tool_timeout_is_retried_under_the_same_key_and_the_effect_happens_once(tmp_path, lands):
    rt = fast(tmp_path, tool_timeout_seconds=0.2)
    state = run(rt.orchestrator.submit(REFUND))
    rt.approvals.decide(approver(rt), state.pending_approval_id, True)
    real_call, slow = rt.mcp.call, {"left": 1}

    async def flaky(name, arguments, ctx):
        if name == "billing_issue_refund" and slow["left"]:
            slow["left"] -= 1
            if lands:
                await real_call(name, arguments, ctx)  # the refund is issued, but the answer is late
            await asyncio.sleep(1)
        return await real_call(name, arguments, ctx)

    rt.mcp.call = flaky
    done = run(rt.orchestrator.resume("demo", state.run_id))
    assert done.status == RunStatus.COMPLETED and done.final_report["outcome"] == "verified"
    assert len(rt.db.kv_list("demo", "billing.refunds")) == 1
    issue = next(s for s in done.plan.steps if s.action == "issue_refund")
    assert (
        issue.attempts == 2 and issue.in_flight is None
    )  # timed out once, then succeeded (the wait is not an attempt)
    assert done.final_report["unresolved_side_effects"] == []
    timeouts = [t for t in issue.tool_calls if t.error and "timed out" in t.error]
    assert len(timeouts) == 1 and timeouts[0].retryable
    assert "step.retry_scheduled" in kinds(rt, state.run_id)
    rt.close()


# Step timeouts and backoff -------------------------------------------------------
class SlowOnce:
    name, model = "slow", "slow-1"

    def __init__(self, inner, action):
        self.inner, self.action, self.done = inner, action, False

    async def complete(self, messages, tools=None):
        envelope = json.loads(messages[1].content) if len(messages) > 1 else {}
        if not self.done and envelope.get("action") == self.action:
            self.done = True
            await asyncio.sleep(5)
        return await self.inner.complete(messages, tools)


def test_a_step_that_hangs_is_cut_off_and_retried_with_backoff(tmp_path):
    rt = fast(tmp_path, step_timeout_seconds=0.3)
    rt.orchestrator.ctx.llm = SlowOnce(rt.llm, "qualify_lead")
    state = run(rt.orchestrator.submit(lead_task()))
    assert state.status == RunStatus.COMPLETED
    timeline = rt.tracer.timeline("demo", state.run_id)
    assert [e["kind"] for e in timeline].count("step.timed_out") == 1
    retry = next(e for e in timeline if e["kind"] == "step.retry_scheduled")
    assert retry["step"] == "qualify_lead" and 0 < retry["delay_s"] <= 0.02
    rt.close()


def test_backoff_delay_is_applied_and_recorded(tmp_path):
    rt = started(make_settings(tmp_path, retry_base_seconds=2.0, retry_max_seconds=3.0))
    delays = []

    async def record(seconds):
        delays.append(seconds)

    rt.orchestrator.backoff_sleep = record
    rt.mock_control.set_fault("demo", "crm.find_contact", times=1, retryable=True)
    state = run(rt.orchestrator.submit(lead_task()))
    assert state.status == RunStatus.COMPLETED
    assert len(delays) == 1 and 1.0 <= delays[0] <= 2.0  # first retry: base, with jitter in the upper half
    events = [e for e in rt.tracer.timeline("demo", state.run_id) if e["kind"] == "step.retry_scheduled"]
    assert events[0]["delay_s"] == round(delays[0], 3)
    rt.close()


# Recovery --------------------------------------------------------------------
def test_side_effect_with_unknown_outcome_is_reported_not_hidden(tmp_path):
    """If retries run out while a call's outcome is unknown, the report says so instead of claiming success."""
    rt = fast(tmp_path, tool_timeout_seconds=0.1)
    state = run(rt.orchestrator.submit(REFUND))
    rt.approvals.decide(approver(rt), state.pending_approval_id, True)
    real_call = rt.mcp.call

    async def always_late(name, arguments, ctx):
        if name == "billing_issue_refund":
            await real_call(name, arguments, ctx)  # it lands every time, but never answers in time
            await asyncio.sleep(1)
        return await real_call(name, arguments, ctx)

    rt.mcp.call = always_late
    done = run(rt.orchestrator.resume("demo", state.run_id))
    assert len(rt.db.kv_list("demo", "billing.refunds")) == 1  # the idempotency key held across attempts
    assert done.final_report["outcome"] == "completed_with_issues"
    [unknown] = done.final_report["unresolved_side_effects"]
    assert unknown["tool"] == "billing_issue_refund" and unknown["arguments"]["invoice_id"] == "INV-1005"
    assert "tool.intent_unresolved" in kinds(rt, state.run_id)
    rt.close()


def test_runs_left_running_by_a_dead_worker_are_recovered(tmp_path):
    rt = started(make_settings(tmp_path))
    state, _ = rt.orchestrator.create(lead_task())
    # A worker claimed the run, started it, and died without releasing.
    generation = rt.db.claim_run("demo", state.run_id, "dead-worker", 60)
    state.generation, state.status = generation, RunStatus.RUNNING
    rt.db.save_run(state, fence=("dead-worker", generation))
    assert rt.db.list_interrupted_runs() == []  # still leased: could be a slow worker, not touched
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="milliseconds")
    rt.db.execute("UPDATE runs SET lease_expires_at=? WHERE run_id=?", (past, state.run_id))
    assert rt.db.list_interrupted_runs() == [("demo", state.run_id)]
    assert run(rt.orchestrator.recover_interrupted()) == [state.run_id]
    final = rt.db.load_run("demo", state.run_id)
    assert final.status == RunStatus.COMPLETED and final.generation == 2
    assert "run.recovery_started" in kinds(rt, state.run_id) and rt.db.list_interrupted_runs() == []
    rt.close()


def test_server_start_recovers_interrupted_runs(tmp_path):
    rt = started(make_settings(tmp_path))
    state, _ = rt.orchestrator.create(lead_task())
    generation = rt.db.claim_run("demo", state.run_id, "dead-worker", 60)
    state.generation, state.status = generation, RunStatus.RUNNING
    rt.db.save_run(state, fence=("dead-worker", generation))
    rt.db.release_lease("demo", state.run_id, "dead-worker", generation)
    with TestClient(create_app(rt, run_worker=False), client=("127.0.0.1", 1)) as c:
        for _ in range(100):
            if c.get(f"/api/runs/{state.run_id}").json()["terminal"]:
                break
            asyncio.run(asyncio.sleep(0.05))
        assert c.get(f"/api/runs/{state.run_id}").json()["status"] == "completed"
    rt.close()


# Run control over the API ---------------------------------------------------
def test_approval_and_controls_over_the_api(rt):
    with TestClient(create_app(rt, run_worker=False), client=("127.0.0.1", 1)) as c:
        partial = {"inputs": {"invoice_id": "INV-1005", "amount": 100, "reason": "partial"}}
        run_id = c.post("/api/workflows/refund_request/start", json=partial).json()["run_id"]
        for _ in range(100):
            detail = c.get(f"/api/runs/{run_id}").json()
            if detail["status"] == "waiting_approval" and not detail["being_executed"]:
                break
            asyncio.run(asyncio.sleep(0.05))
        assert detail["controls"] == {"pause": False, "resume": False, "cancel": True}
        [pending] = detail["approvals"]
        assert c.post(f"/api/runs/{run_id}/resume").status_code == 409  # a decision is required first
        queue = c.get("/api/approvals?status=pending").json()
        assert queue["counts"]["pending"] == 1 and queue["items"][0]["can_decide"]
        assert queue["items"][0]["run"]["step_description"].startswith("Issue the refund")
        decided = c.post(f"/api/approvals/{pending['approval_id']}/decision", json={"approve": True, "comment": "ok"})
        assert decided.status_code == 200 and decided.json()["status"] == "approved"
        for _ in range(100):
            detail = c.get(f"/api/runs/{run_id}").json()
            if detail["terminal"]:
                break
            asyncio.run(asyncio.sleep(0.05))
        assert detail["status"] == "completed" and detail["controls"]["cancel"] is False
        assert c.post(f"/api/runs/{run_id}/cancel").status_code == 409

        second = c.post("/api/workflows/refund_request/start", json=partial).json()["run_id"]
        for _ in range(100):
            if c.get(f"/api/runs/{second}").json()["status"] == "waiting_approval":
                break
            asyncio.run(asyncio.sleep(0.05))
        cancelled = c.post(f"/api/runs/{second}/cancel").json()
        assert cancelled["status"] == "cancelled" and cancelled["cancelled"]
        assert c.get(f"/api/approvals?run_id={second}").json()["items"][0]["status"] == "cancelled"
        verify = c.get("/api/audit/verify").json()
        assert verify["ok"] and verify["records"] >= 6
        security = c.get("/api/security").json()
        assert security["access"]["mode"] == "local_only" and security["audit"]["ok"]
        policy = c.get("/api/policy").json()
        assert policy["is_default"] and policy["editable"]
        assert {t["name"]: t["clean_run"] for t in policy["tools"]}["email_send"] == "require_approval"
        assert c.put("/api/policy", json={"rules": [{"name": "Bad Name!", "decision": "allow"}]}).status_code == 422
    assert len(rt.db.kv_list("demo", "billing.refunds")) == 1


def test_pause_and_resume_over_the_api(rt):
    with TestClient(create_app(rt, run_worker=False), client=("127.0.0.1", 1)) as c:
        state, _ = rt.orchestrator.create(lead_task())
        assert c.post(f"/api/runs/{state.run_id}/pause").json()["pause_requested"]
        paused = run(rt.orchestrator.drive("demo", state.run_id))
        assert paused.status == RunStatus.PAUSED
        assert c.get(f"/api/runs/{state.run_id}").json()["controls"]["resume"]
        assert c.post(f"/api/runs/{state.run_id}/resume").status_code == 202
        for _ in range(100):
            if c.get(f"/api/runs/{state.run_id}").json()["terminal"]:
                break
            asyncio.run(asyncio.sleep(0.05))
        assert c.get(f"/api/runs/{state.run_id}").json()["status"] == "completed"
    actions = [e["action"] for e in rt.audit.list("demo")]
    assert "run.pause_requested" in actions and "run.resume_requested" in actions


# Model providers --------------------------------------------------------------
class ProviderError(Exception):
    def __init__(self, status, headers=None, code=None):
        super().__init__(f"status {status}")
        self.status_code, self.response = status, SimpleNamespace(headers=headers or {})
        self.body = {"error": {"code": code}} if code else None


class Flaky:
    name, model = "fake", "fake-1"

    def __init__(self, errors):
        self.errors, self.calls = list(errors), 0

    async def complete(self, messages, tools=None):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return LLMResponse(text="ok", model=self.model)


def test_classification_of_provider_errors():
    assert classify_provider_error(ProviderError(429, {"retry-after": "2"}), "p").retry_after == 2.0
    assert classify_provider_error(ProviderError(429, {"retry-after-ms": "1500"}), "p").retry_after == 1.5
    assert classify_provider_error(ProviderError(503), "p").retryable
    assert classify_provider_error(TimeoutError(), "p").retryable
    for error in (
        ProviderError(401),
        ProviderError(404),
        ProviderError(400),
        ProviderError(429, code="insufficient_quota"),
    ):
        assert not classify_provider_error(error, "p").retryable
    message = str(classify_provider_error(ProviderError(401), "openai"))
    assert "authentication failed" in message and "status" not in message.lower().split("failed")[0]


def test_resilient_wrapper_retries_what_can_succeed_and_stops_on_what_cannot():
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    flaky = Flaky([ProviderError(429, {"retry-after": "2"}), ProviderError(503)])
    llm = ResilientLLM(flaky, max_retries=4, max_delay_seconds=30, sleep=sleep)
    assert run(llm.complete([LLMMessage(role="user", content="hi")])).text == "ok"
    assert flaky.calls == 3 and waits[0] == 2.0 and 0.5 <= waits[1] <= 2.0
    assert llm.stats == {"calls": 1, "retries": 2, "rate_limited": 1, "failures": 0}

    denied = ResilientLLM(Flaky([ProviderError(401)]), sleep=sleep)
    with pytest.raises(LLMError, match="authentication"):
        run(denied.complete([]))
    assert denied.inner.calls == 1  # never retried

    exhausted = ResilientLLM(Flaky([ProviderError(503)] * 5), max_retries=2, sleep=sleep)
    with pytest.raises(LLMError) as info:
        run(exhausted.complete([]))
    assert info.value.retryable and exhausted.inner.calls == 3


def _openai(response):
    async def create(**kwargs):
        create.kwargs = kwargs
        return response

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return OpenAICompatibleProvider("unused", "test-model", client=client), create


def _choice(content=None, tool_calls=None, finish="stop", refusal=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls, refusal=refusal)
    return SimpleNamespace(message=message, finish_reason=finish)


def _tool(name, arguments):
    return SimpleNamespace(id="c1", function=SimpleNamespace(name=name, arguments=arguments))


def test_openai_responses_are_validated_before_use():
    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20)
    ok, create = _openai(
        SimpleNamespace(
            choices=[_choice(tool_calls=[_tool("crm_find_contact", '{"email": "a@b.example"}')])],
            usage=usage,
            model="test-model",
        )
    )
    from bizops.llm.base import ToolSchema

    response = run(
        ok.complete(
            [LLMMessage(role="user", content="find")],
            [ToolSchema(name="crm_find_contact", description="d", input_schema={"type": "object"})],
        )
    )
    assert response.tool_calls == [ToolCall(call_id="c1", tool="crm_find_contact", arguments={"email": "a@b.example"})]
    assert response.usage.input_tokens == 100 and response.usage.calls == 1 and response.usage.cost_usd > 0
    assert create.kwargs["tools"][0]["function"]["name"] == "crm_find_contact"

    bad = [
        (SimpleNamespace(choices=[], usage=usage), "no choices", True),
        (
            SimpleNamespace(choices=[_choice(content="x", finish="content_filter")], usage=usage),
            "content filter",
            False,
        ),
        (SimpleNamespace(choices=[_choice(refusal="no")], usage=usage), "refused", False),
        (SimpleNamespace(choices=[_choice(tool_calls=[_tool("t", "{not json")])], usage=usage), "not valid JSON", True),
        (SimpleNamespace(choices=[_choice(tool_calls=[_tool("t", "[1, 2]")])], usage=usage), "not an object", True),
        (
            SimpleNamespace(choices=[_choice(tool_calls=[_tool("t", '{"a"')], finish="length")], usage=usage),
            "cut off",
            True,
        ),
        (SimpleNamespace(choices=[_choice(content="  ")], usage=usage), "empty", True),
    ]
    for payload, message, retryable in bad:
        provider, _ = _openai(payload)
        with pytest.raises(LLMError, match=message) as info:
            run(provider.complete([LLMMessage(role="user", content="x")]))
        assert info.value.retryable is retryable, message

    no_usage, _ = _openai(SimpleNamespace(choices=[_choice(content="hello there")], usage=None))
    estimated = run(no_usage.complete([LLMMessage(role="user", content="x" * 400)]))
    assert estimated.usage.input_tokens > 0 and estimated.usage.output_tokens > 0  # budgets still apply


def test_anthropic_cut_off_tool_call_is_rejected():
    block = SimpleNamespace(type="tool_use", id="t1", name="crm_find_contact", input={"email": "a"})
    response = SimpleNamespace(
        content=[block], stop_reason="max_tokens", usage=SimpleNamespace(input_tokens=1, output_tokens=1)
    )

    async def create(**kwargs):
        return response

    provider = AnthropicProvider("unused", client=SimpleNamespace(messages=SimpleNamespace(create=create)))
    with pytest.raises(LLMError, match="cut off"):
        run(provider.complete([LLMMessage(role="user", content="x")]))


def test_agent_turns_a_permanent_model_error_into_a_failed_step(rt):
    class Broken:
        name, model = "broken", "broken-1"

        async def complete(self, messages, tools=None):
            raise LLMError("openai: authentication failed; check the provider API key and its permissions")

    rt.orchestrator.ctx.llm = Broken()
    state = run(
        rt.orchestrator.submit(
            Task(objective="Handle a new inbound lead", workflow="lead_intake", inputs={"name": "x"})
        )
    )
    assert state.status == RunStatus.FAILED
    assert "authentication failed" in json.dumps(state.model_dump(mode="json"))
