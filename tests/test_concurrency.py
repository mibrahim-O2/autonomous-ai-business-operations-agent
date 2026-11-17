"""Single-writer guarantees: leases, fencing tokens, and exactly-once side effects.

These tests reproduce the scenario raised by an external reviewer on issue #1:
a worker stalls past its lease, a second worker takes the run over, and the
first worker wakes up and tries to finish. The first worker must be rejected
and the side effect must happen exactly once.
"""

import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest
from conftest import lead_task, make_settings, run

from bizops.core.errors import StaleLease
from bizops.core.models import RunState, RunStatus, StepStatus, Task
from bizops.runtime import build_runtime
from bizops.storage import Database

EMAIL = "noor.abbasi@quillfield.example"


def two_workers(tmp_path, **settings):
    """Two separate runtimes (two database connections) on one database file, as two processes would be."""
    a = build_runtime(make_settings(tmp_path, **settings))
    b = build_runtime(make_settings(tmp_path, **settings))
    return a, b


def expire_lease(db: Database, run_id: str) -> None:
    """Make the current lease look as it would after the holder stalled past its expiry."""
    past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat(timespec="milliseconds")
    db.execute("UPDATE runs SET lease_expires_at=? WHERE run_id=?", (past, run_id))


def notes(rt) -> list[dict]:
    return rt.db.kv_get("demo", "crm.contacts", EMAIL)["notes"]


def events(rt, run_id: str) -> list[str]:
    return [e["kind"] for e in rt.tracer.timeline("demo", run_id)]


# Storage primitives ------------------------------------------------------------
def test_claim_is_exclusive_and_generation_increases(tmp_path):
    db = Database(tmp_path / "x.db")
    state = RunState(tenant_id="demo", task=lead_task())
    db.save_run(state)
    assert db.claim_run("demo", state.run_id, "a", 60) == 1
    assert db.claim_run("demo", state.run_id, "b", 60) is None  # held and not expired
    assert db.renew_lease("demo", state.run_id, "a", 1, 60) and not db.renew_lease("demo", state.run_id, "b", 1, 60)
    expire_lease(db, state.run_id)
    assert db.claim_run("demo", state.run_id, "b", 60) == 2  # takeover bumps the generation
    assert not db.lease_is_current("demo", state.run_id, "a", 1) and db.lease_is_current("demo", state.run_id, "b", 2)
    assert not db.renew_lease("demo", state.run_id, "a", 1, 60)  # the old holder cannot renew its way back
    db.release_lease("demo", state.run_id, "a", 1)  # a stale release changes nothing
    assert db.get_lease("demo", state.run_id)["lease_owner"] == "b"
    db.release_lease("demo", state.run_id, "b", 2)
    assert db.claim_run("demo", state.run_id, "c", 60) == 3  # generation is never reused


def test_stale_worker_cannot_commit_state(tmp_path):
    db = Database(tmp_path / "x.db")
    state = RunState(tenant_id="demo", task=lead_task())
    db.save_run(state)
    state.generation = db.claim_run("demo", state.run_id, "a", 60)
    expire_lease(db, state.run_id)
    db.claim_run("demo", state.run_id, "b", 60)
    state.status = RunStatus.FAILED
    with pytest.raises(StaleLease):
        db.save_run(state, fence=("a", state.generation))
    assert db.load_run("demo", state.run_id).status == RunStatus.PENDING  # nothing was written


