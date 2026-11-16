"""Command line interface.

bizops demo [lead|collections|inbox]   run a demonstration workflow
bizops run "objective" --input k=v     submit a task
bizops status|resume|pause|cancel|trace RUN_ID
bizops runs | agents | tools | mcp     inspect the system
bizops kb ingest PATH | kb search "q"  manage and query the knowledge base
bizops state [crm|sent|drafts|tasks|invoices]   show mock business data
bizops worker --once                   process queued jobs
bizops serve                           start the HTTP API and worker
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from .core.errors import BizOpsError
from .core.models import RunState, Task
from .runtime import Runtime, build_runtime

DEMO_LEAD = {
    "name": "Noor Abbasi",
    "email": "noor.abbasi@quillfield.example",
    "company": "Quillfield Partners",
    "message": "We need help handling inbound enquiries. We have budget approved and would like a demo this week.",
}
DEMOS = {
    "lead": ("Handle a new inbound lead", "lead_intake", DEMO_LEAD),
    "collections": ("Review overdue invoices and send payment reminders", "invoice_collections", {}),
    "inbox": ("Triage the unread inbox", "inbox_triage", {}),
}
STATE_NAMESPACES = {
    "crm": "crm.contacts",
    "sent": "email.sent",
    "drafts": "email.drafts",
    "tasks": "ops.tasks",
    "invoices": "billing.invoices",
    "events": "calendar.events",
}


def _print_run(state: RunState) -> None:
    print(
        f"Run {state.run_id}  status={state.status.value}  replans={state.replans}  "
        f"model_calls={state.usage.calls}  cost_usd={state.usage.cost_usd}  tainted={state.tainted}"
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


async def _dispatch(args: argparse.Namespace, rt: Runtime) -> int:
    await rt.start()
    tenant = args.tenant or rt.settings.default_tenant
    command = args.command
    if command in ("demo", "run"):
        if command == "demo":
            objective, workflow, inputs = DEMOS[args.scenario]
            task = Task(tenant_id=tenant, objective=objective, workflow=workflow, inputs=inputs, requested_by="demo")
        else:
            task = Task(
                tenant_id=tenant,
                objective=args.objective,
                workflow=args.workflow,
                inputs=_parse_inputs(args.input),
                idempotency_key=args.idempotency_key,
            )
        state = await rt.orchestrator.submit(task)
        _print_run(state)
        if args.json and state.final_report:
            print(json.dumps(state.final_report, indent=2, default=str))
        return 0 if state.status.value in ("completed", "waiting_approval", "paused") else 1
    if command == "status":
        state = rt.db.load_run(tenant, args.run_id)
        _print_run(state)
        if args.json:
            print(state.model_dump_json(indent=2))
    elif command == "resume":
        _print_run(await rt.orchestrator.resume(tenant, args.run_id))
    elif command == "pause":
        rt.orchestrator.request_pause(tenant, args.run_id)
        print("Pause requested; the run stops at its next step boundary.")
    elif command == "cancel":
        _print_run(rt.orchestrator.cancel(tenant, args.run_id))
    elif command == "trace":
        for event in rt.tracer.timeline(tenant, args.run_id):
            duration = f"{event['duration_ms']:>8.1f}ms" if event["duration_ms"] is not None else " " * 10
            detail = {
                k: v
                for k, v in event.items()
                if k not in ("ts", "kind", "span_id", "parent_span_id", "duration_ms") and v not in (None, "")
            }
            print(f"{event['ts']}  {duration}  {event['kind']:<22} {json.dumps(detail, default=str)[:160]}")
    elif command == "runs":
        for row in rt.db.list_runs(tenant):
            print(f"{row['run_id']}  {row['status']:<17} {row['created_at'][:19]}  {row['objective'][:60]}")
    elif command == "agents":
        print(json.dumps(rt.registry.describe(), indent=2))
    elif command == "tools":
        for spec in sorted(rt.gateway.all_specs(), key=lambda s: (s.source, s.name)):
            print(f"{spec.name:<24} risk={spec.risk.value:<6} source={spec.source:<13} {spec.description[:70]}")
    elif command == "mcp":
        print(json.dumps(rt.mcp.server_info, indent=2))
    elif command == "kb":
        if args.kb_command == "ingest":
            for result in rt.knowledge.ingest_path(tenant, Path(args.path)):
                print(f"{result['file']:<32} {result['status']:<10} chunks={result['chunks']}")
        elif args.kb_command == "search":
            for hit in rt.knowledge.search(tenant, args.query, limit=args.limit, trusted_only=args.trusted_only):
                print(f"{hit.score:.3f}  [{hit.citation}]  trusted={hit.trusted}\n       {hit.text[:150]}")
        else:
            for doc in rt.knowledge.list_documents(tenant):
                print(f"{doc['doc_id']}  {doc['doc_type']:<10} trusted={doc['trusted']!s:<5} {doc['title']}")
    elif command == "state":
        print(json.dumps(rt.db.kv_list(tenant, STATE_NAMESPACES[args.what]), indent=2))
    elif command == "worker":
        if args.once:
            for outcome in await rt.worker.drain():
                print(json.dumps(outcome))
        else:
            await rt.worker.run_forever()
    return 0


def _serve(args: argparse.Namespace, rt: Runtime) -> int:
    import uvicorn

    from .api import create_app

    uvicorn.run(create_app(rt), host=args.host, port=args.port, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bizops", description="Autonomous AI Business Operations Agent")
    parser.add_argument("--tenant", default=None, help="tenant id (default from configuration)")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="run a demonstration workflow")
    demo.add_argument("scenario", nargs="?", default="lead", choices=sorted(DEMOS))
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
    for name in ("runs", "agents", "tools", "mcp"):
        sub.add_parser(name)
    kb = sub.add_parser("kb", help="knowledge base")
    kb_sub = kb.add_subparsers(dest="kb_command", required=True)
    kb_sub.add_parser("list")
    ingest = kb_sub.add_parser("ingest")
    ingest.add_argument("path")
    search = kb_sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=4)
    search.add_argument("--trusted-only", action="store_true")
    state = sub.add_parser("state", help="show mock business data")
    state.add_argument("what", choices=sorted(STATE_NAMESPACES))
    worker = sub.add_parser("worker", help="process queued jobs")
    worker.add_argument("--once", action="store_true", help="drain due jobs and exit")
    serve = sub.add_parser("serve", help="start the HTTP API and worker")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rt = build_runtime()
    try:
        if args.command == "serve":
            return _serve(args, rt)
        return asyncio.run(_dispatch(args, rt))
    except BizOpsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        rt.close()


if __name__ == "__main__":
    raise SystemExit(main())
