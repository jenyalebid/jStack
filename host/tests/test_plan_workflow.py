"""One plan, two paths through it — and one record either way.

`use_subagents` decides WHO works a stage: the session itself, inline, or one
subagent per stage spawned with the stage's id. It must not decide what the
work leaves behind. The moment the row shape depends on the path, "is this
done" needs to know who ran it before it can be answered, and the whole point
of the table — a stage id and its proof instead of a conversation — is gone.
So the first test here compares the actual rows of the same plan run both
ways, field by field, and the rest pin the four places that could drift apart:
attribution, the dispatch snapshot, a setting flipped mid-plan, and the
refusal.

NOTHING HERE SPAWNS A SUBAGENT. The subagent path is driven by calling the
same three writers with the subagent's session id, which is precisely what a
real one does: it is handed `JSTACK_STAGE_ID`, it runs its own stage's
`Verify:`, and it writes its own rows through this module. A real spawn would
add a process boundary and prove nothing further about the record, which is
what is under test.

The second half is the enforcement holes — a stage that closed on a misspelled
`Verify:` line, evidence filed against a stage id that does not exist, and
writers that reported success on nothing. Each was reproduced against a real
store before it was fixed.

The store seam is `test_plans.py`'s: `store.get_store()` resolved per call, so
a `tmp_path` store per test is the whole isolation. `environment` resolves
through the same seam, so one patch covers both.
"""

import pytest

from jstack_host import environment, plan_parse, plans, store
from jstack_host.store import SessionStore


@pytest.fixture(autouse=True)
def plan_store(tmp_path, monkeypatch):
    """A private store for each test, in front of conftest's shared probe."""
    s = SessionStore(db_path=tmp_path / "plans.sqlite")
    monkeypatch.setattr(store, "get_store", lambda: s)
    return s


#: The plan both paths run. Two stages so a per-stage fact cannot hide in a
#: single row, and a `command` gate on each so the proof rows are earned by an
#: actual process rather than asserted into existence.
PLAN_STAGES = (
    {"ordinal": 1, "title": "build", "body": "the first unit",
     "verify_kind": "command", "verify_spec": "echo green"},
    {"ordinal": 2, "title": "ship", "body": "the second unit",
     "verify_kind": "command", "verify_spec": "echo shipped"},
)


def _run_plan(title, stage_sessions, *, subagents):
    """Take the plan through start → verify → done, one session per stage.

    `stage_sessions` is the session id that works each stage: the author's own
    for the inline path, a fresh one per stage for the dispatched path. The
    setting is set on every session that runs a stage because the snapshot
    records what was in force for the sitting that did the work.
    """
    author = stage_sessions[0]
    for session in dict.fromkeys(stage_sessions):
        environment.set_value("use_subagents", subagents, session_id=session)
    plan_id = plans.open_plan(title, session_id=author)
    plans.activate(plan_id)
    plans.set_stages(plan_id, [dict(s) for s in PLAN_STAGES])
    for row, session in zip(plans.stages(plan_id), stage_sessions):
        plans.join(plan_id, session)
        plans.stage_start(row["id"], session_id=session)
        plans.run_verify(row["id"], timeout=30)
        plans.stage_done(row["id"])
    return plan_id


#: Columns that MUST differ between two runs of the same plan: the row's own
#: id, what it hangs off, who ran it, when, how long it took, and the snapshot
#: saying which path it was. For these the comparison asserts POPULATED; every
#: other column is compared literally. "Identical rows" cannot mean identical
#: ids, and a comparison that dropped these columns would pass on a path that
#: never recorded a runner at all.
_PER_RUN = ("id", "plan_id", "stage_id", "session_id", "env", "started_at",
            "finished_at", "updated_at", "created_at", "duration_ms")


def _shape(row):
    return {k: ("<set>" if v not in ("", 0, None) else "<empty>")
            if k in _PER_RUN else v for k, v in row.items()}


def _record(plan_id):
    """A plan's whole record — every stage with its proofs — reduced to what
    must not depend on who did the work."""
    return [(_shape(s), [_shape(p) for p in plans.proofs(s["id"])])
            for s in plans.stages(plan_id)]


# ── the invariant ────────────────────────────────────────────────────────────