def test_run_once_is_atomic_idempotent_and_fenced(tmp_path):
    db = Database(tmp_path / "x.db")
    effects = []

    def act(label):
        def action():
            db.kv_put("demo", "effects", str(len(effects)), label)
            effects.append(label)
            return {"by": label}

        return action

    assert db.run_once("demo", "run_1:s:k", "op", act("gen2"), fence_scope="run_1", fence_token=2) == (
        {"by": "gen2"},
        False,
    )
    assert db.run_once("demo", "run_1:s:k", "op", act("again"), fence_scope="run_1", fence_token=2) == (
        {"by": "gen2"},
        True,
    )
    with pytest.raises(StaleLease):  # an older generation is refused even for a different operation
        db.run_once("demo", "run_1:s:other", "op", act("gen1"), fence_scope="run_1", fence_token=1)
    assert effects == ["gen2"]

    def failing():
        db.kv_put("demo", "effects", "partial", "half done")
        raise RuntimeError("target system crashed")

    with pytest.raises(RuntimeError):
        db.run_once("demo", "run_1:s:crash", "op", failing, fence_scope="run_1", fence_token=3)
    assert db.kv_get("demo", "effects", "partial") is None  # the partial effect was rolled back
    assert db.run_once("demo", "run_1:s:crash", "op", act("retry"), fence_scope="run_1", fence_token=3)[1] is False


# The reviewer's scenario -----------------------------------------------------
class StallingModel:
    """Wraps the scripted model and stalls once, just before the model asks for the CRM note.

    At that point the step has already created the contact, and the note (the
    side effect under test) has not been written yet.
    """

    name, model = "stalling", "stalling-1"

    def __init__(self, inner):
        self.inner, self.stalled, self.release, self.used = inner, asyncio.Event(), asyncio.Event(), False

    async def complete(self, messages, tools=None):
        response = await self.inner.complete(messages, tools)
        if not self.used and any(call.tool == "crm_add_note" for call in response.tool_calls):
            self.used = True
            self.stalled.set()
            await self.release.wait()  # the slow model call: process A is stuck here
        return response


def test_stalled_worker_is_fenced_out_after_takeover(tmp_path):
    """Reviewer scenario, variant 1: A wakes up before it has started the side effect."""
    a, b = two_workers(tmp_path)

    async def scenario():
        await a.start()
        await b.start()
        a.orchestrator.heartbeat_enabled = False  # a frozen process cannot heartbeat
        model = StallingModel(a.llm)
        a.orchestrator.ctx.llm = model

        task_a = asyncio.create_task(a.orchestrator.submit(lead_task()))
        await asyncio.wait_for(model.stalled.wait(), 5)  # A is mid-step in qualify_lead
        run_id = a.db.list_runs("demo")[0]["run_id"]
        assert a.db.get_lease("demo", run_id)["generation"] == 1 and notes(a) == []

        expire_lease(a.db, run_id)  # A's lease runs out while it is stalled
        finished_by_b = await b.orchestrator.resume("demo", run_id)  # B takes over and completes
        assert finished_by_b.status == RunStatus.COMPLETED and finished_by_b.generation == 2
        assert len(notes(b)) == 1

        model.release.set()  # A wakes up and tries to carry on
        seen_by_a = await asyncio.wait_for(task_a, 5)
        return run_id, finished_by_b, seen_by_a

    run_id, finished_by_b, seen_by_a = run(scenario())
    # A was rejected as stale and wrote nothing.
    assert "run.stale_worker_stopped" in events(b, run_id)
    assert seen_by_a.generation == 2 and seen_by_a.status == RunStatus.COMPLETED  # A reports B's committed state
    # The side effect happened exactly once.
    assert len(notes(b)) == 1 and len(b.db.kv_list("demo", "email.drafts")) == 1
    # The final state is consistent and is B's.
    final = b.db.load_run("demo", run_id)
    assert final.model_dump() == finished_by_b.model_dump()
    assert final.final_report["outcome"] == "verified" and all(
        s.status == StepStatus.SUCCEEDED for s in final.plan.steps
    )
    lease = b.db.get_lease("demo", run_id)
    assert lease["generation"] == 2 and lease["lease_owner"] is None  # released by B; A's release was a no-op
    a.close()
    b.close()


