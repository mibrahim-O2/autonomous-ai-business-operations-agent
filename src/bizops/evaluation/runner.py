"""Evaluation runner and result store.

Every case runs in its own runtime on a fresh temporary database, so cases
cannot affect each other or the operator's real data. The model provider is the
one configured for the deployment: the offline scripted model by default, or a
hosted model when one is configured (which costs money; the cost is reported).
"""

from __future__ import annotations

import json
import statistics
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core.config import Settings
from ..core.models import RunState, RunStatus, new_id
from ..security.auth import Principal, Role
from ..storage import Database
from .cases import CASES, RETRIEVAL_SET, EvalCase


def _isolated(settings: Settings, directory: str) -> Settings:
    # Short backoff keeps the suite quick; every other setting, including the model, is the deployment's.
    return settings.model_copy(
        update={
            "data_dir": Path(directory),
            "seed_demo_data": True,
            "retry_base_seconds": min(settings.retry_base_seconds, 0.05),
            "retry_max_seconds": min(settings.retry_max_seconds, 0.1),
        }
    )


async def _drive(rt: Any, case: EvalCase) -> RunState:
    state = await rt.orchestrator.submit(case.task.model_copy(update={"tenant_id": rt.settings.default_tenant}))
    for _ in range(12):
        if state.status != RunStatus.WAITING_APPROVAL or case.decision is None:
            break
        principal = Principal(name="evaluator", tenant_id=state.tenant_id, role=Role.ADMIN, kind="system")
        rt.approvals.decide(
            principal,
            state.pending_approval_id,
            case.decision == "approve",
            "evaluation decision",
            scope="step_tool" if case.decision == "approve" else "call",
        )
        state = await rt.orchestrator.resume(state.tenant_id, state.run_id)
    return state


async def run_case(settings: Settings, case: EvalCase, llm: Any = None) -> dict[str, Any]:
    from ..runtime import build_runtime  # local import: the runtime imports this package's siblings

    with tempfile.TemporaryDirectory() as directory:
        rt = build_runtime(_isolated(settings, directory), llm=llm)
        try:
            await rt.start()
            if case.setup:
                case.setup(rt)
            started = time.perf_counter()
            error = None
            try:
                state = await _drive(rt, case)
            except Exception as exc:  # a crash is a failed case, not a failed evaluation
                error = f"{type(exc).__name__}: {exc}"
                state = None
            latency_ms = round((time.perf_counter() - started) * 1000, 1)
            checks = []
            for name, check in case.checks:
                if state is None:
                    checks.append({"name": name, "passed": False, "detail": error})
                    continue
                try:
                    passed, detail = check(rt, state)
                except Exception as exc:
                    passed, detail = False, f"check raised {type(exc).__name__}: {exc}"
                checks.append({"name": name, "passed": bool(passed), "detail": detail})
            tool_calls = sum(len(s.tool_calls) for s in (state.plan.steps if state and state.plan else []))
            return {
                "case_id": case.case_id,
                "title": case.title,
                "category": case.category,
                "description": case.description,
                "passed": all(c["passed"] for c in checks),
                "checks": checks,
                "error": error,
                "status": state.status.value if state else "error",
                "outcome": (state.final_report or {}).get("outcome") if state else None,
                "metrics": {
                    "latency_ms": latency_ms,
                    "model_calls": state.usage.calls if state else 0,
                    "input_tokens": state.usage.input_tokens if state else 0,
                    "output_tokens": state.usage.output_tokens if state else 0,
                    "cost_usd": state.usage.cost_usd if state else 0.0,
                    "steps": state.steps_executed if state else 0,
                    "tool_calls": tool_calls,
                    "replans": state.replans if state else 0,
                    "approvals_requested": len(rt.approvals.list(rt.settings.default_tenant)),
                },
            }
        finally:
            rt.close()


async def run_retrieval(settings: Settings) -> dict[str, Any]:
    """Score retrieval against the labelled set: hit at 1, recall at 3, mean reciprocal rank, trust filtering."""
    from ..runtime import build_runtime

    with tempfile.TemporaryDirectory() as directory:
        rt = build_runtime(_isolated(settings, directory))
        try:
            await rt.start()
            tenant, rows = rt.settings.default_tenant, []
            for item in RETRIEVAL_SET:
                hits = rt.knowledge.search(tenant, item["query"], limit=5)
                ranked = [h.citation for h in hits]
                rank = ranked.index(item["expect"]) + 1 if item["expect"] in ranked else None
                row = {"query": item["query"], "expected": item["expect"], "top": ranked[:3], "rank": rank}
                if item.get("untrusted"):
                    filtered = rt.knowledge.search(tenant, item["query"], limit=5, trusted_only=True)
                    row["excluded_when_trusted_only"] = all(h.trusted for h in filtered)
                rows.append(row)
            ranks = [r["rank"] for r in rows]
            leaks = [r for r in rows if "excluded_when_trusted_only" in r and not r["excluded_when_trusted_only"]]
            return {
                "queries": len(rows),
                "hit_at_1": round(sum(1 for r in ranks if r == 1) / len(rows), 3),
                "recall_at_3": round(sum(1 for r in ranks if r and r <= 3) / len(rows), 3),
                "mrr": round(sum(1 / r for r in ranks if r) / len(rows), 3),
                "untrusted_filter_ok": not leaks,
                "embedder": rt.knowledge.embedder.name,
                "rows": rows,
            }
        finally:
            rt.close()


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[index]


