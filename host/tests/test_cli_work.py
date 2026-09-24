"""The `env` and `plan` verbs, driven the way an agent drives them: argv in,
exit status and streams out.

Three things here are worth more than the rest. A `plan done` that is refused
must exit non-zero AND leave the row open AND put the refusal on stderr — a
command that printed the message and exited 0 would be read by every caller as
a closed stage, which is the failure the whole harness exists to prevent. A
mistyped setting must exit non-zero and store nothing, because a typed registry
whose typos store cleanly is a picker that answers 200 and changes nothing. And
a plan whose parse complained writes NO stages: `set_stages` coerces an
unreadable `Verify:` line to `none`, so a forgiving write produces a stage that
looks gated and closes on anybody's word.

The store seam is `store._store`, which `get_store` consults first and the
package's own conftest isolation defers to — so both modules under test resolve
their database the same way here as they do on the machine.
"""

import contextlib
import io
import json

import pytest

from jstack_host import cli, environment, plans, store
from jstack_host.store import SessionStore

SESSION = "sess-work-1"
AGENT = "ops-chat"

PLAN_MD = """# Work harness

## Stage 1 — the parser
Read the markdown into rows.

Verify: command · true

## Stage 2 — the cli
Wire the verbs up.

Verify: manual · the user runs it once
"""

# Stage 1 declares no proof at all: the parser complains, and what would be
# stored for it is `none` — the kind that closes on assertion.
BROKEN_MD = """# Work harness

## Stage 1 — the parser
Nothing says how this one is proved.

## Stage 2 — the cli
Verify: none
"""

#: Complained about but writable: every kind reads, only the numbering is wrong.
FORCEABLE_MD = """# Work harness

## Stage 1 — the parser
Verify: command · true

## Stage 3 — the cli
Verify: none
"""


@pytest.fixture(autouse=True)
def work_store(tmp_path, monkeypatch):
    """A database of this test's own, through the package's injection point."""
    monkeypatch.setattr(store, "_store",
                        SessionStore(db_path=tmp_path / "work.sqlite"))


@pytest.fixture(autouse=True)
def no_ambient_session(monkeypatch):
    """The shell running the suite may itself be inside a session.

    Left set, every command with no `--session` would write into whatever id
    this machine's harness exported, and the layer assertions below would pass
    or fail depending on who ran them.
    """
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


@pytest.fixture
def session_row():
    """The session→agent link the layer walk follows when no agent is named."""
    with store.get_store().conn() as db:
        db.execute("INSERT INTO sessions (session_id, agent_id) VALUES (?,?)",
                   (SESSION, AGENT))


def _cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def _shown(*argv) -> dict[str, dict]:
    code, out, err = _cli("env", "show", "--json", *argv)
    assert code == 0, err
    return {row["key"]: row for row in json.loads(out)}


def _stored_rows(table: str) -> int:
    with store.get_store().conn() as db:
        return db.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]


# ── env ──────────────────────────────────────────────────────────────────────

def test_a_session_value_reads_back_as_that_session_s():
    assert _cli("env", "set", "delivery_method", "build",
                "--session", SESSION)[0] == 0

    row = _shown("--session", SESSION)["delivery_method"]
    assert (row["value"], row["source"]) == ("build", "session")
    assert _shown("--session", SESSION)["sim_verify"]["source"] == "default"
    # The layer is a layer: the next session over is untouched.
    assert _shown("--session", "someone-else")["delivery_method"] == {
        "key": "delivery_method", "value": "none", "source": "default"}


def test_an_agent_value_is_inherited_and_reported_as_inherited(session_row):
    assert _cli("env", "set", "delivery_method", "testflight",
                "--agent", AGENT)[0] == 0

    row = _shown("--session", SESSION)["delivery_method"]
    assert (row["value"], row["source"]) == ("testflight", "agent")
    assert _cli("env", "get", "delivery_method", "--session", SESSION)[1] \
        == "testflight\n"


def test_unset_hands_the_say_back_to_the_layer_below(session_row):
    _cli("env", "set", "sim_verify", "off", "--agent", AGENT)
    _cli("env", "set", "sim_verify", "on", "--session", SESSION)
    assert _shown("--session", SESSION)["sim_verify"]["source"] == "session"

    code, out, err = _cli("env", "unset", "sim_verify", "--session", SESSION)
    assert code == 0, err
    # What it prints is what is now in force, not what was cleared.
    assert "off" in out and "agent" in out
    row = _shown("--session", SESSION)["sim_verify"]
    assert (row["value"], row["source"]) == ("off", "agent")


