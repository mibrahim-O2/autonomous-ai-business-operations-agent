"""Command line interface.

bizops demo [lead|collections|inbox]   run a demonstration workflow
bizops run "objective" --input k=v     submit a task
bizops status|resume|pause|cancel|trace RUN_ID
bizops runs | agents | tools | mcp     inspect the system
bizops kb ingest PATH | kb search "q"  manage and query the knowledge base
bizops state [crm|sent|drafts|tasks|invoices]   show mock business data
bizops approvals [--status pending]    list approval requests
bizops approve ID [--all-in-step]      approve a request and continue its run
bizops reject ID --comment "why"       reject a request and continue its run
bizops policy [show|reset]             show or reset the approval policy
bizops audit [--verify]                show the audit log or verify its hash chain
bizops keys create NAME --role ROLE    create a tenant- and role-bound API key (shown once)
bizops keys list | keys revoke ID
bizops recover                         resume runs whose worker died
bizops eval [--case ID] [--json]       run the evaluation suite with the configured model
bizops worker --once                   process queued jobs
bizops serve                           start the HTTP API and worker
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from pathlib import Path
from typing import Any

from .approvals import DEFAULT_POLICY, load_policy, save_policy
from .core.errors import BizOpsError
from .core.models import RunState, Task
from .demo import SCENARIOS
from .runtime import Runtime, build_runtime
from .security import Principal, Role

DEMOS = {s["key"]: (s["objective"], name, s["inputs"]) for name, s in SCENARIOS.items()}
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


def _operator(args: argparse.Namespace, tenant: str) -> Principal:
    """The person running the command. Someone with shell access to the deployment is an administrator."""
    name = getattr(args, "as_name", None) or f"cli:{getpass.getuser()}"
    return Principal(name=name, tenant_id=tenant, role=Role.ADMIN, kind="local")


def _print_approval(record: dict[str, Any]) -> None:
    print(
        f"{record['approval_id']}  {record['status']:<9} {record['tool']}  risk={record['risk']}  "
        f"needs={record['required_role']}  run={record['run_id']}"
    )
    print(f"    arguments: {json.dumps(record['arguments'])}")
    print(f"    why: {record['reason']} (rule {record['rule']})")
    if record["signals"]:
        print(f"    look twice: {', '.join(record['signals'])}")
    if record["decided_by"]:
        print(f"    decided by {record['decided_by']} at {record['decided_at']}: {record['comment'] or '(no comment)'}")


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
        if args.paused:
            # Create the run with a pause already requested: it stops at its first step boundary,
            # exactly as when Pause is pressed during a run. Continue it with `bizops resume`.
            created, _ = rt.orchestrator.create(task)
            rt.orchestrator.request_pause(created.tenant_id, created.run_id)
            state = await rt.orchestrator.drive(created.tenant_id, created.run_id)
            _print_run(state)
            print(f"\nPaused before it did anything. Continue with:  bizops resume {state.run_id}")
            return 0
        state = await rt.orchestrator.submit(task)
        _print_run(state)
        if state.status.value == "waiting_approval":
            print("\nWaiting for a person. Nothing below has been executed yet:")
            for record in rt.approvals.list(tenant, status="pending", run_id=state.run_id):
                _print_approval(record)
            print(
                f"\nDecide with:  bizops approve {state.pending_approval_id}   or   bizops reject "
                f'{state.pending_approval_id} --comment "reason"'
            )
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
    elif command == "approvals":
        records = rt.approvals.list(tenant, status=args.status)
        if not records:
            print("No approval requests" + (f" with status {args.status}." if args.status else "."))
        for record in records:
            _print_approval(record)
    elif command in ("approve", "reject"):
        principal = _operator(args, tenant)
        scope = "step_tool" if getattr(args, "all_in_step", False) else "call"
        record = rt.approvals.decide(principal, args.approval_id, command == "approve", args.comment, scope)
        print(f"Recorded: {record['status']} by {record['decided_by']}.")
        state = await rt.orchestrator.resume(tenant, record["run_id"])
        _print_run(state)
        if state.status.value == "waiting_approval":
            print(f"\nAnother request is waiting: {state.pending_approval_id}")
    elif command == "policy":
        if args.policy_command == "reset":
            saved = save_policy(rt.db, tenant, DEFAULT_POLICY)
            rt.audit.record(tenant, _operator(args, tenant).name, "policy.reset", "approval", actor_role="admin")
            print(f"Approval policy reset to the default (version {saved.version}).")
        else:
            print(load_policy(rt.db, tenant).model_dump_json(indent=2))
    elif command == "audit":
        if args.verify:
            result = rt.audit.verify(tenant)
            print(json.dumps(result, indent=2))
            return 0 if result["ok"] else 1
        for entry in reversed(rt.audit.list(tenant, args.limit)):
            detail = json.dumps(entry["detail"], default=str)[:110]
            print(
                f"{entry['seq']:>5}  {entry['at'][:19]}  {entry['actor']:<22} {entry['action']:<24} "
                f"{entry['target']:<18} {detail}"
            )
    elif command == "keys":
        if args.keys_command == "create":
            key_id, token = rt.keys.create(tenant, args.name, Role(args.role), created_by=_operator(args, tenant).name)
            rt.audit.record(
                tenant, _operator(args, tenant).name, "key.created", key_id, actor_role="admin", role=args.role
            )
            print(f"Created key {key_id} for tenant '{tenant}' with role '{args.role}'.")
            print("Store this token now; it cannot be shown again:")
            print(token)
        elif args.keys_command == "revoke":
            if not rt.keys.revoke(tenant, args.key_id):
                raise SystemExit("key not found or already revoked")
            rt.audit.record(tenant, _operator(args, tenant).name, "key.revoked", args.key_id, actor_role="admin")
            print(f"Revoked {args.key_id}.")
        else:
            for key in rt.keys.list(tenant):
                state_text = "revoked" if key["revoked_at"] else "active"
                print(
                    f"{key['key_id']}  {key['role']:<9} {state_text:<8} {key['name']}  "
                    f"last used {key['last_used_at'] or 'never'}"
                )
    elif command == "eval":
        from .evaluation import EvaluationStore
        from .evaluation.runner import evaluate_and_store

        store = EvaluationStore(rt.db)
        if rt.llm.name != "scripted":
            print(f"Evaluating with the live model {rt.llm.name} {rt.llm.model}. This makes billed API calls.")
        eval_id = await evaluate_and_store(
            store, rt.settings, tenant, rt.llm.name, rt.llm.model, _operator(args, tenant).name, args.case or None
        )
        report = store.get(tenant, eval_id)
        assert report is not None
        if args.json:
            print(json.dumps(report, indent=2, default=str))
        for case in report["cases"]:
            mark = "PASS" if case["passed"] else "FAIL"
            print(
                f"{mark}  {case['case_id']:<24} {case['category']:<13} {case['metrics']['latency_ms']:>8.1f} ms  "
                f"{case['title']}"
            )
            for check in case["checks"]:
                if not check["passed"]:
                    print(f"        failed: {check['name']}: {check['detail']}")
        summary, retrieval = report["summary"], report["summary"]["retrieval"]
        print(
            f"\n{summary['passed']}/{summary['cases']} cases passed ({summary['checks_passed']}/{summary['checks']} "
            f"checks); p95 latency {summary['latency_ms']['p95']} ms; {summary['model_calls']} model calls; "
            f"cost {summary['cost_usd']} USD"
        )
        print(
            f"Retrieval: hit@1 {retrieval['hit_at_1']}, recall@3 {retrieval['recall_at_3']}, MRR {retrieval['mrr']}, "
            f"untrusted filter {'ok' if retrieval['untrusted_filter_ok'] else 'LEAKED'}  (stored as {eval_id})"
        )
        return 0 if summary["passed"] == summary["cases"] else 1
    elif command == "recover":
        recovered = await rt.orchestrator.recover_interrupted()
        print(f"Recovered {len(recovered)} interrupted run(s): {', '.join(recovered) or 'none'}")
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
    demo.add_argument("--paused", action="store_true", help="create the run paused, to test pause and resume")
    run = sub.add_parser("run", help="submit a task")
    run.add_argument("objective")
    run.add_argument("--workflow", default=None)
    run.add_argument("--input", action="append", default=[], metavar="KEY=VALUE")
    run.add_argument("--idempotency-key", default=None)
    run.add_argument("--json", action="store_true")
    run.add_argument("--paused", action="store_true", help="create the run paused, to test pause and resume")
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
    listing = sub.add_parser("approvals", help="list approval requests")
    listing.add_argument("--status", default=None, choices=["pending", "approved", "rejected", "expired", "cancelled"])
    for name in ("approve", "reject"):
        decision = sub.add_parser(name, help=f"{name} an approval request and continue its run")
        decision.add_argument("approval_id")
        decision.add_argument("--comment", default="", required=name == "reject")
        decision.add_argument("--as", dest="as_name", default=None, help="name recorded as the decider")
        if name == "approve":
            decision.add_argument(
                "--all-in-step", action="store_true", help="also approve further calls to this tool in the same step"
            )
    policy = sub.add_parser("policy", help="approval policy")
    policy.add_argument("policy_command", nargs="?", default="show", choices=["show", "reset"])
    audit = sub.add_parser("audit", help="audit log")
    audit.add_argument("--verify", action="store_true", help="verify the hash chain")
    audit.add_argument("--limit", type=int, default=40)
    keys = sub.add_parser("keys", help="API keys")
    keys_sub = keys.add_subparsers(dest="keys_command", required=True)
    keys_sub.add_parser("list")
    create = keys_sub.add_parser("create")
    create.add_argument("name")
    create.add_argument("--role", required=True, choices=[r.value for r in Role])
    revoke = keys_sub.add_parser("revoke")
    revoke.add_argument("key_id")
    sub.add_parser("recover", help="resume runs whose worker died")
    evaluate = sub.add_parser("eval", help="run the evaluation suite with the configured model")
    evaluate.add_argument("--case", action="append", default=[], help="run only this case (repeatable)")
    evaluate.add_argument("--json", action="store_true")
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
