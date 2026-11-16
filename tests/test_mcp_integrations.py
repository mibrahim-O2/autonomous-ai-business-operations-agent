"""MCP server and client, mock integrations, and the HTTP integration framework."""

import json
import sys

import httpx
import pytest
from conftest import make_settings, run
from mcp import StdioServerParameters

from bizops.core.errors import ToolError
from bizops.core.models import PlanStep, RiskLevel, RunState, Task, ToolCall
from bizops.integrations import IntegrationError
from bizops.integrations.http import CircuitBreaker, HttpIntegration, RestCRMAdapter
from bizops.tools.base import ToolContext
from bizops.tools.mcp_executor import MCPToolExecutor


def _ctx(rt, tenant="demo", key=""):
    return ToolContext(tenant_id=tenant, run_id="run_test", db=rt.db, idempotency_key=key)


def test_discovery_maps_mcp_tools_to_specs(rt):
    specs = {s.name: s for s in rt.mcp.specs()}
    assert len(specs) == 16 and all(s.source == "mcp:business" for s in specs.values())
    assert specs["crm_find_contact"].risk == RiskLevel.READ and not specs["crm_find_contact"].side_effect
    assert specs["email_send"].risk == RiskLevel.HIGH and specs["billing_issue_refund"].risk == RiskLevel.HIGH
    assert (
        specs["crm_merge_contacts"].risk == RiskLevel.HIGH and specs["calendar_create_event"].risk == RiskLevel.MEDIUM
    )
    assert specs["email_read_message"].returns_untrusted and not specs["email_list_inbox"].returns_untrusted
    # Platform-controlled parameters are hidden from the model.
    for spec in specs.values():
        assert "tenant_id" not in spec.input_schema["properties"]
        assert "idempotency_key" not in spec.input_schema["properties"]
    assert rt.mcp.server_info["prompts"] == ["collections_review", "inbox_triage"]
    assert "bizops://{tenant_id}/crm/contacts" in rt.mcp.server_info["resource_templates"]


def test_resources_and_prompts_are_served(rt):
    contacts = json.loads(run(rt.mcp.read_resource("bizops://demo/crm/contacts")))
    overdue = json.loads(run(rt.mcp.read_resource("bizops://demo/billing/invoices/overdue")))
    assert len(contacts) == 5 and {i["invoice_id"] for i in overdue} == {"INV-1001", "INV-1002", "INV-1003"}
    assert json.loads(run(rt.mcp.read_resource("bizops://nobody/crm/contacts"))) == []
    inbox = json.loads(run(rt.mcp.read_resource("bizops://demo/email/inbox")))
    assert all("body" not in message for message in inbox)  # bodies only through the untrusted-flagged tool
    assert "collections policy" in run(rt.mcp.get_prompt("collections_review", {"max_invoices": "5"}))


def test_model_cannot_choose_the_tenant(rt):
    """A tenant_id supplied in tool arguments is discarded; the run's tenant is used."""
    found = run(rt.mcp.call("crm_search_contacts", {"query": "", "tenant_id": "demo"}, _ctx(rt, tenant="globex")))
    assert found == {"contacts": []}
    assert len(run(rt.mcp.call("crm_search_contacts", {"query": ""}, _ctx(rt)))["contacts"]) == 5


def test_idempotency_key_prevents_a_repeated_side_effect(rt):
    args = {"email": "jon.mercer@bluepeak.example", "note": "called the customer"}
    first = run(rt.mcp.call("crm_add_note", args, _ctx(rt, key="k1")))
    again = run(rt.mcp.call("crm_add_note", args, _ctx(rt, key="k1")))
    other = run(rt.mcp.call("crm_add_note", args, _ctx(rt, key="k2")))
    assert first == again == {"notes": 1} and other == {"notes": 2}
    assert len(rt.db.kv_get("demo", "crm.contacts", "jon.mercer@bluepeak.example")["notes"]) == 2