def test_an_unknown_key_exits_nonzero_and_stores_nothing():
    code, _, err = _cli("env", "set", "delivery_methdo", "build",
                        "--session", SESSION)
    assert code != 0
    assert "delivery_methdo" in err
    assert _stored_rows("session_env") == 0


def test_a_value_outside_the_enum_exits_nonzero_and_stores_nothing():
    code, _, err = _cli("env", "set", "delivery_method", "sideways",
                        "--session", SESSION)
    assert code != 0
    assert "sideways" in err
    assert _stored_rows("session_env") == 0
    assert _shown("--session", SESSION)["delivery_method"]["value"] == "none"


def test_no_flag_means_the_session_this_shell_is_in(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "ambient-1")
    assert _cli("env", "set", "use_subagents", "on")[0] == 0
    assert environment.get("use_subagents", session_id="ambient-1") == "on"


def test_with_no_session_to_infer_the_layer_has_to_be_named():
    """Never a row owned by `''` — that one is inherited by every session
    whose id is also empty, and is findable from no screen."""
    code, _, err = _cli("env", "set", "use_subagents", "on")
    assert code != 0
    assert "--session" in err and "--agent" in err
    assert _stored_rows("session_env") == 0


def test_naming_both_layers_is_refused_rather_than_guessed():
    code, _, err = _cli("env", "set", "use_subagents", "on",
                        "--session", SESSION, "--agent", AGENT)
    assert code != 0
    assert _stored_rows("session_env") == 0 and _stored_rows("agent_env") == 0


# ── plan ─────────────────────────────────────────────────────────────────────

def _plan_with_stages(tmp_path, body=PLAN_MD, *, force=False):
    code, out, err = _cli("plan", "open", "Work harness")
    assert code == 0, err
    plan_id = out.strip()
    path = tmp_path / "plan.md"
    path.write_text(body)
    argv = ["plan", "stages", plan_id, "--from-file", str(path)]
    if force:
        argv.append("--force")
    return (plan_id, *_cli(*argv))


def test_stages_come_out_of_the_markdown(tmp_path):
    plan_id, code, out, err = _plan_with_stages(tmp_path)
    assert code == 0, err
    rows = plans.stages(plan_id)
    assert [r["ordinal"] for r in rows] == [1, 2]
    assert rows[0]["verify_kind"] == "command" and rows[0]["verify_spec"] == "true"
    assert rows[0]["id"] in out


def test_a_complained_about_plan_writes_no_stages_until_forced(tmp_path):
    """The parse problems are printed either way; the write is what is withheld."""
    plan_id, code, _, err = _plan_with_stages(tmp_path, FORCEABLE_MD)
    assert code != 0
    assert "numbered 3" in err
    assert plans.stages(plan_id) == []

    path = tmp_path / "plan.md"
    code, _, forced_err = _cli("plan", "stages", plan_id,
                               "--from-file", str(path), "--force")
    assert code == 0, forced_err
    assert "numbered 3" in forced_err
    assert len(plans.stages(plan_id)) == 2


def test_force_cannot_conjure_a_gate_that_was_never_declared(tmp_path):
    """What `--force` overrides is the parser's complaint, never the writer's rule.

    `BROKEN_MD`'s first stage declares no `Verify:` line at all, so there is no
    proof that could ever close it. Writing it under `--force` would put back
    exactly the hole this harness exists to shut — a stage that reads as gated,
    closes on nothing, and reports done. The writer refuses, and the CLI has to
    hand that refusal over as a sentence rather than a traceback.
    """
    plan_id, code, _, err = _plan_with_stages(tmp_path, BROKEN_MD)
    assert code != 0
    assert plans.stages(plan_id) == []

    path = tmp_path / "plan.md"
    code, out, forced_err = _cli("plan", "stages", plan_id,
                                 "--from-file", str(path), "--force")
    assert code == 1
    assert "not a proof kind" in forced_err
    assert "Traceback" not in forced_err and out == ""
    assert plans.stages(plan_id) == []


def test_a_mistyped_stage_id_is_a_message_on_every_verb(tmp_path):
    """Four writers refuse an unknown stage; none of them may do it by traceback."""
    for argv in (("plan", "start", "ghost"),
                 ("plan", "done", "ghost"),
                 ("plan", "block", "ghost", "--reason", "because"),
                 ("plan", "proof", "ghost", "--kind", "manual", "--ok")):
        code, out, err = _cli(*argv)
        assert code == 1, argv
        assert "no such stage" in err and "ghost" in err, argv
        assert "Traceback" not in err and out == "", argv


