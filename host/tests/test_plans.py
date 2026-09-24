"""The plan harness — what it refuses, and what it refuses to forget.

Two failures are worth the whole file. The first is a stage that closes on a
claim: the table exists so "is this done" answers with a stage id and its proof,
and a `done` row nobody verified is worse than no row at all, because it reads
exactly like an earned one. The second is a re-parse eating history — the user
edits the plan markdown all day, and a stage that already ran must survive being
dropped out of the file.

The store seam: `plans` resolves its database through `store.get_store()` on
every call, so pointing that at a `tmp_path` store is the whole isolation. The
package's conftest already redirects the state dir for the run; this narrows it
to a fresh file per test so no test reads another's stages.
"""

import pytest

from jstack_host import plans, store
from jstack_host.store import SessionStore


@pytest.fixture(autouse=True)
def plan_store(tmp_path, monkeypatch):
    """A private store for each test, in front of conftest's shared probe."""
    s = SessionStore(db_path=tmp_path / "plans.sqlite")
    monkeypatch.setattr(store, "get_store", lambda: s)
    return s


def _stage(ordinal, title, kind="none", spec="", body=""):
    return {"ordinal": ordinal, "title": title, "verify_kind": kind,
            "verify_spec": spec, "body": body}


def _only_stage(plan_id, **kw):
    plans.set_stages(plan_id, [_stage(1, "the stage", **kw)])
    return plans.stages(plan_id)[0]["id"]


# ── the refusal ──────────────────────────────────────────────────────────────

def test_a_command_stage_cannot_close_without_a_proof():
    plan_id = plans.open_plan("harness", session_id="s1")
    stage_id = _only_stage(plan_id, kind="command", spec="pytest -q")
    plans.stage_start(stage_id, session_id="s1")

    with pytest.raises(plans.VerificationMissing) as caught:
        plans.stage_done(stage_id)

    message = str(caught.value)
    assert stage_id in message and "command" in message and "pytest -q" in message
    # The refusal wrote nothing: the stage is exactly as `stage_start` left it.
    row = plans.stages(plan_id)[0]
    assert row["status"] == "running"
    assert row["finished_at"] == 0

    plans.add_proof(stage_id, "command", True, detail="pytest -q", exit_code=0)
    plans.stage_done(stage_id)
    assert plans.stages(plan_id)[0]["status"] == "done"


def test_a_stage_declaring_none_closes_with_no_proof():
    """`none` is a real declaration — not every stage has a machine check, and a
    harness that demanded one would be satisfied with a fabricated proof."""
    plan_id = plans.open_plan("harness")
    stage_id = _only_stage(plan_id, kind="none")
    plans.stage_start(stage_id)
    plans.stage_done(stage_id)
    assert plans.stages(plan_id)[0]["status"] == "done"
    assert plans.proofs(stage_id) == []


def test_a_failing_proof_does_not_satisfy_the_gate():
    """The gate reads `ok`, never the presence of a row — a recorded failure is
    evidence the check ran and said no."""
    plan_id = plans.open_plan("harness")
    stage_id = _only_stage(plan_id, kind="artifact", spec="dist/app.zip")
    plans.stage_start(stage_id)
    plans.add_proof(stage_id, "artifact", False, detail="dist/app.zip missing")

    with pytest.raises(plans.VerificationMissing):
        plans.stage_done(stage_id)
    assert plans.stages(plan_id)[0]["status"] == "running"
    assert len(plans.proofs(stage_id)) == 1


# ── many-to-many ─────────────────────────────────────────────────────────────

def test_two_sessions_share_one_plan_and_a_stage_records_its_runner():
    plan_id = plans.open_plan("shared", session_id="author-1")
    plans.join(plan_id, "helper-2", "subagent")
    plans.set_stages(plan_id, [_stage(1, "first"), _stage(2, "second")])
    first, second = plans.stages(plan_id)

    plans.stage_start(first["id"], session_id="author-1")
    plans.stage_start(second["id"], session_id="helper-2", env={"engine": "codex"})

    assert [p["id"] for p in plans.plans_for_session("author-1")] == [plan_id]
    assert [p["id"] for p in plans.plans_for_session("helper-2")] == [plan_id]
    by_ordinal = {s["ordinal"]: s for s in plans.stages(plan_id)}
    assert by_ordinal[1]["session_id"] == "author-1"
    assert by_ordinal[2]["session_id"] == "helper-2"
    assert by_ordinal[2]["env"] == '{"engine": "codex"}'
    # A rejoin with no role must not demote the author.
    plans.join(plan_id, "author-1")
    with plan_store_conn() as db:
        role = db.execute("SELECT role FROM plan_sessions WHERE session_id = 'author-1'"
                          ).fetchone()["role"]
    assert role == "author"


def plan_store_conn():
    return store.get_store().conn()


# ── re-parse ─────────────────────────────────────────────────────────────────

def test_a_reparse_keeps_finished_work_and_drops_only_pending_tails():
    plan_id = plans.open_plan("editable", session_id="s1")
    plans.set_stages(plan_id, [_stage(1, "build"), _stage(2, "ship"),
                               _stage(3, "tidy")])
    build, ship, tidy = plans.stages(plan_id)

    plans.stage_start(build["id"], session_id="s1")
    plans.stage_done(build["id"])
    plans.stage_start(ship["id"], session_id="s2")
    plans.add_proof(ship["id"], "commit", True, detail="deadbeef")
    plans.stage_done(ship["id"])

    # The user rewrites the markdown down to one stage, with a new title on it.
    plans.set_stages(plan_id, [_stage(1, "build, renamed")])

    rows = {s["ordinal"]: s for s in plans.stages(plan_id)}
    assert rows[1]["title"] == "build, renamed"
    assert rows[1]["status"] == "done"
    assert rows[1]["session_id"] == "s1"
    assert 2 in rows, "a finished stage is history; a re-parse does not erase it"
    assert rows[2]["status"] == "done" and rows[2]["session_id"] == "s2"
    assert [p["detail"] for p in plans.proofs(ship["id"])] == ["deadbeef"]
    assert 3 not in rows, "a still-pending trailing stage goes with the markdown"
    assert plans.stages(plan_id) == sorted(plans.stages(plan_id),
                                           key=lambda s: s["ordinal"])
    assert tidy["id"] not in {s["id"] for s in plans.stages(plan_id)}


