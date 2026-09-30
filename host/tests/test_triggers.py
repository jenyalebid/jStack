"""The environment's one mechanism: a trigger's condition is met → an action lands.

Every test drives `dispatch` — the function the hook runs — with a hook payload, and reads
back what the hook would print and what the fire log holds. The compact-on-delivery cases
use the real Claude transcript and Codex rollout record shapes the compact suites pin;
an invented shape is the one thing these tests cannot use.
"""
import json
import os
import stat
import sys

import pytest

from jstack_host import compact_delivery as cod
from jstack_host import triggers as tr

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
import test_compact_delivery_codex as cx  # noqa: E402  (the real codex record shapes)


@pytest.fixture(autouse=True)
def quarantined(tmp_path, monkeypatch):
    """Fire log, triggers dir, dedup and the compact log all somewhere disposable: a fire
    log carrying test rows would put them in the jRemote sidebar."""
    monkeypatch.setenv("JREMOTE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("JSTACK_TRIGGER_LOG", str(tmp_path / "triggers.jsonl"))
    monkeypatch.setenv("JSTACK_TRIGGERS_DIR", str(tmp_path / "triggers"))
    monkeypatch.setenv("JSTACK_COMPACT_LOG", str(tmp_path / "decisions.jsonl"))
    monkeypatch.setenv("JSTACK_COMPACT_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.delenv("SKIP_SESSION_HOOK", raising=False)
    (tmp_path / "triggers").mkdir()


@pytest.fixture
def tdir(tmp_path):
    return tmp_path / "triggers"


def script(tdir, name, body):
    path = tdir / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def trigger(tdir, tid, **fields):
    t = {"id": tid, "events": ["UserPromptSubmit"], "action": "inject",
         "condition": {"script": f"{tid}.sh"}, **fields}
    (tdir / f"{tid}.json").write_text(json.dumps(t))
    return t


def claude_transcript(tmp_path, text, tokens=40000, name="claude.jsonl"):
    """Claude's shape: `assistant` rows carrying `message.usage` and text content."""
    path = tmp_path / name
    with open(path, "w") as fh:
        fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": "go"}}) + "\n")
        fh.write(json.dumps({"type": "assistant", "message": {
            "id": "m1", "role": "assistant", "usage": {"input_tokens": tokens},
            "content": [{"type": "text", "text": text}]}}) + "\n")
    return str(path)


def log(sid=None):
    return tr.fires(sid)


def payload(sid="sess-1", **kw):
    return {"session_id": sid, **kw}


# --- the registry ------------------------------------------------------------------------

def test_the_builtin_compact_trigger_is_registered():
    found, problems = tr.registry()
    assert problems == []
    t = next(t for t in found if t["id"] == "compact-on-delivery")
    assert t["events"] == ["Stop"] and set(t["engines"]) == {"claude", "codex"}
    assert t["action"] == "input" and t["executor"] == "compact-delivery"


@pytest.mark.parametrize("bad, why", [
    ({"events": ["Stop"], "action": "inject", "condition": {"script": "x"}}, "needs an id"),
    ({"id": "a", "events": ["Nope"], "action": "inject", "condition": {"script": "x"}}, "events"),
    ({"id": "a", "events": ["Stop"], "action": "shout", "condition": {"script": "x"}}, "action"),
    ({"id": "a", "events": ["Stop"], "action": "inject", "condition": {}}, "condition"),
    ({"id": "a", "events": ["Stop"], "action": "inject",
      "condition": {"script": "x", "builtin": "compact-on-delivery"}}, "condition"),
    ({"id": "a", "events": ["Stop"], "action": "block", "condition": {"script": "x"}}, "PreToolUse"),
    ({"id": "a", "events": ["Stop"], "action": "input", "condition": {"script": "x"}}, "executor"),
    ({"id": "a", "events": ["Stop"], "action": "inject", "engines": ["gemini"],
      "condition": {"script": "x"}}, "engines"),
    ({"id": "a", "events": ["Stop"], "action": "inject", "dedup": "sometimes",
      "condition": {"script": "x"}}, "dedup"),
])
def test_a_trigger_that_does_not_validate_is_refused(bad, why):
    with pytest.raises(tr.Invalid, match=why):
        tr.validate(bad)


def test_a_broken_trigger_file_is_reported_and_the_rest_still_load(tdir):
    (tdir / "broken.json").write_text("{not json")
    (tdir / "wrong.json").write_text(json.dumps({"id": "w", "events": ["Stop"]}))
    trigger(tdir, "good")
    found, problems = tr.registry()
    ids = [t["id"] for t in found]
    assert "good" in ids and "w" not in ids
    assert len(problems) == 2 and any("broken.json" in p for p in problems)


def test_an_id_already_taken_is_refused(tdir):
    trigger(tdir, "compact-on-delivery")
    found, problems = tr.registry()
    assert [t["id"] for t in found].count("compact-on-delivery") == 1
    assert any("already registered" in p for p in problems)


def test_a_disabled_trigger_is_not_loaded(tdir):
    trigger(tdir, "off", enabled=False)
    assert "off" not in [t["id"] for t in tr.registry()[0]]


def test_adding_a_trigger_arms_its_event_without_touching_hooks_json(tdir):
    """The stub reads `.armed` to skip the host on events nothing listens to."""
    assert tr.arm() == {"Stop"}
    trigger(tdir, "late", events=["PreToolUse"], action="block")
    assert tr.arm() == {"Stop", "PreToolUse"}
    assert (tdir / ".armed").read_text() == "PreToolUse\nStop\n"


# --- script conditions ---------------------------------------------------------------------

def test_a_script_that_exits_zero_is_met_and_its_text_is_injected(tdir):
    script(tdir, "hi.sh", 'cat >/dev/null; echo \'{"text": "heads up"}\'')
    trigger(tdir, "hi")
    out = tr.dispatch("UserPromptSubmit", payload(prompt="x"))
    assert out == {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                          "additionalContext": "heads up"}}
    row = log()[-1]
    assert row["trigger"] == "hi" and row["fired"] and row["outcome"] == "delivered"
    assert len(row["fire"]) == 12


def test_the_script_reads_the_normalized_event_on_stdin(tdir, tmp_path):
    seen = tmp_path / "seen.json"
    script(tdir, "echo.sh", f"cat > {seen}; exit 1")
    trigger(tdir, "echo")
    tr.dispatch("UserPromptSubmit", payload(prompt="build it", cwd="/w"))
    ev = json.loads(seen.read_text())
    assert ev["event"] == "UserPromptSubmit" and ev["prompt"] == "build it"
    assert ev["cwd"] == "/w" and ev["sid"] == "sess-1" and ev["engine"] == "claude"
    assert ev["payload"]["prompt"] == "build it"


def test_plain_stdout_is_the_text(tdir):
    script(tdir, "plain.sh", "cat >/dev/null; echo just words")
    trigger(tdir, "plain")
    out = tr.dispatch("UserPromptSubmit", payload())
    assert out["hookSpecificOutput"]["additionalContext"] == "just words"


def test_the_trigger_text_is_used_when_the_script_says_nothing(tdir):
    script(tdir, "quiet.sh", "cat >/dev/null; exit 0")
    trigger(tdir, "quiet", text="from the registry")
    out = tr.dispatch("UserPromptSubmit", payload())
    assert out["hookSpecificOutput"]["additionalContext"] == "from the registry"


def test_exit_one_is_not_met_and_leaves_no_row(tdir):
    script(tdir, "no.sh", "cat >/dev/null; exit 1")
    trigger(tdir, "no")
    assert tr.dispatch("UserPromptSubmit", payload()) is None
    assert log() == []


def test_a_crashing_script_is_not_met_and_its_error_is_logged(tdir):
    script(tdir, "boom.sh", "cat >/dev/null; echo kaput >&2; exit 3")
    trigger(tdir, "boom")
    assert tr.dispatch("UserPromptSubmit", payload()) is None
    row = log()[-1]
    assert row["met"] is False and row["error"] == "exit 3: kaput"


def test_a_script_that_overruns_is_not_met_and_the_turn_goes_on(tdir):
    script(tdir, "slow.sh", "sleep 5")
    trigger(tdir, "slow", condition={"script": "slow.sh", "timeout": 0.3})
    assert tr.dispatch("UserPromptSubmit", payload()) is None
    assert "timed out" in log()[-1]["error"]


def test_a_missing_script_is_not_met_and_says_so(tdir):
    trigger(tdir, "ghost")
    assert tr.dispatch("UserPromptSubmit", payload()) is None
    assert "cannot run" in log()[-1]["error"]


def test_a_trigger_listens_only_to_its_events_engines_and_tools(tdir, tmp_path):
    script(tdir, "yes.sh", "cat >/dev/null; echo hit")
    trigger(tdir, "yes", events=["PreToolUse"], tools=["Bash"])
    assert tr.dispatch("UserPromptSubmit", payload()) is None
    assert tr.dispatch("PreToolUse", payload(tool_name="Read")) is None
    assert tr.dispatch("PreToolUse", payload(tool_name="Bash")) is not None
    trigger(tdir, "yes", events=["PreToolUse"], engines=["codex"])
    path = cx.rollout(tmp_path, cx.meta(), cx.assistant("x"))
    assert tr.dispatch("PreToolUse", payload(tool_name="Bash")) is None
    assert tr.dispatch("PreToolUse", payload(tool_name="Bash", transcript_path=path)) is not None
    assert log()[-1]["engine"] == "codex"


# --- actions render per event ---------------------------------------------------------------

def test_block_denies_the_tool_with_the_text_as_reason(tdir):
    script(tdir, "sim.sh", 'cat >/dev/null; echo \'{"text": "sim-verify is off"}\'')
    trigger(tdir, "sim", events=["PreToolUse"], action="block")
    out = tr.dispatch("PreToolUse", payload(tool_name="Bash"))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == "sim-verify is off"


def test_inject_on_stop_is_the_block_reason():
    assert tr.render("Stop", ["keep going"], []) == {"decision": "block", "reason": "keep going"}


def test_inject_where_no_channel_reaches_the_model_says_nothing():
    assert tr.render("SessionEnd", ["x"], []) is None
    assert tr.render("PreCompact", ["x"], []) is None


def test_two_fired_injects_arrive_together(tdir):
    for n in ("a", "b"):
        script(tdir, f"{n}.sh", f"cat >/dev/null; echo {n}")
        trigger(tdir, n)
    out = tr.dispatch("UserPromptSubmit", payload())
    assert out["hookSpecificOutput"]["additionalContext"] == "a\n\nb"


# --- dedup -----------------------------------------------------------------------------------

def test_session_dedup_fires_once_per_session(tdir):
    script(tdir, "once.sh", "cat >/dev/null; echo once")
    trigger(tdir, "once", dedup="session")
    assert tr.dispatch("UserPromptSubmit", payload()) is not None
    assert tr.dispatch("UserPromptSubmit", payload()) is None
    assert tr.dispatch("UserPromptSubmit", payload(sid="sess-2")) is not None
    held = [r for r in log("sess-1") if r["met"] and not r["fired"]]
    assert held and "already fired this session" in held[0]["reason"]


def test_key_dedup_fires_once_per_key(tdir, tmp_path):
    keyfile = tmp_path / "key"
    script(tdir, "k.sh", f'cat >/dev/null; echo "{{\\"text\\": \\"t\\", \\"key\\": \\"$(cat {keyfile})\\"}}"')
    trigger(tdir, "k", dedup="key")
    keyfile.write_text("one")
    assert tr.dispatch("UserPromptSubmit", payload()) is not None
    assert tr.dispatch("UserPromptSubmit", payload()) is None
    keyfile.write_text("two")
    assert tr.dispatch("UserPromptSubmit", payload()) is not None


# --- the fire log ------------------------------------------------------------------------------

def test_a_settle_row_folds_its_outcome_onto_the_fire():
    tr.record(sid="abcdef12-3456", trigger="t", met=True, fired=True, fire="f1", outcome="pending")
    tr.settle("f1", "compact")
    tr.settle("f1", "sent/continued")
    [row] = log("abcdef12-3456")
    assert row["outcome"] == "sent/continued"
    assert [s["outcome"] for s in row["settled"]] == ["compact", "sent/continued"]


def test_the_log_matches_a_full_sid_or_its_short_form():
    tr.record(sid="abcdef12", trigger="t", met=True, fired=True, fire="f1")
    assert len(log("abcdef12-0000-0000-0000-000000000000")) == 1
    assert log("99999999-0000") == []


def test_an_unknown_event_is_logged_and_the_hook_still_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("{}"))
    assert tr.main(["Whenever"]) == 0
    assert capsys.readouterr().out == ""
    assert tr.fires()[-1]["error"] == "unknown event"


