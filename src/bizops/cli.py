"""Command line interface.

bizops demo                      run the built-in lead intake demonstration
bizops run "objective" --input key=value ...
bizops status RUN_ID             show a run's state and report
bizops resume RUN_ID             continue a paused or suspended run
bizops trace RUN_ID              print the execution timeline
bizops runs                      list recent runs
bizops agents | tools            show registered specialists or tools
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from .core.errors import BizOpsError
from .core.models import RunState, Task
from .runtime import Runtime, build_runtime

DEMO_LEAD = {
    "name": "Amelia Hart",
    "email": "amelia.hart@harborline.example",
    "company": "Harborline Realty",
    "message": "We need help handling inbound enquiries. We have budget approved and would like a demo this week.",
}


def _print_run(state: RunState) -> None:
    print(
        f"Run {state.run_id}  status={state.status.value}  replans={state.replans}  "
        f"model_calls={state.usage.calls}  cost_usd={state.usage.cost_usd}"
    )
    if state.plan:
        print(f"Plan v{state.plan.version}: {state.plan.rationale}")
        for step in state.plan.steps:
            summary = (step.result or {}).get("_summary") or step.error or ""
            print(f"  {step.index + 1}. [{step.status.value:<9}] {step.agent}.{step.action}  {summary}")
    if state.error:
        print(f"Error: {state.error}")
    if state.final_report:
        verification = state.final_report["verification"]
        print(f"Outcome: {state.final_report['outcome']}  (verification passed: {verification['passed']})")


def _parse_inputs(pairs: list[str]) -> dict[str, Any]:
    inputs: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--input expects key=value, got '{pair}'")
        key, value = pair.split("=", 1)
        inputs[key] = value
    return inputs


def _dispatch(args: argparse.Namespace, rt: Runtime) -> int:
    tenant = args.tenant or rt.settings.default_tenant
    if args.command in ("demo", "run"):
        if args.command == "demo":
            task = Task(
                tenant_id=tenant,
                objective="Handle a new inbound lead",
                workflow="lead_intake",
                inputs=DEMO_LEAD,
                source="cli",
                requested_by="demo",
            )
        else:
            task = Task(
                tenant_id=tenant,
                objective=args.objective,
                workflow=args.workflow,
                inputs=_parse_inputs(args.input),
                idempotency_key=args.idempotency_key,
                source="cli",
            )
        state = asyncio.run(rt.orchestrator.submit(task))
        _print_run(state)
        if args.json and state.final_report:
            print(json.dumps(state.final_report, indent=2, default=str))
        return 0 if state.status.value in ("completed", "waiting_approval", "paused") else 1
    if args.command == "status":
        state = rt.db.load_run(tenant, args.run_id)
        _print_run(state)
        if args.json:
            print(state.model_dump_json(indent=2))
    elif args.command == "resume":
        _print_run(asyncio.run(rt.orchestrator.resume(tenant, args.run_id)))
    elif args.command == "pause":
        rt.orchestrator.request_pause(tenant, args.run_id)
        print("Pause requested; the run stops at its next step boundary.")
    elif args.command == "cancel":
        _print_run(rt.orchestrator.cancel(tenant, args.run_id))
    elif args.command == "trace":
        for event in rt.tracer.timeline(tenant, args.run_id):
            duration = f"{event['duration_ms']:>8.1f}ms" if event["duration_ms"] is not None else " " * 10
            detail = {
                k: v
                for k, v in event.items()
                if k not in ("ts", "kind", "span_id", "parent_span_id", "duration_ms") and v not in (None, "")
            }
            print(f"{event['ts']}  {duration}  {event['kind']:<22} {json.dumps(detail, default=str)[:160]}")
    elif args.command == "runs":
        for row in rt.db.list_runs(tenant):
            print(f"{row['run_id']}  {row['status']:<17} {row['created_at'][:19]}  {row['objective'][:60]}")
    elif args.command == "agents":
        print(json.dumps(rt.registry.describe(), indent=2))
    elif args.command == "tools":
        for spec in rt.gateway.all_specs():
            print(f"{spec.name:<22} risk={spec.risk.value:<6} source={spec.source:<8} {spec.description}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bizops", description="Autonomous AI Business Operations Agent")
    parser.add_argument("--tenant", default=None, help="tenant id (default from configuration)")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="run the lead intake demonstration")
    demo.add_argument("--json", action="store_true")
    run = sub.add_parser("run", help="submit a task")
    run.add_argument("objective")
    run.add_argument("--workflow", default=None)
    run.add_argument("--input", action="append", default=[], metavar="KEY=VALUE")
    run.add_argument("--idempotency-key", default=None)
    run.add_argument("--json", action="store_true")
    for name in ("status", "resume", "pause", "cancel", "trace"):
        command = sub.add_parser(name)
        command.add_argument("run_id")
        if name == "status":
            command.add_argument("--json", action="store_true")
    for name in ("runs", "agents", "tools"):
        sub.add_parser(name)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rt = build_runtime()
    try:
        return _dispatch(args, rt)
    except BizOpsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        rt.close()


if __name__ == "__main__":
    raise SystemExit(main())
