"""Job queue, worker, and the webhook API."""

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

from conftest import lead_task, make_settings, run, started
from fastapi.testclient import TestClient

from bizops.api import create_app
from bizops.api.app import verify_signature
from bizops.jobs import JobQueue, Worker

SECRET, API_KEY = "whsec_test_secret", "key_test_123"


def _signed(body: dict, secret: str = SECRET) -> tuple[bytes, str]:
    raw = json.dumps(body).encode()
    return raw, "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def test_queue_dedupes_claims_once_and_respects_run_at(rt):
    queue = rt.queue
    job_id, created = queue.enqueue_task(lead_task(), dedupe_key="evt-1")
    again, created_again = queue.enqueue_task(lead_task(), dedupe_key="evt-1")
    assert created and not created_again and again == job_id
    later, _ = queue.enqueue_task(lead_task(), run_at=datetime.now(UTC) + timedelta(hours=1))
    claimed = queue.claim_due()
    assert claimed["job_id"] == job_id and claimed["attempts"] == 1
    assert queue.claim_due() is None  # the first job is taken, the second is not due yet
    assert queue.get(later)["status"] == "pending" and queue.counts() == {"pending": 1, "running": 1}


def test_worker_runs_queued_tasks_and_survives_failures(rt):
    job_id, _ = rt.queue.enqueue_task(lead_task())
    outcomes = run(rt.worker.drain())
    assert len(outcomes) == 1 and outcomes[0]["run_status"] == "completed"
    assert rt.queue.get(job_id)["status"] == "done" and rt.queue.get(job_id)["result"]["run_id"].startswith("run_")

    class Exploding:
        async def submit(self, task):
            raise RuntimeError("database unavailable")

    queue = JobQueue(rt.db)
    failing, _ = queue.enqueue_task(lead_task())
    outcome = run(Worker(queue, Exploding()).run_once())
    job = queue.get(failing)
    assert "database unavailable" in outcome["error"] and job["status"] == "pending" and job["attempts"] == 1
    assert job["run_at"] > datetime.now(UTC).isoformat()  # rescheduled with a delay, not retried in a tight loop
    for _ in range(2):  # exhaust the remaining attempts
        rt.db.execute("UPDATE jobs SET run_at=? WHERE job_id=?", ("2000-01-01T00:00:00+00:00", failing))
        run(Worker(queue, Exploding()).run_once())
    assert queue.get(failing)["status"] == "dead"


def test_signature_verification():
    raw, signature = _signed({"a": 1})
    assert verify_signature(SECRET, raw, signature)
    assert not verify_signature(SECRET, raw + b" ", signature)
    assert not verify_signature("other", raw, signature)
    assert not verify_signature(SECRET, raw, None) and not verify_signature("", raw, signature)
    assert not verify_signature(SECRET, raw, signature.removeprefix("sha256="))
    assert not verify_signature(SECRET, raw, "sha256=\u00e9\u00e9")  # non-ASCII header must not raise


def test_webhook_to_run_end_to_end(tmp_path):
    rt = started(make_settings(tmp_path, webhook_secret=SECRET, api_key=API_KEY))
    lead = {
        "name": "Felix Arnaud",
        "email": "felix.arnaud@tallowbridge.example",
        "company": "Tallow Bridge Estates",
        "message": "Please send a quote, urgent.",
    }
    event = {"event": "lead.created", "data": lead}
    raw, signature = _signed(event)
    headers = {"X-Bizops-Signature": signature, "X-Bizops-Event-Id": "evt-100"}
    auth = {"Authorization": f"Bearer {API_KEY}"}
    with TestClient(create_app(rt, run_worker=False)) as client:
        assert client.get("/health").json()["status"] == "ok"
        accepted = client.post("/webhooks/website", content=raw, headers=headers)
        assert accepted.status_code == 202 and accepted.json()["status"] == "queued"
        duplicate = client.post("/webhooks/website", content=raw, headers=headers)
        assert duplicate.json() == {"job_id": accepted.json()["job_id"], "status": "duplicate"}

        # Rejections: bad signature, missing event id, unsupported event, malformed body.
        assert (
            client.post(
                "/webhooks/website", content=raw, headers={**headers, "X-Bizops-Signature": "sha256=00"}
            ).status_code
            == 401
        )
        assert (
            client.post("/webhooks/website", content=raw, headers={"X-Bizops-Signature": signature}).status_code == 400
        )
        other_raw, other_sig = _signed({"event": "unknown.event"})
        assert (
            client.post(
                "/webhooks/website",
                content=other_raw,
                headers={"X-Bizops-Signature": other_sig, "X-Bizops-Event-Id": "e2"},
            ).status_code
            == 422
        )
        bad = b"not json"
        bad_sig = "sha256=" + hmac.new(SECRET.encode(), bad, hashlib.sha256).hexdigest()
        assert (
            client.post(
                "/webhooks/website", content=bad, headers={"X-Bizops-Signature": bad_sig, "X-Bizops-Event-Id": "e3"}
            ).status_code
            == 400
        )

        outcomes = run(rt.worker.drain())  # the worker picks the job up
        assert len(outcomes) == 1 and outcomes[0]["run_status"] == "completed"
        job = client.get(f"/jobs/{accepted.json()['job_id']}", headers=auth).json()
        report = client.get(f"/runs/{job['result']['run_id']}", headers=auth).json()
        assert report["status"] == "completed" and report["report"]["results"]["qualify_lead"]["tier"] in (
            "hot",
            "warm",
        )
        assert len(client.get(f"/runs/{job['result']['run_id']}/trace", headers=auth).json()) > 10

        # Inspection endpoints need the key; unknown ids are 404.
        assert client.get(f"/jobs/{job['job_id']}").status_code == 401
        assert (
            client.get(f"/runs/{job['result']['run_id']}", headers={"Authorization": "Bearer wrong"}).status_code == 401
        )
        assert client.get("/runs/run_missing", headers=auth).status_code == 404
    assert rt.db.kv_get("demo", "crm.contacts", "felix.arnaud@tallowbridge.example")["lead_tier"] in ("hot", "warm")
    assert len(rt.db.list_runs("demo")) == 1  # the duplicate delivery did not start a second run
    rt.close()


def test_http_surfaces_are_disabled_without_secrets(rt):
    raw, signature = _signed({"event": "lead.created"})
    with TestClient(create_app(rt, run_worker=False)) as client:
        response = client.post(
            "/webhooks/x", content=raw, headers={"X-Bizops-Signature": signature, "X-Bizops-Event-Id": "e"}
        )
        assert response.status_code == 503
        assert client.get("/runs/anything", headers={"Authorization": "Bearer "}).status_code == 503
        assert client.get("/health").status_code == 200


def test_job_abandoned_by_a_dead_worker_is_reclaimed(rt):
    job_id, _ = rt.queue.enqueue_task(lead_task())
    assert rt.queue.claim_due()["job_id"] == job_id  # a worker takes it, then dies
    assert rt.queue.claim_due() is None
    later = datetime.now(UTC) + timedelta(minutes=11)
    reclaimed = rt.queue.claim_due(now=later)
    assert reclaimed["job_id"] == job_id and reclaimed["attempts"] == 2


def test_non_ascii_api_key_header_is_rejected_not_crashed(tmp_path):
    rt = started(make_settings(tmp_path, api_key=API_KEY))
    with TestClient(create_app(rt, run_worker=False)) as client:
        assert client.get("/runs/x", headers={"Authorization": "Bearer caf\u00e9".encode()}).status_code == 401
    rt.close()