def test_a_dispatcher_that_breaks_never_breaks_the_turn(monkeypatch, capsys):
    monkeypatch.setattr(tr, "registry", lambda: 1 / 0)
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO('{"session_id": "s"}'))
    assert tr.main(["Stop"]) == 0
    assert "ZeroDivisionError" in tr.fires()[-1]["error"]


def test_a_registry_problem_is_logged_on_the_session_that_hit_it(tdir):
    (tdir / "broken.json").write_text("{")
    tr.dispatch("UserPromptSubmit", payload())
    assert "registry: broken.json" in log("sess-1")[-1]["error"]


# --- compact-on-delivery, moved onto the system -------------------------------------------------

@pytest.fixture
def spawned(monkeypatch):
    seen = []
    monkeypatch.setattr(cod, "spawn_child", lambda handoff: seen.append(handoff))
    monkeypatch.setattr(cod, "session_row", lambda sid, path: (sid, None))
    return seen


def test_a_claude_stop_fires_compact_and_hands_the_child_the_fire(tmp_path, spawned):
    path = claude_transcript(tmp_path, "done. " + cod.CONTINUE_MARK)
    out = tr.dispatch("Stop", payload(sid="c1", transcript_path=path))
    assert out is None, "input speaks through keys, never through the hook's stdout"
    [handoff] = spawned
    [row] = log("c1")
    assert row["trigger"] == "compact-on-delivery" and row["engine"] == "claude"
    assert row["fired"] and row["outcome"] == "pending"
    assert handoff["fire"] == row["fire"] and handoff["engine"] == "claude"
    assert handoff["path"] == path and handoff["sid"] == "c1"