def test_integration_errors_cross_mcp_with_their_retry_flag(rt):
    rt.mock_control.set_fault("demo", "billing.list_invoices", times=1, retryable=True)
    with pytest.raises(ToolError) as transient:
        run(rt.mcp.call("billing_list_invoices", {}, _ctx(rt)))
    assert transient.value.retryable
    with pytest.raises(ToolError) as permanent:
        run(rt.mcp.call("billing_get_invoice", {"invoice_id": "INV-0"}, _ctx(rt)))
    assert not permanent.value.retryable and "no invoice" in str(permanent.value)


def test_gateway_validates_mcp_arguments_against_json_schema(rt):
    state = RunState(tenant_id="demo", task=Task(objective="test task"))
    step = PlanStep(agent="finance_ops", action="send_reminders", description="x")
    bad_type = run(
        rt.gateway.execute(
            state,
            step,
            "finance_ops",
            ["billing_*"],
            ToolCall(
                tool="billing_issue_refund", arguments={"invoice_id": "INV-1005", "amount": "lots", "reason": "r"}
            ),
        )
    )
    missing = run(rt.gateway.execute(state, step, "finance_ops", ["billing_*"], ToolCall(tool="billing_issue_refund")))
    outside = run(rt.gateway.execute(state, step, "finance_ops", ["billing_*"], ToolCall(tool="email_send")))
    assert not bad_type.ok and "invalid arguments" in bad_type.error and "amount" in bad_type.error
    assert not missing.ok and "required" in missing.error
    assert not outside.ok and "not permitted" in outside.error  # pattern allow-list excludes other tools
    assert rt.db.kv_list("demo", "billing.refunds") == {}


def test_tool_selection_offers_the_most_relevant_tools(rt):
    everything = rt.gateway.specs_for(["*"])
    chosen = rt.gateway.specs_for(["*"], "send a payment reminder for an overdue invoice", 3)
    assert len(everything) == 19 and len(chosen) == 3 and chosen[0].name == "billing_send_reminder"
    assert [s.name for s in rt.gateway.specs_for(["calendar_*"])] == [
        "calendar_find_slots", "calendar_create_event", "calendar_cancel_event"]  # fmt: skip


def test_mock_billing_enforces_refund_invariants(rt):
    billing = rt.integrations.billing
    with pytest.raises(IntegrationError, match="only paid invoices"):
        billing.issue_refund("demo", "INV-1001", 10, "x")
    with pytest.raises(IntegrationError, match="exceeds refundable balance"):
        billing.issue_refund("demo", "INV-1005", 5000, "x")
    billing.issue_refund("demo", "INV-1005", 300, "partial")
    with pytest.raises(IntegrationError, match="exceeds refundable balance"):
        billing.issue_refund("demo", "INV-1005", 21, "too much")
    assert billing.get_invoice("demo", "INV-1005")["refunded"] == 300


def test_mock_crm_merge_and_calendar_conflict(rt):
    crm, calendar = rt.integrations.crm, rt.integrations.calendar
    crm.add_note("demo", "a.hart@harborline.example", "duplicate note", "tester")
    merged = crm.merge_contacts("demo", "amelia.hart@harborline.example", "a.hart@harborline.example")
    assert merged["notes"][0]["text"] == "duplicate note"
    assert crm.find_contact("demo", "a.hart@harborline.example")["merged_into"] == "amelia.hart@harborline.example"
    assert len(crm.search_contacts("demo", "")) == 4  # archived duplicate no longer listed
    with pytest.raises(IntegrationError, match="already archived"):
        crm.merge_contacts("demo", "amelia.hart@harborline.example", "a.hart@harborline.example")
    slots = calendar.find_slots("demo", None, 7, 30)
    assert slots[0]["start"] == "2026-11-17T14:00"  # 10:00 on that day is taken by the seeded stand-up
    calendar.create_event("demo", "Intro", slots[0]["start"], 30, [])
    with pytest.raises(IntegrationError, match="already booked"):
        calendar.create_event("demo", "Clash", slots[0]["start"], 30, [])


def test_mcp_server_works_over_stdio(tmp_path):
    """The same server runs as a separate process for any MCP host."""
    env = {"BIZOPS_DATA_DIR": str(tmp_path), "BIZOPS_LOG_LEVEL": "WARNING"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "bizops.mcp_server.server"], env=env)
    executor = MCPToolExecutor("stdio", params)
    specs = run(executor.discover())
    assert len(specs) == 16

    class Ctx:
        tenant_id, run_id, idempotency_key = "demo", "r", ""

    found = run(executor.call("crm_find_contact", {"email": "jon.mercer@bluepeak.example"}, Ctx))
    assert found["found"] and found["contact"]["company"] == "Bluepeak Consulting"