def test_done_is_refused_nonzero_with_the_message_and_the_row_left_open(tmp_path):
    plan_id, code, _, err = _plan_with_stages(tmp_path)
    assert code == 0, err
    stage_id = plans.stages(plan_id)[0]["id"]
    assert _cli("plan", "start", stage_id)[0] == 0

    code, out, err = _cli("plan", "done", stage_id)

    assert code != 0
    assert stage_id in err and "no passing proof" in err
    # The remedy travels with the refusal, and as something its reader can run:
    # an agent mid-turn is at a shell, not inside a Python session.
    assert f"jstack-host plan verify {stage_id}" in err
    assert "run_verify" not in err and "add_proof(" not in err
    assert "Traceback" not in err and out == ""
    row = plans.stages(plan_id)[0]
    assert row["status"] == "running" and row["finished_at"] == 0


def test_a_filed_proof_is_what_lets_the_stage_close(tmp_path):
    plan_id, _, _, err = _plan_with_stages(tmp_path)
    stage_id = plans.stages(plan_id)[0]["id"]

    code, out, err = _cli("plan", "proof", stage_id, "--kind", "command",
                          "--ok", "--detail", "true", "--exit-code", "0")
    assert code == 0, err
    code, out, err = _cli("plan", "done", stage_id)
    assert code == 0, err
    assert plans.stages(plan_id)[0]["status"] == "done"


def test_a_failed_proof_is_recorded_and_still_does_not_close_the_stage(tmp_path):
    plan_id, _, _, _ = _plan_with_stages(tmp_path)
    stage_id = plans.stages(plan_id)[0]["id"]

    assert _cli("plan", "proof", stage_id, "--kind", "command", "--failed",
                "--detail", "true", "--exit-code", "1")[0] == 0
    assert plans.proofs(stage_id)[0]["ok"] == 0
    assert _cli("plan", "done", stage_id)[0] != 0
    assert plans.stages(plan_id)[0]["status"] != "done"


def test_verify_runs_the_declared_command_and_files_the_proof(tmp_path):
    plan_id, _, _, _ = _plan_with_stages(tmp_path)
    stage_id = plans.stages(plan_id)[0]["id"]

    code, out, err = _cli("plan", "verify", stage_id)
    assert code == 0, err
    assert "exit 0" in out
    assert [p["ok"] for p in plans.proofs(stage_id)] == [1]
    assert _cli("plan", "done", stage_id)[0] == 0


def test_verify_refuses_a_stage_that_declared_no_command(tmp_path):
    plan_id, _, _, _ = _plan_with_stages(tmp_path)
    manual = plans.stages(plan_id)[1]["id"]
    code, _, err = _cli("plan", "verify", manual)
    assert code != 0 and "manual" in err
    assert plans.proofs(manual) == []


def test_block_records_what_it_is_blocked_on(tmp_path):
    plan_id, _, _, _ = _plan_with_stages(tmp_path)
    stage_id = plans.stages(plan_id)[0]["id"]
    assert _cli("plan", "block", stage_id, "--reason", "waiting on the key")[0] == 0
    row = plans.stages(plan_id)[0]
    assert row["status"] == "blocked"
    assert row["blocked_reason"] == "waiting on the key"


def test_show_json_round_trips(tmp_path):
    plan_id, _, _, _ = _plan_with_stages(tmp_path)
    stage_id = plans.stages(plan_id)[0]["id"]
    _cli("plan", "proof", stage_id, "--kind", "commit", "--ok", "--detail", "abc123")

    code, out, err = _cli("plan", "show", plan_id, "--json")
    assert code == 0, err
    payload = json.loads(out)
    assert payload["plan"]["id"] == plan_id
    assert payload["plan"]["title"] == "Work harness"
    assert [s["ordinal"] for s in payload["stages"]] == [1, 2]
    assert payload["stages"][0]["proofs"][0]["detail"] == "abc123"
    assert payload["stages"][1]["proofs"] == []


def test_list_json_carries_the_plan_a_session_opened(tmp_path):
    plan_id, _, _, _ = _plan_with_stages(tmp_path)
    code, out, err = _cli("plan", "list", "--json")
    assert code == 0, err
    assert [p["id"] for p in json.loads(out)] == [plan_id]


def test_opening_a_plan_joins_the_session_that_typed_it(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "ambient-2")
    code, out, err = _cli("plan", "open", "Ambient")
    assert code == 0, err
    assert plans.open_plan_for_session("ambient-2")["id"] == out.strip()


def test_show_says_so_rather_than_traceback_on_an_id_that_is_not_a_plan():
    code, _, err = _cli("plan", "show", "no-such-plan")
    assert code != 0 and "no-such-plan" in err