def test_a_codex_stop_fires_compact_on_the_real_rollout_shape(tmp_path, spawned):
    path = cx.rollout(tmp_path, cx.meta(), cx.task_started(), cx.user("go"),
                      cx.assistant("seam. " + cod.CONTINUE_MARK), cx.token_count(90000),
                      cx.task_complete())
    tr.dispatch("Stop", payload(sid="x1", transcript_path=path))
    [handoff] = spawned
    [row] = log("x1")
    assert row["engine"] == "codex" and handoff["engine"] == "codex"
    assert handoff["fire"] == row["fire"]


def test_a_codex_fire_is_filed_under_the_managed_session_the_app_shows(tmp_path, spawned,
                                                                        monkeypatch):
    # Codex mints its own thread id; the app knows the session by the id it spawned it
    # under. A fire keyed by the thread id never reaches that session's sidebar.
    path = cx.rollout(tmp_path, cx.meta(), cx.assistant("seam. " + cod.CONTINUE_MARK),
                      cx.token_count(90000), cx.task_complete())
    monkeypatch.setattr(cod, "session_row", lambda sid, p: (
        ("managed-1", {"transcript": p}) if p == path else (sid, None)))
    tr.dispatch("Stop", payload(sid="thread-9", transcript_path=path))
    assert log("thread-9") == []
    [row] = log("managed-1")
    assert row["engine"] == "codex" and row["fired"]


