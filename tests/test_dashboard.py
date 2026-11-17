"""Dashboard API contract, access control, and static asset safety."""

import json
import re
import time
from pathlib import Path

from conftest import make_settings, run, started, submit_approved
from fastapi.testclient import TestClient

from bizops.api import create_app
from bizops.core.models import Task

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboard"
LOCAL = ("127.0.0.1", 50000)


def _client(rt, client=LOCAL):
    return TestClient(create_app(rt, run_worker=False), client=client)


def _wait_for(client, run_id, timeout=10.0, until=None):
    """Poll a run until it is terminal, or until it reaches the status named by `until`."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        detail = client.get(f"/api/runs/{run_id}").json()
        if detail["terminal"] or (until and detail["status"] == until and not detail["being_executed"]):
            return detail
        time.sleep(0.05)
    raise AssertionError("run did not finish in time")


def test_overview_agents_and_workflows_reflect_real_state(rt):
    # send_reminders runs twice: once until it reaches the approval, once after the decision.
    submit_approved(rt, Task(objective="Review overdue invoices", workflow="invoice_collections"))
    with _client(rt) as client:
        meta = client.get("/api/meta").json()
        assert meta["mode"] == "live" and meta["llm"] == {
            "provider": "scripted",
            "model": "scripted-v1",
            "live_model": False,
        }
        overview = client.get("/api/overview").json()
        assert overview["totals"]["runs"] == 1 and overview["totals"]["success_rate"] == 1.0
        assert (
            overview["by_workflow"][0]["workflow"] == "invoice_collections" and overview["usage"]["model_calls"] == 10
        )
        assert overview["activity"][0]["kind"] == "run.completed"
        agents = {a["name"]: a for a in client.get("/api/agents").json()}
        assert set(agents) == {"research", "sales_ops", "communications", "scheduling", "finance_ops"}
        finance_tools = {t["name"] for t in agents["finance_ops"]["tools"]}
        assert "billing_send_reminder" in finance_tools and "email_send" not in finance_tools
        assert agents["finance_ops"]["steps_executed"] == 3 and agents["scheduling"]["steps_executed"] == 0
        workflows = {w["name"]: w for w in client.get("/api/workflows").json()}
        assert [s["action"] for s in workflows["invoice_collections"]["steps"]] == [
            "research_policy",
            "review_overdue",
            "send_reminders",
        ]
        assert workflows["invoice_collections"]["runs"] == {"completed": 1} and workflows["general"]["scenario"] is None


def test_starting_a_workflow_executes_a_real_run(rt):
    with _client(rt) as client:
        started_run = client.post("/api/workflows/inbox_triage/start", json={})
        assert started_run.status_code == 202
        detail = _wait_for(client, started_run.json()["run_id"])
        assert detail["status"] == "completed" and detail["tainted"] and detail["requested_by"] == "local-operator"
        assert [s["action"] for s in detail["steps"]] == ["triage_inbox", "draft_responses"]
        read_calls = [t for t in detail["steps"][0]["tool_calls"] if t["tool"] == "email_read_message"]
        assert len(read_calls) == 3 and all(t["untrusted"] for t in read_calls)
        assert detail["final_report"]["outcome"] == "verified" and "triage_inbox" in detail["working_memory"]
        timeline = client.get(f"/api/runs/{detail['run_id']}/timeline").json()
        assert timeline[0]["kind"] == "run.created" and timeline[-1]["kind"] in ("run.completed", "run.drive")
        assert client.get("/api/runs?status=completed").json()[0]["run_id"] == detail["run_id"]
        assert client.get("/api/runs?status=failed").json() == [] and client.get("/api/runs?q=nomatch").json() == []
        assert client.get("/api/runs?workflow=inbox_triage").json()[0]["workflow"] == "inbox_triage"
        assert client.post("/api/workflows/nope/start", json={}).status_code == 404
        assert client.get("/api/runs/run_missing").status_code == 404
    assert len(rt.db.kv_list("demo", "email.drafts")) == 2  # the run changed real state


def test_disabling_an_agent_blocks_plans_that_need_it(rt):
    with _client(rt) as client:
        assert client.post("/api/agents/finance_ops/enabled", json={"enabled": False}).json() == {
            "name": "finance_ops",
            "enabled": False,
        }
        blocked = _wait_for(client, client.post("/api/workflows/invoice_collections/start", json={}).json()["run_id"])
        assert blocked["status"] == "failed" and "disabled" in blocked["error"] and "finance_ops" in blocked["error"]
        assert rt.db.kv_list("demo", "email.sent") == {}
        client.post("/api/agents/finance_ops/enabled", json={"enabled": True})
        allowed = _wait_for(
            client,
            client.post("/api/workflows/invoice_collections/start", json={}).json()["run_id"],
            until="waiting_approval",
        )
        # With the agent enabled again the plan proceeds, as far as the reminder that needs a person.
        assert allowed["status"] == "waiting_approval" and allowed["steps"][1]["status"] == "succeeded"
        assert client.post("/api/agents/ghost/enabled", json={"enabled": False}).status_code == 404
        assert client.post("/api/agents/research/enabled", json={"enabled": "maybe"}).status_code == 422


def test_tools_catalogue_and_execution_history(rt):
    run(rt.orchestrator.submit(Task(objective="Triage the unread inbox", workflow="inbox_triage")))
    with _client(rt) as client:
        catalog = client.get("/api/tools").json()
        servers = {s["source"]: s for s in catalog["servers"]}
        assert servers["mcp:business"]["tools"] == 16 and servers["mcp:business"]["prompts"] == [
            "collections_review",
            "inbox_triage",
        ]
        assert servers["local"]["tools"] == 3
        tools = {t["name"]: t for t in catalog["tools"]}
        assert tools["email_send"]["risk"] == "high" and tools["email_send"]["agents"] == ["communications"]
        assert "tenant_id" not in tools["email_send"]["parameters"] and "to" in tools["email_send"]["required"]
        assert tools["email_read_message"]["returns_untrusted"] and not tools["kb_search"]["side_effect"]
        calls = client.get("/api/tool-calls").json()
        assert {c["tool"] for c in calls} >= {
            "email_list_inbox",
            "email_read_message",
            "email_create_draft",
            "ops_create_task",
        }
        assert all(c["ok"] and c["outcome"] == "executed" and c["run_id"] for c in calls)


def test_knowledge_and_memory_endpoints(rt):
    run(
        rt.orchestrator.submit(
            Task(
                objective="lead",
                workflow="lead_intake",
                inputs={
                    "name": "Ada Voss",
                    "email": "ada@voss.example",
                    "company": "Voss Ltd",
                    "message": "demo please",
                },
            )
        )
    )
    with _client(rt) as client:
        documents = client.get("/api/knowledge/documents").json()
        assert len(documents) == 4 and all(d["status"] == "indexed" and d["chunks"] > 0 for d in documents)
        assert {d["title"]: d["trusted"] for d in documents}["Vendor Terms (received from a supplier)"] is False
        created = client.post(
            "/api/knowledge/documents",
            json={
                "title": "Travel Policy",
                "text": "# Travel\n\nFlights over 800 USD need director approval.",
                "doc_type": "policy",
            },
        )
        assert created.status_code == 201 and created.json()["status"] == "created" and created.json()["chunks"] == 1
        found = client.post("/api/knowledge/search", json={"query": "flights director approval"}).json()
        assert found["results"][0]["citation"] == "Travel Policy > Travel" and found["embedder"] == "hashing-v1"
        assert client.delete(f"/api/knowledge/documents/{created.json()['doc_id']}").json()["deleted"]
        assert (
            client.post("/api/knowledge/search", json={"query": "flights director approval"}).json()["results"][0][
                "title"
            ]
            != "Travel Policy"
        )
        assert client.delete("/api/knowledge/documents/doc_missing").status_code == 404
        assert client.post("/api/knowledge/search", json={"query": "x"}).status_code == 422
        assert client.post("/api/knowledge/documents", json={"title": "", "text": "x"}).status_code == 422

        memory = client.get("/api/memory").json()
        assert {m["kind"] for m in memory} == {"semantic", "episodic"}
        fact = next(m for m in client.get("/api/memory?kind=semantic").json() if "Ada Voss" in m["text"])
        assert client.get("/api/memory?q=Voss").json()[0]["score"] > 0
        assert client.get("/api/memory?kind=bogus").status_code == 422
        assert client.delete(f"/api/memory/{fact['item_id']}").status_code == 200
        assert client.delete(f"/api/memory/{fact['item_id']}").status_code == 404
        assert not any("Ada Voss" in m["text"] for m in client.get("/api/memory?kind=semantic").json())


def test_settings_never_contain_secret_values(tmp_path):
    secrets = {
        "OPENAI_API_KEY": "sk-test-SECRETVALUE123",
        "api_key": "dash-key-SECRET",
        "webhook_secret": "whsec-SECRET",
    }
    rt = started(make_settings(tmp_path, **secrets))
    with _client(rt) as client:
        assert client.get("/api/settings").status_code == 401  # a key is configured, so it is required
        response = client.get("/api/settings", headers={"Authorization": "Bearer dash-key-SECRET"})
        body = response.text
        assert response.status_code == 200 and "SECRET" not in body and "sk-test" not in body
        settings = response.json()
        assert (
            settings["model"]["openai_key_configured"] is True and settings["security"]["dashboard_access"] == "api_key"
        )
        assert settings["security"]["webhooks_enabled"] is True and {i["mode"] for i in settings["integrations"]} == {
            "mock"
        }
        for path in ("/api/meta", "/api/overview", "/config.js", "/"):
            text = client.get(path, headers={"Authorization": "Bearer dash-key-SECRET"}).text
            assert "SECRET" not in text and "sk-test" not in text
    rt.close()


def test_dashboard_api_is_closed_to_other_machines_without_a_key(rt):
    with _client(rt, client=("203.0.113.9", 4444)) as remote:
        assert remote.get("/api/overview").status_code == 403
        assert remote.post("/api/workflows/lead_intake/start", json={}).status_code == 403
        assert remote.get("/health").status_code == 200
    with _client(rt) as local:
        assert local.get("/api/overview").status_code == 200
    assert rt.db.list_runs("demo") == []


def test_local_only_mode_rejects_cross_site_and_rebinding_requests(rt):
    """A page on another site, open in the same browser, must not be able to drive the local API."""
    with _client(rt) as client:
        cross_site = client.post(
            "/api/workflows/lead_intake/start", json={}, headers={"Origin": "https://evil.example"}
        )
        assert cross_site.status_code == 403 and "cross-origin" in cross_site.json()["detail"]
        rebinding = client.get("/api/overview", headers={"Host": "attacker.example:8000"})
        assert rebinding.status_code == 403
        same_origin = client.post(
            "/api/agents/research/enabled", json={"enabled": True}, headers={"Origin": "http://testserver"}
        )
        assert same_origin.status_code == 200
    assert rt.db.list_runs("demo") == []


def test_backend_serves_the_dashboard_in_live_mode(rt):
    with _client(rt) as client:
        config = client.get("/config.js")
        assert (
            config.status_code == 200
            and '"mode": "live"' in config.text
            and config.headers["cache-control"] == "no-store"
        )
        page = client.get("/")
        assert page.status_code == 200 and "BizOps Agent Console" in page.text
        assert (
            client.get("/assets/js/app.js").status_code == 200 and client.get("/assets/styles.css").status_code == 200
        )


# Static assets ---------------------------------------------------------------
def test_static_default_is_demo_mode_with_a_strict_content_security_policy():
    assert '"demo"' in (DASHBOARD / "config.js").read_text()
    html = (DASHBOARD / "index.html").read_text()
    policy = re.search(r'Content-Security-Policy" content="([^"]+)"', html).group(1)
    assert "default-src 'self'" in policy and "script-src 'self'" in policy and "unsafe-inline" not in policy
    assert not re.search(r"<script(?![^>]*\bsrc=)", html), "inline scripts are not allowed"
    assert not re.search(r"(src|href)=\"https?://", html), "the dashboard must not load third-party resources"


def test_frontend_never_interprets_data_as_markup_or_stores_keys():
    raw = "\n".join(p.read_text() for p in (DASHBOARD / "assets" / "js").rglob("*.js"))
    sources = re.sub(r"^\s*//.*$", "", raw, flags=re.MULTILINE)  # comments may name what is forbidden
    sources = sources.replace("http://www.w3.org/2000/svg", "")  # the SVG namespace identifier is not a request
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "sessionStorage"):
        assert forbidden not in sources, f"{forbidden} must not be used in the dashboard"
    # localStorage is used for the colour theme only; the access key lives in memory.
    assert set(re.findall(r"localStorage\.\w+\(\"([^\"]+)\"", sources)) == {"bizops-theme"}
    assert not re.search(r"https?://", sources), "no external endpoints in frontend code"


def test_demo_snapshot_matches_the_contract_and_holds_no_private_data():
    snapshot = json.loads((DASHBOARD / "demo" / "snapshot.json").read_text())
    required = {"recorded", "meta", "overview", "agents", "workflows", "runs", "run_details", "timelines", "tools",
                "tool_calls", "documents", "searches", "memory", "settings"}  # fmt: skip
    assert required <= set(snapshot)
    assert set(snapshot["run_details"]) == {r["run_id"] for r in snapshot["runs"]} == set(snapshot["timelines"])
    assert snapshot["meta"]["llm"]["provider"] == "scripted" and "fictional" in snapshot["recorded"]["note"]
    assert any(r["replans"] for r in snapshot["runs"]), "the snapshot should include a replanned run"
    text = json.dumps(snapshot)
    assert not re.search(r"sk-[A-Za-z0-9]{8,}|/home/|/tmp/|/Users/", text), "no keys or local paths in the public demo"
    emails = set(re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text))
    assert emails and all(e.endswith(".example") for e in emails), "only fictional .example addresses may appear"
    model = snapshot["settings"]["model"]
    assert not model["openai_key_configured"] and not model["anthropic_key_configured"]