# HTTP integration framework ---------------------------------------------------
async def _no_sleep(_seconds):
    return None


def _http(handler, **kwargs):
    return HttpIntegration(
        "https://crm.test", token="t", transport=httpx.MockTransport(handler), sleep=_no_sleep, **kwargs
    )


def test_http_retries_transient_failures_and_sends_idempotency_key():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(503) if len(seen) < 3 else httpx.Response(200, json={"ok": True})

    result = run(_http(handler).request("PUT", "/x", json={"a": 1}, idempotency_key="key-1"))
    assert result == {"ok": True} and len(seen) == 3
    assert all(r.headers["Idempotency-Key"] == "key-1" and r.headers["Authorization"] == "Bearer t" for r in seen)


def test_http_does_not_retry_client_errors():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(422, text="bad field")

    with pytest.raises(IntegrationError) as caught:
        run(_http(handler).request("POST", "/x", json={}))
    assert len(calls) == 1 and not caught.value.retryable and "422" in str(caught.value)


def test_http_gives_up_after_max_attempts_and_handles_timeouts():
    def handler(request):
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(IntegrationError) as caught:
        run(_http(handler, max_attempts=2).request("GET", "/x"))
    assert caught.value.retryable and "after 2 attempts" in str(caught.value)


def test_circuit_breaker_opens_then_allows_a_trial_call():
    now = {"t": 0.0}
    breaker = CircuitBreaker(threshold=2, reset_seconds=30, clock=lambda: now["t"])
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500) if len(calls) <= 2 else httpx.Response(200, json={})

    http = _http(handler, max_attempts=2, breaker=breaker)
    with pytest.raises(IntegrationError):
        run(http.request("GET", "/x"))
    assert breaker.state == "open"
    with pytest.raises(IntegrationError, match="circuit breaker is open"):
        run(http.request("GET", "/x"))
    assert len(calls) == 2  # the open breaker did not call the failing service
    now["t"] = 31.0
    assert breaker.state == "half_open"
    run(http.request("GET", "/x"))
    assert breaker.state == "closed"


def test_rest_crm_adapter_maps_requests_and_missing_records():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/ghost@x.example"):
            return httpx.Response(404, text="not found")
        if request.method == "PUT":
            return httpx.Response(200, json={"email": "a@x.example", **json.loads(request.content)})
        return httpx.Response(200, json=[{"email": "a@x.example"}])

    adapter = RestCRMAdapter(_http(handler))
    assert run(adapter.find_contact("acme", "Ghost@x.example")) is None
    assert run(adapter.upsert_contact("acme", "a@x.example", {"name": "A"}, "k"))["name"] == "A"
    assert run(adapter.search_contacts("acme", "a")) == [{"email": "a@x.example"}]


def test_settings_keep_http_surfaces_disabled_by_default(tmp_path):
    settings = make_settings(tmp_path)
    assert settings.api_key is None and settings.webhook_secret is None


def test_reminder_retry_does_not_email_the_customer_twice(rt):
    """Sending and recording a reminder are one transaction: a failure half-way leaves nothing behind."""
    rt.mock_control.set_fault("demo", "billing.record_reminder", times=1, retryable=True)
    with pytest.raises(ToolError) as first:
        run(rt.mcp.call("billing_send_reminder", {"invoice_id": "INV-1001"}, _ctx(rt, key="rem-1")))
    assert first.value.retryable and rt.db.kv_list("demo", "email.sent") == {}  # the send was rolled back
    result = run(rt.mcp.call("billing_send_reminder", {"invoice_id": "INV-1001"}, _ctx(rt, key="rem-1")))
    again = run(rt.mcp.call("billing_send_reminder", {"invoice_id": "INV-1001"}, _ctx(rt, key="rem-1")))
    assert result == again and result["reminders_sent"] == 1 and len(rt.db.kv_list("demo", "email.sent")) == 1