def test_a_stop_right_after_a_boundary_is_not_met_and_says_why(tmp_path, spawned):
    path = cx.rollout(tmp_path, cx.meta(), cx.assistant("x"), cx.token_count(90000),
                      cx.compacted())
    tr.dispatch("Stop", payload(sid="x2", transcript_path=path))
    assert spawned == []
    [row] = log("x2")
    assert row["met"] is False and "boundary" in row["reason"]


def test_a_stop_without_a_transcript_does_not_spawn(spawned):
    tr.dispatch("Stop", payload(sid="none"))
    assert spawned == []


def test_the_childs_decision_and_outcome_settle_onto_the_fire(monkeypatch):
    """What the detached child records in its own log is carried back to the fire, so the
    fire log answers "what happened" and not only "something started"."""
    tr.record(sid="c9", trigger="compact-on-delivery", met=True, fired=True, fire="f9",
              outcome="pending")
    monkeypatch.setattr(cod, "_FIRE", "f9")
    cod.record(sid="c9", weight=170000, decision="compact", resume=True)
    cod.record(sid="c9", outcome="sent/continued")
    [row] = log("c9")
    assert row["outcome"] == "sent/continued"
    assert row["settled"][0]["outcome"] == "compact" and row["settled"][0]["weight"] == 170000