def test_stalled_worker_cannot_complete_a_side_effect_already_in_flight(tmp_path):
    """Reviewer scenario, variant 2: A passed every local check and is inside the tool call when it stalls.

    This is the hard case: no check inside A can help, because A is already past
    them. The target system must refuse A's fencing token.
    """
    a, b = two_workers(tmp_path)

    async def scenario():
        await a.start()
        await b.start()
        a.orchestrator.heartbeat_enabled = False
        stalled, release = asyncio.Event(), asyncio.Event()
        real_call = a.mcp.call

        async def stalling_call(name, arguments, ctx):
            if name == "crm_add_note" and not stalled.is_set():
                stalled.set()
                await release.wait()  # A has decided to write the note and is about to send it
            return await real_call(name, arguments, ctx)

        a.mcp.call = stalling_call
        task_a = asyncio.create_task(a.orchestrator.submit(lead_task()))
        await asyncio.wait_for(stalled.wait(), 5)
        run_id = a.db.list_runs("demo")[0]["run_id"]

        expire_lease(a.db, run_id)
        finished_by_b = await b.orchestrator.resume("demo", run_id)
        assert finished_by_b.status == RunStatus.COMPLETED and len(notes(b)) == 1

        release.set()  # A now sends the note with fencing token 1
        seen_by_a = await asyncio.wait_for(task_a, 5)
        return run_id, finished_by_b, seen_by_a

    run_id, finished_by_b, seen_by_a = run(scenario())
    assert len(notes(b)) == 1  # the target refused A; the note exists once
    stale = [e for e in b.tracer.timeline("demo", run_id) if e["kind"] == "run.stale_worker_stopped"]
    assert len(stale) == 1 and "stale fencing token 1" in stale[0]["reason"] and stale[0]["generation"] == 1
    assert seen_by_a.model_dump() == b.db.load_run("demo", run_id).model_dump() == finished_by_b.model_dump()
    a.close()
    b.close()


def test_stalled_worker_that_acts_first_is_not_repeated_by_its_successor(tmp_path):
    """The mirror image: A's side effect lands, then A dies before recording it. B must not repeat it."""
    a, b = two_workers(tmp_path)

    async def scenario():
        await a.start()
        await b.start()
        a.orchestrator.heartbeat_enabled = False
        real_call = a.mcp.call

        async def crash_after_note(name, arguments, ctx):
            result = await real_call(name, arguments, ctx)
            if name == "crm_add_note":
                raise asyncio.CancelledError  # the process dies right after the note was written
            return result

        a.mcp.call = crash_after_note
        with pytest.raises(asyncio.CancelledError):
            await a.orchestrator.submit(lead_task())
        run_id = a.db.list_runs("demo")[0]["run_id"]
        assert len(notes(a)) == 1
        in_flight = a.db.load_run("demo", run_id)
        assert in_flight.status == RunStatus.RUNNING  # interrupted mid-step, exactly as a crash leaves it

        finished = await b.orchestrator.resume("demo", run_id)  # A released on the way out; no expiry needed
        return run_id, finished

    run_id, finished = run(scenario())
    assert finished.status == RunStatus.COMPLETED and finished.final_report["outcome"] == "verified"
    assert len(notes(b)) == 1  # B re-ran the step; the target recognised the idempotency key
    qualify = next(s for s in finished.plan.steps if s.action == "qualify_lead")
    assert qualify.attempts == 2 and [t.tool for t in qualify.tool_calls].count("crm_add_note") == 1
    a.close()
    b.close()


class RewordingModel:
    """A model that words the CRM note differently, as a live model may on a second attempt."""

    name, model = "rewording", "rewording-1"

    def __init__(self, inner):
        self.inner = inner

    async def complete(self, messages, tools=None):
        response = await self.inner.complete(messages, tools)
        for call in response.tool_calls:
            if call.tool == "crm_add_note":
                call.arguments["note"] = "Reworded on the second attempt: " + call.arguments["note"]
        return response


