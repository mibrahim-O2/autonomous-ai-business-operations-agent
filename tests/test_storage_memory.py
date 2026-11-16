from conftest import lead_task

from bizops.core.errors import RunNotFound
from bizops.core.models import RunState, RunStatus
from bizops.memory import ContextBuilder, MemoryStore
from bizops.memory.store import bm25_scores
from bizops.storage import Database
from bizops.storage.db import MIGRATIONS


def test_run_checkpoint_roundtrip_and_tenant_isolation(tmp_path):
    db = Database(tmp_path / "a.db")
    state = RunState(tenant_id="acme", task=lead_task(tenant_id="acme"))
    db.save_run(state)
    state.status = RunStatus.PAUSED
    db.save_run(state)
    loaded = db.load_run("acme", state.run_id)
    assert loaded.status == RunStatus.PAUSED and loaded.task.inputs["company"] == "Quillfield Partners"
    assert db.list_runs("other") == []
    try:
        db.load_run("other", state.run_id)
    except RunNotFound:
        pass
    else:
        raise AssertionError("a tenant read another tenant's run")


def test_migrations_are_idempotent_and_persist(tmp_path):
    path = tmp_path / "b.db"
    db = Database(path)
    db.kv_put("t", "ns", "k", {"v": 1})
    db.close()
    reopened = Database(path)  # must not re-run migrations or lose data
    reopened.migrate(MIGRATIONS)
    assert reopened.kv_get("t", "ns", "k") == {"v": 1}
    assert reopened.kv_get("other", "ns", "k") is None
    assert reopened.kv_delete("t", "ns", "k") and reopened.kv_list("t", "ns") == {}


def test_control_signal_requires_existing_run(tmp_path):
    db = Database(tmp_path / "c.db")
    try:
        db.set_control("t", "run_missing", "pause")
    except RunNotFound:
        pass
    else:
        raise AssertionError("signal accepted for a missing run")


def test_bm25_ranks_relevant_document_first():
    docs = ["Harborline Realty prefers morning meetings", "Invoice policy for late payment", "Realty market report"]
    scores = bm25_scores("harborline meetings", docs)
    assert scores.index(max(scores)) == 0 and scores[1] == 0
    assert bm25_scores("anything", []) == []


def test_memory_dedupes_facts_and_isolates_tenants(tmp_path):
    memory = MemoryStore(Database(tmp_path / "m.db"))
    first = memory.add_fact("acme", "Harborline Realty prefers morning meetings")
    assert memory.add_fact("acme", "harborline realty prefers morning meetings ") == first
    memory.add_episode("acme", "run_1", "Lead intake for Harborline finished verified", workflow="lead_intake")
    hits = memory.search("acme", "Harborline")
    assert {h.kind for h in hits} == {"semantic", "episodic"}
    assert memory.search("acme", "Harborline", kind="episodic")[0].metadata["run_id"] == "run_1"
    assert memory.search("globex", "Harborline") == []
    assert memory.search("acme", "zebra") == []


def test_context_builder_respects_budget(tmp_path):
    memory = MemoryStore(Database(tmp_path / "x.db"))
    for i in range(30):
        memory.add_fact("demo", f"Harborline fact number {i} " + "detail " * 40)
    state = RunState(tenant_id="demo", task=lead_task())
    from bizops.core.models import Plan, PlanStep

    step = PlanStep(agent="research", action="recall_context", description="recall Harborline")
    state.plan = Plan(steps=[step])
    text, stats = ContextBuilder(memory, token_budget=120).build(state, step, {"query": "Harborline"})
    assert stats["tokens_used"] <= 120 and (stats["truncated"] or stats["dropped"])
    assert text.startswith("## objective")  # the most important section always survives
    roomy, roomy_stats = ContextBuilder(memory, token_budget=6000).build(state, step, {"query": "Harborline"})
    assert "## memory" in roomy and roomy_stats["memories"] == 5 and not roomy_stats["dropped"]
