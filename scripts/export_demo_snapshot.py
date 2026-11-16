"""Record the read-only snapshot used by the GitHub Pages demonstration.

The snapshot is not hand-written. This script starts the real runtime on a
temporary database with the offline scripted model, runs the demonstration
workflows (including a retry and a replanning case), calls the same dashboard
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
from bizops.runtime import build_runtime

OUT = Path(__file__).resolve().parents[1] / "dashboard" / "demo" / "snapshot.json"
SAMPLE_QUERIES = [
    "how many reminders can a customer receive for one invoice",
    "refund approval by a manager",
    "introductory meeting length",
    "payment terms net 30 interest",
]


async def run_scenarios(rt) -> None:
    for name, scenario in SCENARIOS.items():
        await rt.orchestrator.submit(
            Task(
                objective=scenario["objective"], workflow=name, inputs=scenario["inputs"], requested_by="demo-recording"
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
            }
        rt.close()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(snapshot, indent=1, default=str) + "\n", encoding="utf-8")
    print(f"Wrote {OUT} ({OUT.stat().st_size // 1024} KB, {len(snapshot['runs'])} runs)")


if __name__ == "__main__":
    main()