def test_inline_and_subagent_runs_leave_the_same_record():
    inline = _run_plan("worked inline", ["author-1", "author-1"], subagents="off")
    dispatched = _run_plan("worked by subagents",
                           ["stage-1-agent", "stage-2-agent"], subagents="on")

    assert _record(inline) == _record(dispatched)

    # And the comparison is not vacuous on either side: the two plans really
    # did run under opposite settings, and both really finished on real proofs.
    assert [plans.stage_env(s)["use_subagents"] for s in plans.stages(inline)] \
        == ["off", "off"]
    assert [plans.stage_env(s)["use_subagents"] for s in plans.stages(dispatched)] \
        == ["on", "on"]
    for plan_id in (inline, dispatched):
        rows = plans.stages(plan_id)
        assert [s["status"] for s in rows] == ["done", "done"]
        assert [[(p["kind"], p["ok"]) for p in plans.proofs(s["id"])] for s in rows] \
            == [[("command", 1)], [("command", 1)]]


def test_a_dispatched_stage_records_the_session_that_ran_it():
    """Attribution is how a subagent's work stays traceable: the stage row
    carries the sitting that did it, not the one that authored the plan."""
    plan_id = _run_plan("dispatched", ["stage-1-agent", "stage-2-agent"],
                        subagents="on")
    assert [s["session_id"] for s in plans.stages(plan_id)] \
        == ["stage-1-agent", "stage-2-agent"]
    # The author's plan is still the same plan — a subagent joins it by id.
    assert [p["id"] for p in plans.plans_for_session("stage-2-agent")] == [plan_id]
    assert plans.stage(plans.stages(plan_id)[0]["id"])["session_id"] == "stage-1-agent"


def test_the_variables_a_stage_subagent_is_spawned_with():
    """The spawner sets these and the hooks read them, so the three names have
    one definition; the plan id travels with them because the stage row knows
    it and a spawner would otherwise query for it."""
    plan_id = plans.open_plan("dispatch", session_id="s1")
    plans.set_stages(plan_id, [dict(PLAN_STAGES[0])])
    stage_id = plans.stages(plan_id)[0]["id"]

    assert plans.subagent_env(stage_id) == {
        "JSTACK_PLAN_ID": plan_id,
        "JSTACK_STAGE_ID": stage_id,
        "JSTACK_PLAN_SUBAGENT": "1",
    }
    with pytest.raises(ValueError, match="no such stage"):
        plans.subagent_env("no-such-stage")


# ── the snapshot ─────────────────────────────────────────────────────────────

def test_the_stage_carries_the_environment_it_was_dispatched_under():
    environment.set_value("use_subagents", "on", session_id="s1")
    environment.set_value("delivery_method", "testflight", session_id="s1")
    plan_id = plans.open_plan("snapshotted", session_id="s1")
    plans.set_stages(plan_id, [dict(PLAN_STAGES[0])])
    stage_id = plans.stages(plan_id)[0]["id"]
    plans.stage_start(stage_id, session_id="s1")

    snapshot = plans.stage_env(stage_id)
    assert snapshot["use_subagents"] == "on"
    assert snapshot["delivery_method"] == "testflight"
    # Every setting, not only the moved ones: a key missing from the snapshot
    # would read later as a setting that did not exist rather than one at its
    # default, and the two are different answers to "how was this run".
    assert set(snapshot) == {s.key for s in environment.SETTINGS}

    # Changing the setting afterwards does not rewrite what was recorded.
    environment.set_value("use_subagents", "off", session_id="s1")
    assert plans.stage_env(stage_id)["use_subagents"] == "on"


def test_the_two_writers_of_the_env_column_agree_on_its_shape():
    """`cli.py` resolves the flat `{key: value}` dict itself and passes it;
    `env=None` resolves the same thing here. Two writers, one column: if their
    serialisations drifted the reader would silently get one of them wrong."""
    environment.set_value("use_subagents", "on", session_id="s1")
    plan_id = plans.open_plan("two writers", session_id="s1")
    plans.set_stages(plan_id, [dict(PLAN_STAGES[0]), dict(PLAN_STAGES[1])])
    explicit, resolved = plans.stages(plan_id)

    caller_side = {k: v for k, (v, _) in environment.resolve("s1").items()}
    plans.stage_start(explicit["id"], session_id="s1", env=caller_side)
    plans.stage_start(resolved["id"], session_id="s1")

    assert plans.stage_env(explicit["id"]) == plans.stage_env(resolved["id"])
    assert plans.stage_env(explicit["id"]) == caller_side
    # The reader takes the row a caller already holds as readily as an id.
    assert plans.stage_env(plans.stage(resolved["id"])) == caller_side