def summarise(results: list[dict[str, Any]], retrieval: dict[str, Any]) -> dict[str, Any]:
    categories: dict[str, dict[str, int]] = {}
    for r in results:
        bucket = categories.setdefault(r["category"], {"cases": 0, "passed": 0})
        bucket["cases"] += 1
        bucket["passed"] += int(r["passed"])
    latencies = [r["metrics"]["latency_ms"] for r in results]
    checks = [c for r in results for c in r["checks"]]
    return {
        "cases": len(results),
        "passed": sum(r["passed"] for r in results),
        "pass_rate": round(sum(r["passed"] for r in results) / len(results), 3) if results else 0.0,
        "checks": len(checks),
        "checks_passed": sum(c["passed"] for c in checks),
        "categories": categories,
        "latency_ms": {
            "mean": round(statistics.fmean(latencies), 1) if latencies else 0.0,
            "p50": _percentile(latencies, 50),
            "p95": _percentile(latencies, 95),
        },
        "model_calls": sum(r["metrics"]["model_calls"] for r in results),
        "tokens": sum(r["metrics"]["input_tokens"] + r["metrics"]["output_tokens"] for r in results),
        "cost_usd": round(sum(r["metrics"]["cost_usd"] for r in results), 6),
        "retrieval": {k: v for k, v in retrieval.items() if k != "rows"},
    }


async def run_evaluation(
    settings: Settings, case_ids: list[str] | None = None, llm: Any = None
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Run the selected cases (all by default) and the retrieval set. Returns (results, retrieval, summary)."""
    selected = [c for c in CASES if not case_ids or c.case_id in case_ids]
    if case_ids and len(selected) != len(set(case_ids)):
        unknown = sorted(set(case_ids) - {c.case_id for c in CASES})
        raise ValueError(f"unknown evaluation case(s): {unknown}")
    results = [await run_case(settings, case, llm) for case in selected]
    retrieval = await run_retrieval(settings)
    return results, retrieval, summarise(results, retrieval)


class EvaluationStore:
    def __init__(self, db: Database):
        self.db = db

    def start(self, tenant_id: str, provider: str, model: str, triggered_by: str) -> str:
        eval_id = new_id("eval")
        self.db.execute(
            "INSERT INTO evaluations(eval_id, tenant_id, status, provider, model, triggered_by, started_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (eval_id, tenant_id, "running", provider, model, triggered_by, datetime.now(UTC).isoformat()),
        )
        return eval_id

    def finish(
        self, eval_id: str, results: list[dict[str, Any]], retrieval: dict[str, Any], summary: dict[str, Any]
    ) -> None:
        self.db.execute(
            "UPDATE evaluations SET status=?, finished_at=?, summary_json=?, results_json=? WHERE eval_id=?",
            (
                "passed" if summary["passed"] == summary["cases"] else "failed",
                datetime.now(UTC).isoformat(),
                json.dumps(summary),
                json.dumps({"cases": results, "retrieval": retrieval}),
                eval_id,
            ),
        )

    def fail(self, eval_id: str, error: str) -> None:
        self.db.execute(
            "UPDATE evaluations SET status='error', finished_at=?, summary_json=? WHERE eval_id=?",
            (datetime.now(UTC).isoformat(), json.dumps({"error": error}), eval_id),
        )

    def _row(self, row: Any, full: bool) -> dict[str, Any]:
        data = {
            k: row[k] for k in ("eval_id", "status", "provider", "model", "triggered_by", "started_at", "finished_at")
        }
        data["summary"] = json.loads(row["summary_json"])
        if full:
            data.update(
                json.loads(row["results_json"]) if row["results_json"] != "[]" else {"cases": [], "retrieval": {}}
            )
        return data

    def list(self, tenant_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM evaluations WHERE tenant_id=? ORDER BY started_at DESC LIMIT ?", (tenant_id, limit)
        )
        return [self._row(r, full=False) for r in rows]

    def get(self, tenant_id: str, eval_id: str) -> dict[str, Any] | None:
        rows = self.db.query("SELECT * FROM evaluations WHERE tenant_id=? AND eval_id=?", (tenant_id, eval_id))
        return self._row(rows[0], full=True) if rows else None


async def evaluate_and_store(
    store: EvaluationStore,
    settings: Settings,
    tenant_id: str,
    provider: str,
    model: str,
    triggered_by: str,
    case_ids: list[str] | None = None,
    llm: Any = None,
) -> str:
    eval_id = store.start(tenant_id, provider, model, triggered_by)
    try:
        results, retrieval, summary = await run_evaluation(settings, case_ids, llm)
    except Exception as exc:
        store.fail(eval_id, f"{type(exc).__name__}: {exc}")
        raise
    store.finish(eval_id, results, retrieval, summary)
    return eval_id


__all__ = ["EvaluationStore", "evaluate_and_store", "run_case", "run_evaluation", "run_retrieval", "summarise"]
