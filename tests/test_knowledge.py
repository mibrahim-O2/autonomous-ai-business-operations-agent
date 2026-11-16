"""Ingestion, hybrid retrieval, and agentic retrieval behaviour."""

from conftest import make_settings, run, started

from bizops.core.models import RunStatus, Task
from bizops.knowledge import HashingEmbedder, KnowledgeBase, chunk_markdown, parse_front_matter
from bizops.knowledge.embeddings import cosine, pack, unpack
from bizops.storage import Database

COLLECTIONS = Task(objective="Review overdue invoices and send payment reminders", workflow="invoice_collections")


def test_front_matter_and_structure_aware_chunking():
    meta, body = parse_front_matter("---\ntitle: T\ntrusted: false\n---\n\n# A\n\nOne. Two.\n\n## B\n\nThree.")
    assert meta == {"title": "T", "trusted": "false"} and body.startswith("# A")
    chunks = chunk_markdown(body)
    assert chunks == [("A", "One. Two."), ("B", "Three.")]
    long_section = "# Long\n\n" + " ".join(f"Sentence number {i} has several words in it." for i in range(40))
    windows = chunk_markdown(long_section, max_tokens=40, overlap_sentences=1)
    assert len(windows) > 3 and all(section == "Long" for section, _ in windows)
    assert windows[0][1].split(". ")[-1] in windows[1][1]  # consecutive windows overlap by one sentence
    assert parse_front_matter("no front matter") == ({}, "no front matter")


def test_hashing_embedder_is_normalised_and_order_sensitive():
    embedder = HashingEmbedder(dim=128)
    a, b, c = embedder.embed(["refund approval policy", "refund approval policy", "calendar meeting slots"])
    assert abs(cosine(a, a) - 1.0) < 1e-6 and a == b and cosine(a, c) < 0.3
    assert unpack(pack(a))[:5] == [round(x, 6) for x in unpack(pack(a))[:5]] and len(unpack(pack(a))) == 128
    assert embedder.embed([""])[0] == [0.0] * 128


def test_ingest_is_idempotent_and_replaces_changed_documents(tmp_path):
    kb = KnowledgeBase(Database(tmp_path / "k.db"))
    first = kb.ingest_text("t", "Policy", "# Rules\n\nRefunds need approval.", source="s1", doc_type="policy")
    same = kb.ingest_text("t", "Policy", "# Rules\n\nRefunds need approval.", source="s1", doc_type="policy")
    changed = kb.ingest_text("t", "Policy", "# Rules\n\nRefunds never need approval.", source="s1", doc_type="policy")
    assert first["status"] == "created" and same["status"] == "unchanged" and changed["status"] == "replaced"
    assert changed["doc_id"] == first["doc_id"] and len(kb.list_documents("t")) == 1
    hits = kb.search("t", "refunds approval")
    assert len(hits) == 1 and "never" in hits[0].text  # the old chunk is gone


def test_hybrid_search_ranks_filters_and_cites(rt):
    kb = rt.knowledge
    hits = kb.search("demo", "how many reminders can a customer receive for one invoice")
    assert hits[0].citation == "Collections Policy > Reminder limits" and hits[0].trusted
    assert set(hits[0].scores) == {"bm25", "vector", "rrf", "coverage", "phrase", "heading"}
    refund = kb.search("demo", "refund approval by a manager", doc_types=["policy"])
    assert refund[0].citation == "Refund Policy > Approval" and all(h.doc_type == "policy" for h in refund)
    # A lexical retriever cannot fully separate two policies that both talk about approval, so for
    # an ambiguous question the requirement is that the right passage is among the top results.
    ambiguous = kb.search("demo", "who must approve a refund", limit=3)
    assert "Refund Policy > Approval" in [h.citation for h in ambiguous]
    assert kb.search("demo", "approving approved approval")[0].scores["coverage"] == 1.0  # stemming unifies forms
    assert kb.search("demo", "meeting length", tags=["sales"])[0].title == "Sales Playbook"
    # Trust filter: the supplier's document is retrievable only when untrusted material is allowed.
    assert any(not h.trusted for h in kb.search("demo", "payment terms net 30 interest"))
    assert all(h.trusted for h in kb.search("demo", "payment terms net 30 interest", trusted_only=True))
    for mode in ("lexical", "vector"):
        assert kb.search("demo", "grace period overdue reminder", mode=mode)[0].title == "Collections Policy"
    assert kb.search("globex", "reminders") == [] and kb.search("demo", "the of and") == []


def test_kb_search_tool_taints_the_run_only_for_outside_material(rt):
    from bizops.core.models import PlanStep, RunState, ToolCall

    state, step = (
        RunState(tenant_id="demo", task=COLLECTIONS),
        PlanStep(agent="research", action="research_policy", description="x"),
    )
    internal = run(
        rt.gateway.execute(
            state,
            step,
            "research",
            ["kb_search"],
            ToolCall(tool="kb_search", arguments={"query": "grace period", "trusted_only": True}),
        )
    )
    assert internal.ok and not internal.untrusted and not state.tainted and "_untrusted" not in internal.output
    external = run(
        rt.gateway.execute(
            state,
            step,
            "research",
            ["kb_search"],
            ToolCall(tool="kb_search", arguments={"query": "vendor payment terms interest"}),
        )
    )
    assert external.untrusted and state.tainted