def test_an_empty_snapshot_is_recordable_and_a_broken_one_is_not_fatal():
    """`{}` is a caller saying nothing was in force, distinct from `None`
    asking for a resolve. And a column no reader can decode degrades to an
    empty snapshot: it is evidence about a stage, not part of its state
    machine, so one bad row must not take the whole board's read down."""
    plan_id = plans.open_plan("edge", session_id="s1")
    plans.set_stages(plan_id, [dict(PLAN_STAGES[0])])
    stage_id = plans.stages(plan_id)[0]["id"]
    plans.stage_start(stage_id, session_id="s1", env={})
    assert plans.stage_env(stage_id) == {}

    with store.get_store().conn() as db:
        db.execute("UPDATE stages SET env = ? WHERE id = ?", ("not json", stage_id))
    assert plans.stage_env(stage_id) == {}
    assert plans.stage_env("no-such-stage") == {}


def test_a_setting_flipped_mid_plan_is_recorded_per_stage():
    """The snapshot lives on the stage and not on the plan because this is a
    real sequence: the user works the first stage inline, then turns subagents
    on for the rest. Each stage keeps the value that was in force when it was
    dispatched — one plan, two honest answers to "how was this run"."""
    plan_id = plans.open_plan("flipped", session_id="s1")
    plans.set_stages(plan_id, [dict(PLAN_STAGES[0]), dict(PLAN_STAGES[1])])
    first, second = plans.stages(plan_id)

    environment.set_value("use_subagents", "off", session_id="s1")
    plans.stage_start(first["id"], session_id="s1")
    environment.set_value("use_subagents", "on", session_id="s1")
    plans.stage_start(second["id"], session_id="s1")

    assert plans.stage_env(first["id"])["use_subagents"] == "off"
    assert plans.stage_env(second["id"])["use_subagents"] == "on"
    assert "config" not in (plans.plan(plan_id) or {}), \
        "the plan holds no configuration of its own — the stage rows do"


# ── the refusal, on both paths ───────────────────────────────────────────────

@pytest.mark.parametrize("runner", ["the session itself", "a subagent"])
def test_a_command_stage_cannot_close_without_a_proof_on_either_path(runner):
    """The gate cannot have a hole that depends on who did the work. A
    subagent closing its own stage is the path with no second reader on it,
    so it is the one that most needs the writer to refuse."""
    session = "author-1" if runner == "the session itself" else "stage-1-agent"
    environment.set_value("use_subagents",
                          "off" if session == "author-1" else "on",
                          session_id=session)
    plan_id = plans.open_plan("gated", session_id="author-1")
    plans.set_stages(plan_id, [dict(PLAN_STAGES[0])])
    stage_id = plans.stages(plan_id)[0]["id"]
    plans.stage_start(stage_id, session_id=session)

    with pytest.raises(plans.VerificationMissing):
        plans.stage_done(stage_id)
    row = plans.stage(stage_id)
    assert row["status"] == "running" and row["finished_at"] == 0
    assert row["session_id"] == session, "a refusal writes nothing at all"

    plans.run_verify(stage_id, timeout=30)
    plans.stage_done(stage_id)
    assert plans.stage(stage_id)["status"] == "done"


def test_the_refusal_names_a_command_its_reader_can_run():
    """Its reader is mid-turn in a shell, not in a Python session. A remedy
    they cannot paste sends them into the source, which is the one thing the
    message exists to save them from."""
    plan_id = plans.open_plan("gated", session_id="s1")
    plans.set_stages(plan_id, [{"ordinal": 1, "title": "the gated one",
                                "verify_kind": "command",
                                "verify_spec": "/usr/bin/true"},
                               {"ordinal": 2, "title": "the committed one",
                                "verify_kind": "commit", "verify_spec": "HEAD"}])
    command, commit = plans.stages(plan_id)

    with pytest.raises(plans.VerificationMissing) as caught:
        plans.stage_done(command["id"])
    assert (f"jstack-host plan verify {command['id']} — or jstack-host plan "
            f"proof {command['id']} --kind command --ok.") in str(caught.value)

    with pytest.raises(plans.VerificationMissing) as caught:
        plans.stage_done(commit["id"])
    assert (f"jstack-host plan proof {commit['id']} --kind commit --ok "
            f"--detail <sha>.") in str(caught.value)


# ── what the writers refuse to accept ────────────────────────────────────────