def test_a_skip_decision_settles_with_its_reason(monkeypatch):
    tr.record(sid="c8", trigger="compact-on-delivery", met=True, fired=True, fire="f8")
    monkeypatch.setattr(cod, "_FIRE", "f8")
    cod.record(sid="c8", decision="skip", reason="the turn ended without asking")
    assert log("c8")[0]["outcome"] == "skip: the turn ended without asking"


def test_the_child_takes_its_fire_from_the_handoff(monkeypatch):
    monkeypatch.setattr(cod, "held", lambda sid: None)
    monkeypatch.setattr(cod, "_FIRE", None)
    cod.child([json.dumps({"sid": "s", "fire": "abc"})])
    assert cod._FIRE == "abc"


def test_the_old_hook_no_longer_delivers():
    """Kept in hooks.json for Codex's trust ordinals; the trigger delivers now."""
    hook = os.path.join(HERE, "..", "..", "plugins", "jstack", "hooks",
                        "stop-compact-delivery.sh")
    import subprocess
    r = subprocess.run(["sh", hook], input='{"session_id":"s"}', capture_output=True,
                       text=True, timeout=10)
    assert r.returncode == 0 and r.stdout == ""


# --- one event, two registrations ---------------------------------------------------------

def test_the_same_hook_event_twice_acts_once(tdir, tmp_path, monkeypatch):
    """The legacy Stop hook forwards to the dispatcher so a session whose hook list predates
    the dispatcher still reaches it; a session on the new list runs both copies. The second
    copy of one event (same session, same transcript length) must not fire again."""
    monkeypatch.setattr(tr.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    script(tdir, "hi.sh", 'cat >/dev/null; echo \'{"text": "heads up"}\'')
    trigger(tdir, "hi")
    t = tmp_path / "t.jsonl"
    t.write_text("{}\n")
    p = payload(prompt="x", transcript_path=str(t))
    assert tr.dispatch("UserPromptSubmit", p) is not None
    assert tr.dispatch("UserPromptSubmit", p) is None
    assert len([r for r in log() if r.get("fired")]) == 1
    # The next event on the same session is a new event: the transcript has grown.
    t.write_text("{}\n{}\n")
    assert tr.dispatch("UserPromptSubmit", p) is not None