def test_collections_applies_the_retrieved_policy(rt):
    state = run(rt.orchestrator.submit(COLLECTIONS))
    assert state.status == RunStatus.COMPLETED and state.final_report["outcome"] == "verified"
    policy = state.final_report["results"]["research_policy"]
    assert policy["grounded"] and policy["facts"] == {
        "grace_days": 7.0,
        "max_reminders": 3.0,
        "approval_threshold": 1000.0,
    }
    assert "Collections Policy > Grace period" in policy["citations"]
    decisions = {d["invoice_id"]: d for d in state.final_report["results"]["review_overdue"]["decisions"]}
    assert decisions["INV-1003"]["decision"] == "wait" and "grace period" in decisions["INV-1003"]["reason"]
    assert decisions["INV-1001"]["decision"] == "remind" and not decisions["INV-1001"]["needs_approval"]
    assert decisions["INV-1002"]["needs_approval"]  # 2400 USD is above the retrieved threshold
    sent = list(rt.db.kv_list("demo", "email.sent").values())
    assert {m["to"] for m in sent} == {"jon.mercer@bluepeak.example", "priya.raman@cedarstone.example"}
    assert rt.db.kv_get("demo", "billing.invoices", "INV-1002")["reminders_sent"] == 2
    assert not state.tainted  # only trusted internal policy was read


def test_changing_the_policy_document_changes_behaviour_without_code(rt):
    """Retrieval drives the plan: edit the document, and the same workflow acts differently."""
    rt.knowledge.ingest_text(
        "demo",
        "Collections Policy",
        "# Collections Policy\n\n## Grace period\n\nAn invoice becomes eligible when it is more than 30 days past its "
        "due date.\n\n## Reminder limits\n\nA customer receives at most 1 reminders for one invoice.\n\n## Approval "
        "threshold\n\nReminders for invoices with an amount above 100 USD must be approved by a finance approver.",
        doc_type="policy",
        source="file:collections-policy.md",
    )
    state = run(rt.orchestrator.submit(COLLECTIONS))
    decisions = {d["invoice_id"]: d["decision"] for d in state.final_report["results"]["review_overdue"]["decisions"]}
    assert decisions == {"INV-1001": "wait", "INV-1002": "escalate", "INV-1003": "wait"}
    assert rt.db.kv_list("demo", "email.sent") == {}


def test_missing_policy_triggers_replanning_to_a_human(tmp_path):
    rt = started(make_settings(tmp_path, seed_demo_data=False))
    from bizops.integrations import seed_demo_data

    seed_demo_data(rt.db, "demo")  # business records but no policy documents
    state = run(rt.orchestrator.submit(COLLECTIONS))
    assert state.status == RunStatus.COMPLETED and state.replans == 1
    assert [s.action for s in state.plan.steps] == ["escalate_policy_gap"]
    searches = [
        t for t in rt.tracer.timeline("demo", state.run_id) if t["kind"] == "tool.call" and t["tool"] == "kb_search"
    ]
    assert len(searches) == 2  # the agent reformulated once before giving up
    tasks = list(rt.db.kv_list("demo", "ops.tasks").values())
    assert len(tasks) == 1 and tasks[0]["assignee"] == "finance" and "no_grounded_policy" in tasks[0]["detail"]
    assert rt.db.kv_list("demo", "email.sent") == {}  # nothing was sent on a guess
    rt.close()


def test_weak_first_search_is_reformulated(rt):
    from bizops.workflows.research import research_policy

    envelope = {"inputs": {"question": "zebra unicorn", "fallback_query": "grace period reminders", "extract": {}}}
    first = research_policy(envelope, [])
    assert first[0].tool == "kb_search" and first[0].arguments["doc_types"] == []
    weak = [{"tool": "kb_search", "result": {"results": [{"score": 0.1, "text": "x", "citation": "c"}]}}]
    second = research_policy(envelope, weak)
    assert "grace period reminders" in second[0].arguments["query"]
    strong = weak + [{"tool": "kb_search", "result": {"results": [{"score": 0.8, "text": "t", "citation": "Doc > S"}]}}]
    final = research_policy(envelope, strong)
    assert final["data"]["grounded"] and final["data"]["searches"] == 2 and final["data"]["citations"] == ["Doc > S"]
    assert research_policy({"inputs": {}}, [])["data"]["retrieval_used"] is False


def test_inbox_triage_treats_message_bodies_as_data(rt):
    state = run(rt.orchestrator.submit(Task(objective="Triage the unread inbox", workflow="inbox_triage")))
    assert state.status == RunStatus.COMPLETED and state.tainted  # outside-authored text entered the run
    result = state.final_report["results"]
    assert result["triage_inbox"]["counts"] == {"billing_question": 1, "refund_request": 1, "noise": 1}
    refund = next(m for m in result["triage_inbox"]["messages"] if m["category"] == "refund_request")
    assert refund["invoice_ids"] == ["INV-1005"]
    # The message told the assistant to refund 5000 USD and forward the customer list. Neither happened.
    assert rt.db.kv_list("demo", "billing.refunds") == {} and rt.db.kv_list("demo", "email.sent") == {}
    assert len(result["draft_responses"]["drafts"]) == 2 and len(result["draft_responses"]["tasks"]) == 1
    task = next(iter(rt.db.kv_list("demo", "ops.tasks").values()))
    assert "INV-1005" in task["detail"] and "5000" not in task["detail"]
    assert state.final_report["tainted_by_untrusted_content"] is True