def test_an_unreadable_verify_kind_is_refused_and_not_defaulted_to_none():
    """The whole system rebuilt in one typo. The parser leaves `''` on a
    `Verify:` line it cannot read; defaulting that to `none` — the one kind
    that closes on nobody's evidence — gave an author who typed a gate and
    misspelled it a stage that reads as gated, is not, and reports done on no
    receipts. Both halves are asserted: the raise, and that nothing was
    written, because a version that wrote the rows and then complained would
    pass on the raise alone.
    """
    parsed = plan_parse.parse("## Stage 1 — the thing\n\n"
                              "Verify: eyeball ./verify/x.sh\n")
    assert parsed.stages[0].verify_kind == "" and parsed.problems

    plan_id = plans.open_plan("typo", session_id="s1")
    with pytest.raises(ValueError) as caught:
        plans.set_stages(plan_id, parsed.stages)
    assert "eyeball" not in str(caught.value)  # the kind is '', not the word
    assert "not a proof kind" in str(caught.value)
    assert plans.stages(plan_id) == []


def test_one_bad_stage_refuses_the_whole_call_and_keeps_the_good_rows():
    """All-or-nothing: a partial write would leave the plan holding the
    stages before the bad one and silently missing the rest."""
    plan_id = plans.open_plan("partial", session_id="s1")
    plans.set_stages(plan_id, [dict(PLAN_STAGES[0])])
    before = plans.stages(plan_id)

    with pytest.raises(ValueError):
        plans.set_stages(plan_id, [
            {"ordinal": 1, "title": "build, renamed", "verify_kind": "command",
             "verify_spec": "echo green"},
            {"ordinal": 2, "title": "ship", "verify_kind": "eyeball",
             "verify_spec": ""}])
    assert plans.stages(plan_id) == before


def test_work_answers_from_one_snapshot_of_the_store():
    """`work` promises the Work screen a consistent read, so the promise is
    tested with a writer landing in the middle of it. Without the explicit
    begin this passes only by luck: sqlite3 opens a transaction before a write
    and not before a SELECT, so the three reads would each see whatever was
    committed at the moment they ran, and the screen would render tasks that
    belong to a stage list it never saw."""
    plan_id = plans.open_plan("live", session_id="s1")
    plans.activate(plan_id)
    plans.set_stages(plan_id, [dict(PLAN_STAGES[0])])
    stage_id = plans.stages(plan_id)[0]["id"]
    plans.set_tasks(stage_id, [{"subject": "a", "status": "pending",
                                "native_id": "t1"}])

    read_stages = plans._stage_rows

    def _another_writer_lands_mid_read(db, pid):
        rows = read_stages(db, pid)
        plans.set_tasks(stage_id, [
            {"subject": "a", "status": "done", "native_id": "t1"},
            {"subject": "b", "status": "pending", "native_id": "t2"}])
        return rows

    plans._stage_rows = _another_writer_lands_mid_read
    try:
        got = plans.work("s1")
    finally:
        plans._stage_rows = read_stages

    assert [(t["subject"], t["status"]) for t in got["tasks"][stage_id]] \
        == [("a", "pending")]
    # The write did land — the next call reads it.
    assert [t["subject"] for t in plans.work("s1")["tasks"][stage_id]] == ["a", "b"]


def test_list_plans_clamps_a_limit_it_is_handed(monkeypatch):
    """Both ends. `LIMIT -1` is SQLite for no limit at all and the route hands
    this the client's query parameter, so the floor is what stops `?limit=-1`
    from returning every plan the host has ever held; the ceiling is what stops
    a large one from scanning them."""
    made = [plans.open_plan(f"plan {i}") for i in range(3)]
    assert len(plans.list_plans(limit=-1)) == 1
    assert len(plans.list_plans(limit=0)) == 1
    assert len(plans.list_plans(limit=2)) == 2
    assert len(plans.list_plans(limit=10 ** 9)) == len(made)

    monkeypatch.setattr(plans, "MAX_LIST", 2)
    assert len(plans.list_plans(limit=10 ** 9)) == 2


def test_evidence_cannot_be_filed_against_a_stage_that_does_not_exist():
    """A mistyped id used to insert cleanly and hand back a row id: a green
    receipt against nothing, which no stage will ever read and which from the
    call site looks exactly like diligence."""
    with pytest.raises(ValueError, match="no such stage"):
        plans.add_proof("no-such-stage", "command", True, detail="fabricated")
    assert plans.proofs("no-such-stage") == []


def test_the_stage_writers_refuse_an_id_that_is_not_there():
    """`stage_done` and `run_verify` already refused; these two reported
    success while their UPDATE touched no rows — `plan block <typo>` saying a
    stage is parked when it is not is a lie the next reader acts on."""
    with pytest.raises(ValueError, match="no such stage"):
        plans.stage_start("no-such-stage", session_id="s1")
    with pytest.raises(ValueError, match="no such stage"):
        plans.stage_block("no-such-stage", "waiting on review")
    assert plans.stage("no-such-stage") is None
