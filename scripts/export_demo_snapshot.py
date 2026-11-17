"""Record the read-only snapshot used by the GitHub Pages demonstration.

The snapshot is not hand-written. This script starts the real runtime on a
temporary database with the offline scripted model, runs the demonstration
workflows (including a retry, a replanning case, an approved batch, a
rejected refund, and a refund left waiting for a decision), calls the same dashboard
API the live application serves, and writes the responses to
dashboard/demo/snapshot.json. The static demo then replays exactly what the
backend returned, and labels it as recorded sample data.

Usage: python scripts/export_demo_snapshot.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from bizops.api import create_app
from bizops.core.config import Settings
from bizops.core.models import Task
from bizops.demo import SCENARIOS
from bizops.evaluation import EvaluationStore
from bizops.evaluation.runner import evaluate_and_store
from bizops.runtime import build_runtime
from bizops.security import Principal, Role

# The fictional person recorded as making the demo's decisions.
RECORDER = Principal(name="demo-approver", tenant_id="demo", role=Role.ADMIN, kind="system")

OUT = Path(__file__).resolve().parents[1] / "dashboard" / "demo" / "snapshot.json"
SAMPLE_QUERIES = [
    "how many reminders can a customer receive for one invoice",
    "refund approval by a manager",
    "introductory meeting length",
    "payment terms net 30 interest",
]


async def run_scenarios(rt) -> None:
    states = {}
    for name, scenario in SCENARIOS.items():
        if name == "refund_request":
            continue  # recorded below, in two variants
        states[name] = await rt.orchestrator.submit(
            Task(
                objective=scenario["objective"], workflow=name, inputs=scenario["inputs"], requested_by="demo-recording"
            )
        )
    # Collections stops for a person before the first reminder. A recorded approver approves the batch.
    collections = states["invoice_collections"]
    rt.approvals.decide(
        RECORDER, collections.pending_approval_id, True, "Policy applied correctly; send both.", scope="step_tool"
    )
    await rt.orchestrator.resume("demo", collections.run_id)
    # A refund that a recorded approver rejects: the run hands the case to finance instead.
    refund = SCENARIOS["refund_request"]
    rejected = await rt.orchestrator.submit(
        Task(
            objective=refund["objective"],
            workflow="refund_request",
            inputs={**refund["inputs"], "amount": 120, "reason": "Partial refund requested by email"},
            requested_by="demo-recording",
        )
    )
    rt.approvals.decide(
        RECORDER, rejected.pending_approval_id, False, "Partial refunds need the account manager first."
    )
    await rt.orchestrator.resume("demo", rejected.run_id)
    # A refund left waiting, so the demo shows what an approver sees. Nothing will ever decide it.
    await rt.orchestrator.submit(
        Task(
            objective=refund["objective"],
            workflow="refund_request",
            inputs=refund["inputs"],
            requested_by="demo-recording",
        )
    )
    # A transient calendar fault: the step is retried and succeeds.
    rt.mock_control.set_fault("demo", "calendar.find_slots", times=1, retryable=True, message="calendar timed out")
    lead = {
        "name": "Felix Arnaud",
        "email": "felix.arnaud@tallowbridge.example",
        "company": "Tallow Bridge Estates",
        "message": "Could we arrange a demo? We are ready to start this month.",
    }
    await rt.orchestrator.submit(
        Task(objective="Handle a new inbound lead", workflow="lead_intake", inputs=lead, requested_by="demo-recording")
    )
    # A permanent mail fault: the planner replans to a manual follow-up.
    rt.mock_control.set_fault(
        "demo", "email.create_draft", times=99, retryable=False, message="mail server rejected it"
    )
    lead = {
        "name": "Wendy Castellan",
        "email": "wendy.castellan@northgate.example",
        "company": "Northgate Advisory",
        "message": "Please send information about your services.",
    }
    await rt.orchestrator.submit(
        Task(objective="Handle a new inbound lead", workflow="lead_intake", inputs=lead, requested_by="demo-recording")
    )
    rt.mock_control.set_fault("demo", "email.create_draft", times=0)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rt = build_runtime(Settings(_env_file=None, data_dir=Path(tmp), log_level="WARNING"))
        asyncio.run(rt.start())
        asyncio.run(run_scenarios(rt))
        # A recorded evaluation of the offline model, stored exactly as `bizops eval` stores it.
        asyncio.run(
            evaluate_and_store(EvaluationStore(rt.db), rt.settings, "demo", rt.llm.name, rt.llm.model, "demo-recording")
        )
        logging.disable(logging.INFO)  # keep the recording quiet
        with TestClient(create_app(rt, run_worker=False), client=("127.0.0.1", 50000)) as client:
            get = lambda path: client.get(f"/api{path}").json()  # noqa: E731
            runs = get("/runs")
            settings = get("/settings")
            settings["environment"]["data_dir"] = "(not included in the demo)"
            snapshot = {
                "recorded": {
                    "note": "Recorded sample data from an offline run of the scripted model. "
                    "All people, companies, and records are fictional.",
                    "generator": "scripts/export_demo_snapshot.py",
                },
                "meta": get("/meta"),
                "overview": get("/overview"),
                "agents": get("/agents"),
                "workflows": get("/workflows"),
                "runs": runs,
                "run_details": {r["run_id"]: get(f"/runs/{r['run_id']}") for r in runs},
                "timelines": {r["run_id"]: get(f"/runs/{r['run_id']}/timeline") for r in runs},
                "tools": get("/tools"),
                "tool_calls": get("/tool-calls"),
                "documents": get("/knowledge/documents"),
                "searches": {q: client.post("/api/knowledge/search", json={"query": q}).json() for q in SAMPLE_QUERIES},
                "memory": get("/memory"),
                "settings": settings,
                "approvals": get("/approvals"),
                "policy": get("/policy"),
                "audit": get("/audit"),
                "audit_verify": get("/audit/verify"),
                "security": get("/security"),
                "observability": get("/observability"),
                "events": get("/events?limit=500"),
                "evaluations": get("/evaluations"),
            }
            snapshot["evaluation_details"] = {
                r["eval_id"]: get(f"/evaluations/{r['eval_id']}") for r in snapshot["evaluations"]["runs"]
            }
        rt.close()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(snapshot, indent=1, default=str) + "\n", encoding="utf-8")
    print(f"Wrote {OUT} ({OUT.stat().st_size // 1024} KB, {len(snapshot['runs'])} runs)")


if __name__ == "__main__":
    main()