# ── run_verify ───────────────────────────────────────────────────────────────

def test_run_verify_passing_closes_the_gate(tmp_path):
    plan_id = plans.open_plan("verified")
    stage_id = _only_stage(plan_id, kind="command", spec="echo green")
    plans.stage_start(stage_id)

    proof = plans.run_verify(stage_id, cwd=str(tmp_path), timeout=30)
    assert proof["ok"] == 1 and proof["exit_code"] == 0
    assert "green" in proof["output"]
    plans.stage_done(stage_id)
    assert plans.stages(plan_id)[0]["status"] == "done"


def test_run_verify_failing_records_the_exit_code_and_the_gate_stays_shut():
    plan_id = plans.open_plan("verified")
    stage_id = _only_stage(plan_id, kind="command", spec="echo boom >&2; exit 3")
    plans.stage_start(stage_id)

    proof = plans.run_verify(stage_id, timeout=30)
    assert proof["ok"] == 0 and proof["exit_code"] == 3
    assert "boom" in proof["output"]
    with pytest.raises(plans.VerificationMissing):
        plans.stage_done(stage_id)
    assert plans.stages(plan_id)[0]["status"] == "running"


def test_run_verify_refuses_a_kind_it_cannot_run():
    """No invented proof for a kind nobody can execute."""
    plan_id = plans.open_plan("verified")
    stage_id = _only_stage(plan_id, kind="manual", spec="the user looked at it")
    with pytest.raises(ValueError):
        plans.run_verify(stage_id)
    assert plans.proofs(stage_id) == []


def test_a_long_output_is_kept_as_a_tail():
    """A proof row is evidence, not a log file: the end is what says why."""
    plan_id = plans.open_plan("verified")
    stage_id = _only_stage(plan_id, kind="command",
                           spec=f"python3 -c \"print('x' * {plans.OUTPUT_TAIL * 2})\"")
    proof = plans.run_verify(stage_id, timeout=60)
    assert len(proof["output"]) < plans.OUTPUT_TAIL * 2
    assert "truncated" in proof["output"]


# ── the one read the API layer makes ─────────────────────────────────────────

def test_work_returns_the_open_plan_with_its_stages_in_order():
    done_plan = plans.open_plan("finished", session_id="s1")
    plans.finish(done_plan)
    plan_id = plans.open_plan("current", session_id="s1")
    plans.activate(plan_id)
    plans.set_stages(plan_id, [_stage(2, "second"), _stage(1, "first"),
                               _stage(3, "third")])
    stage_id = plans.stages(plan_id)[0]["id"]
    plans.set_tasks(stage_id, [{"subject": "a", "status": "done", "native_id": "t1"},
                               {"subject": "b", "status": "pending", "native_id": "t2"}])

    got = plans.work("s1")
    assert got["plan"]["id"] == plan_id and got["plan"]["status"] == "active"
    assert [s["title"] for s in got["stages"]] == ["first", "second", "third"]
    assert [t["subject"] for t in got["tasks"][stage_id]] == ["a", "b"]
    assert plans.work("nobody") == {"plan": None, "stages": [], "tasks": {}}


def test_a_task_list_reconciles_rather_than_churning_ids():
    plan_id = plans.open_plan("tasks")
    stage_id = _only_stage(plan_id)
    plans.set_tasks(stage_id, [{"subject": "a", "status": "pending", "native_id": "t1"},
                               {"subject": "b", "status": "pending", "native_id": "t2"}])
    first_ids = [t["id"] for t in plans.tasks(stage_id)]

    plans.upsert_task(stage_id, "t2", "b", "in_progress")
    plans.upsert_task(stage_id, "t3", "c", "pending")
    rows = plans.tasks(stage_id)
    assert [t["id"] for t in rows][:2] == first_ids
    assert [t["status"] for t in rows] == ["pending", "in_progress", "pending"]

    plans.set_tasks(stage_id, [{"subject": "a", "status": "done", "native_id": "t1"}])
    assert [(t["id"], t["status"]) for t in plans.tasks(stage_id)] == [(first_ids[0], "done")]


def test_set_stages_takes_objects_as_well_as_dicts():
    """The parser hands back objects; nothing here imports the parser to find out."""
    class Parsed:
        def __init__(self, ordinal, title):
            self.ordinal, self.title = ordinal, title
            self.verify_kind, self.verify_spec, self.body = "command", "true", "why"

    plan_id = plans.open_plan("duck")
    plans.set_stages(plan_id, [Parsed(1, "from an object")])
    row = plans.stages(plan_id)[0]
    assert (row["title"], row["verify_kind"], row["body"]) == \
        ("from an object", "command", "why")


def test_list_plans_can_hide_the_closed_ones():
    open_id = plans.open_plan("open one")
    closed = plans.open_plan("closed one")
    plans.abandon(closed)
    assert [p["id"] for p in plans.list_plans(include_done=False)] == [open_id]
    assert {p["id"] for p in plans.list_plans()} == {open_id, closed}