def test_interrupted_side_effect_is_not_duplicated_when_the_model_rewords_it(tmp_path):
    """A's note lands and A dies before recording it; B's model then phrases the note differently.

    Different arguments would mean a different idempotency key and a second note. The write-ahead
    intent makes B send A's original call again instead, which the target recognises.
    """
    a, b = two_workers(tmp_path)

    async def scenario():
        await a.start()
        await b.start()
        real_call = a.mcp.call

        async def crash_after_note(name, arguments, ctx):
            result = await real_call(name, arguments, ctx)
            if name == "crm_add_note":
                raise asyncio.CancelledError
            return result

        a.mcp.call = crash_after_note
        with pytest.raises(asyncio.CancelledError):
            await a.orchestrator.submit(lead_task())
        run_id = a.db.list_runs("demo")[0]["run_id"]
        interrupted = next(s for s in a.db.load_run("demo", run_id).plan.steps if s.action == "qualify_lead")
        assert interrupted.in_flight is not None and interrupted.in_flight.tool == "crm_add_note"
        original = notes(a)[0]

        b.orchestrator.ctx.llm = RewordingModel(b.llm)
        return run_id, original, await b.orchestrator.resume("demo", run_id)

    run_id, original, finished = run(scenario())
    assert finished.status == RunStatus.COMPLETED
    assert notes(b) == [original]  # one note, with the wording that was actually sent
    assert "tool.intent_recovered" in events(b, run_id)
    assert all(s.in_flight is None for s in finished.plan.steps)
    a.close()
    b.close()


# Simultaneous resume ---------------------------------------------------------
def test_simultaneous_resume_from_two_workers_executes_once(tmp_path):
    a, b = two_workers(tmp_path)
    run(a.start())
    run(b.start())
    state, _ = a.orchestrator.create(lead_task())
    a.orchestrator.request_pause("demo", state.run_id)
    paused = run(a.orchestrator.drive("demo", state.run_id))
    assert paused.status == RunStatus.PAUSED and a.db.get_lease("demo", state.run_id)["lease_owner"] is None

    barrier, results = threading.Barrier(2), {}

    def resume(name, rt):
        barrier.wait()  # release both workers at the same instant
        results[name] = asyncio.run(rt.orchestrator.resume("demo", state.run_id))

    threads = [threading.Thread(target=resume, args=("a", a)), threading.Thread(target=resume, args=("b", b))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)

    final = a.db.load_run("demo", state.run_id)
    timeline = events(a, state.run_id)
    assert final.status == RunStatus.COMPLETED and final.final_report["outcome"] == "verified"
    # Exactly one worker executed; the other was turned away at the claim (or found the run finished).
    assert timeline.count("run.resumed") == 1 and timeline.count("run.completed") == 1
    assert final.steps_executed == 5 and final.generation == 2
    assert len(notes(a)) == 1 and len(a.db.kv_list("demo", "email.drafts")) == 1
    # The worker turned away reports whatever was committed at that instant: still paused if the
    # winner had claimed but not yet written, running, or already completed.
    assert {r.status for r in results.values()} <= {RunStatus.COMPLETED, RunStatus.RUNNING, RunStatus.PAUSED}
    assert RunStatus.COMPLETED in {r.status for r in results.values()}
    a.close()
    b.close()


def test_many_simultaneous_claims_have_one_winner(tmp_path):
    path = tmp_path / "x.db"
    seed = Database(path)
    state = RunState(tenant_id="demo", task=lead_task())
    seed.save_run(state)
    workers, barrier, won = 8, threading.Barrier(8), []

    def claim(index):
        db = Database(path)  # each worker has its own connection, like a separate process
        barrier.wait()
        generation = db.claim_run("demo", state.run_id, f"worker_{index}", 60)
        if generation is not None:
            won.append((index, generation))
        db.close()

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert len(won) == 1 and won[0][1] == 1 and seed.get_lease("demo", state.run_id)["generation"] == 1


def test_heartbeat_keeps_a_slow_but_healthy_worker_in_charge(tmp_path):
    """A healthy worker with a long step must not lose its run: the heartbeat renews the lease."""
    a, b = two_workers(tmp_path, run_lease_seconds=0.15)

    async def scenario():
        await a.start()
        await b.start()
        model = StallingModel(a.llm)
        a.orchestrator.ctx.llm = model
        task_a = asyncio.create_task(a.orchestrator.submit(lead_task()))
        await asyncio.wait_for(model.stalled.wait(), 5)
        run_id = a.db.list_runs("demo")[0]["run_id"]
        await asyncio.sleep(0.5)  # several lease lengths pass while A is slow but alive
        rejected = await b.orchestrator.resume("demo", run_id)
        assert rejected.status == RunStatus.RUNNING  # B could not take the run
        model.release.set()
        return run_id, await asyncio.wait_for(task_a, 5)

    run_id, finished = run(scenario())
    assert finished.status == RunStatus.COMPLETED and finished.generation == 1 and len(notes(a)) == 1
    assert "run.claim_rejected" in events(a, run_id) and "run.stale_worker_stopped" not in events(a, run_id)
    a.close()
    b.close()


def test_cancel_does_not_override_a_running_worker(tmp_path):
    a, b = two_workers(tmp_path)

    async def scenario():
        await a.start()
        await b.start()
        model = StallingModel(a.llm)
        a.orchestrator.ctx.llm = model
        task_a = asyncio.create_task(a.orchestrator.submit(lead_task()))
        await asyncio.wait_for(model.stalled.wait(), 5)
        run_id = a.db.list_runs("demo")[0]["run_id"]
        seen = b.orchestrator.cancel("demo", run_id)  # B cannot claim; it only leaves the signal
        assert seen.status == RunStatus.RUNNING
        model.release.set()
        return await asyncio.wait_for(task_a, 5)

    finished = run(scenario())
    assert finished.status == RunStatus.CANCELLED  # A honoured the signal at its next step boundary
    a.close()
    b.close()


def test_job_result_from_a_superseded_attempt_is_discarded(tmp_path):
    rt = build_runtime(make_settings(tmp_path))
    job_id, _ = rt.queue.enqueue_task(lead_task())
    first = rt.queue.claim_due()
    second = rt.queue.claim_due(now=datetime.now(UTC) + timedelta(minutes=11))  # reclaimed after the first stalled
    assert (first["attempts"], second["attempts"]) == (1, 2)
    assert rt.queue.finish(job_id, "done", {"by": "second"}, attempt=2) is True
    assert rt.queue.finish(job_id, "done", {"by": "first, late"}, attempt=1) is False
    assert rt.queue.get(job_id)["result"] == {"by": "second"}
    rt.close()


def test_superseded_worker_files_no_approval_request(tmp_path):
    """A worker that lost its run must not file approval requests a person would then be asked to decide.

    The stale worker wakes with a refund call worded differently from anything its successor will send
    (a live model may do this), so the request it would file is not one the successor would reuse.
    """
    a, b = two_workers(tmp_path)

    async def scenario():
        await a.start()
        await b.start()
        a.orchestrator.heartbeat_enabled = False
        stalled, release = asyncio.Event(), asyncio.Event()
        inner = a.llm

        class StallBeforeRefund:
            name, model = "stall", "stall-1"

            async def complete(self, messages, tools=None):
                response = await inner.complete(messages, tools)
                for call in response.tool_calls:
                    if call.tool == "billing_issue_refund" and not stalled.is_set():
                        stalled.set()
                        await release.wait()
                        call.arguments["amount"] = 300.0  # differs from what the successor will ask for
                return response

        a.orchestrator.ctx.llm = StallBeforeRefund()
        refund = Task(objective="Handle a refund request", workflow="refund_request", inputs={"invoice_id": "INV-1005"})
        task_a = asyncio.create_task(a.orchestrator.submit(refund))
        await asyncio.wait_for(stalled.wait(), 5)
        run_id = a.db.list_runs("demo")[0]["run_id"]
        expire_lease(a.db, run_id)
        assert b.db.claim_run("demo", run_id, "successor", 60) == 2  # taken over, not yet at the refund step
        release.set()
        await asyncio.wait_for(task_a, 5)
        return run_id

    run_id = run(scenario())
    assert b.approvals.list("demo", run_id=run_id) == []  # nobody is asked about the stale worker's call
    assert "run.stale_worker_stopped" in events(b, run_id)
    a.close()
    b.close()
