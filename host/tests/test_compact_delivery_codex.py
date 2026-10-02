"""The seam on CODEX, the engine it had never once worked on.

`test_compact_delivery.py` is 123 tests and none names codex; `test_codex_compat.py` adds
three pane tests. So the chain that decides a codex seam had no codex test at any link:
`is_boundary` and `closing_text_after` none at all, and `turn_of`, `asked_to_continue`,
`settle_declaration` and `decide` only through Claude rows -- which answer every one of
them on the claude branch while the codex branch beside it was carried by inspection.

It ran that way for weeks: 39 codex deliveries across two machines, every one recorded
`the turn ended without asking to be continued`, zero codex compactions against 136
Claude ones, and nothing here would have failed.

EVERY FIXTURE IS A REAL RECORD SHAPE, field-for-field from a 240k-token rollout that
climbed to its own auto-compact (a local rollout store).
Inventing the shape is the one thing these tests cannot do: the failure they cover is a
branch reading a shape nobody checked.
"""
import json
import os

import pytest

from jstack_host import compact_delivery as cod

CONT = cod.CONTINUE_MARK


@pytest.fixture(autouse=True)
def quarantined_state(tmp_path, monkeypatch):
    """Same quarantine the Claude suite takes, and for the same reason: `decide()` records,
    and a diagnostic log carrying test rows is a diagnostic that lies."""
    monkeypatch.setenv("JSTACK_COMPACT_LOG", str(tmp_path / "decisions.jsonl"))
    monkeypatch.setenv("JSTACK_COMPACT_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setattr(cod, "RETRY_WINDOWS", (0.05, 0.05))


# --- the real record shapes ---------------------------------------------------------------

def meta(sid="01a0ef3f-7447-79f3-9559-05feaa7a2824"):
    """Line 1 of every rollout. This is what `engine_of` sniffs to answer "codex"."""
    return {"timestamp": "2026-09-29T22:18:28.477Z", "ordinal": 0, "type": "session_meta",
            "payload": {"session_id": sid, "id": sid,
                        "timestamp": "2026-09-29T22:18:28.477Z"}}


def assistant(text, ordinal=13):
    """An agent turn: `response_item` / `message` / `output_text`. Claude's shape is
    `{"type": "assistant", "message": {"content": [{"type": "text"}]}}` -- no field in
    common, which is why a Claude-only suite proves nothing about this branch."""
    return {"timestamp": "2026-09-29T22:18:37.207Z", "ordinal": ordinal,
            "type": "response_item",
            "payload": {"type": "message", "id": f"msg_{ordinal}", "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                        "phase": "commentary"}}


def user(text, ordinal=8):
    """A person's prompt: same envelope, `input_text` instead of `output_text`."""
    return {"timestamp": "2026-09-29T22:18:30.000Z", "ordinal": ordinal,
            "type": "response_item",
            "payload": {"type": "message", "id": f"msg_{ordinal}", "role": "user",
                        "content": [{"type": "input_text", "text": text}]}}


def token_count(tokens, ordinal=14):
    """The reading. Codex reports it whole per model call under `last_token_usage`; the
    total-usage figure beside it is cumulative across the session and is not a window."""
    return {"timestamp": "2026-09-29T22:22:12.354Z", "ordinal": ordinal, "type": "event_msg",
            "payload": {"type": "token_count",
                        "info": {"total_token_usage": {"input_tokens": 5799393,
                                                       "total_tokens": 5805149},
                                 "last_token_usage": {"input_tokens": tokens,
                                                      "cached_input_tokens": tokens - 300,
                                                      "output_tokens": 80},
                                 "model_context_window": 258400}}}


def task_started(ordinal=1):
    return {"timestamp": "2026-09-29T22:18:28.477Z", "ordinal": ordinal, "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "t1", "root_turn_id": "t1",
                        "started_at": 1790720308, "model_context_window": 258400}}


def task_complete(ordinal=568, last=""):
    return {"timestamp": "2026-09-29T22:25:02.201Z", "ordinal": ordinal, "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "t1",
                        "last_agent_message": last}}


def compacted(ordinal=316):
    """What a codex compaction lands: top-level `type: compacted`, an EMPTY `message`, and
    an opaque `replacement_history`. There is no summariser to instruct and no summary to
    read -- which is the whole reason the codex half of the ceiling note talks about what
    survives on disk instead of what to write in the closing message."""
    return {"timestamp": "2026-09-29T22:22:32.481Z", "ordinal": ordinal, "type": "compacted",
            "payload": {"message": "",
                        "replacement_history": [{"type": "message", "role": "developer",
                                                 "content": [{"type": "input_text",
                                                              "text": "<skills…>"}]}]}}


def rollout(tmp_path, *entries, name="rollout.jsonl"):
    path = tmp_path / name
    with open(path, "w") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")
    return str(path)


# --- the engine is read off the file, not guessed from the registry ----------------------

def test_a_codex_rollout_names_itself_codex(tmp_path):
    """`engine_of` reads the file first and the registry second, so a hand-started codex
    session -- one no managed row knows about -- still gets the codex grammar."""
    path = rollout(tmp_path, meta(), assistant("hi"))
    assert cod.engine_of(path) == "codex"
    assert cod.engine_of(path, {"engine": "claude"}) == "codex", (
        "the file is the fact; a stale registry row must not override it")


def test_the_registry_fallback_is_unreachable_and_this_pins_it(tmp_path):
    """`engine_of`'s docstring offers the registry row as the answer for a session the file
    cannot name. It never gets asked: `context_ceiling.engine_of` answers "claude" for an
    unreadable path rather than None, so `found in ENGINES` is already true and the row is
    dead code. A managed CODEX session whose rollout is missing is therefore judged with
    Claude's grammar -- which fails SAFE (Claude's footer never matches a codex pane, so the
    gate refuses and the seam is skipped), but it is not what the docstring says.

    Pinned rather than fixed: changing the default is a change to which grammar every
    unreadable transcript gets, and that belongs on its own issue with its own proof.
    """
    gone = str(tmp_path / "gone.jsonl")
    assert cod.engine_of(gone, {"engine": "codex"}) == "claude"
    assert cod.engine_of(gone, None) == "claude"


# --- is_boundary: no test existed for either engine -------------------------------------

def test_the_codex_boundary_is_a_top_level_compacted_record(tmp_path):
    assert cod.is_boundary(compacted(), "codex") is True


def test_each_engine_refuses_the_others_boundary():
    """The two shapes share nothing, and reading one for the other is a wait that never
    ends: the child sits out its whole budget and reports `sent/no-boundary` over a
    compaction that landed."""
    claude_mark = {"type": "system", "subtype": "compact_boundary"}
    assert cod.is_boundary(claude_mark, "claude") is True
    assert cod.is_boundary(claude_mark, "codex") is False
    assert cod.is_boundary(compacted(), "claude") is False


def test_an_ordinary_codex_record_is_not_a_boundary():
    for entry in (assistant("done"), user("go"), token_count(1000), task_complete()):
        assert cod.is_boundary(entry, "codex") is False


# --- turn_of on the codex shape ----------------------------------------------------------

def test_an_agent_turn_carries_its_output_text(tmp_path):
    role, said = cod.turn_of(assistant("Parked at a seam.\n" + CONT), "codex")
    assert role == "assistant"
    assert said.splitlines()[-1] == CONT, "the closing line is what the marker is read from"


def test_a_prompt_is_a_turn_but_carries_no_text():
    """Only assistant text is returned -- `text` is what the marker is read from, and a
    person's prompt quoting the marker must never declare on the agent's behalf."""
    assert cod.turn_of(user("compact at " + CONT), "codex") == ("user", "")


def test_the_rollout_talking_to_itself_is_not_a_turn():
    """Codex inlines its instruction docs and every tool caveat as user-role text. Counting
    one as a person speaking makes the continue abort on the session's own boundary."""
    from jstack_host import codex_transcript
    noise = codex_transcript._NOISE_PREFIXES[0] + " whatever follows"
    assert cod.turn_of(user(noise), "codex") == (None, "")
    assert cod.turn_of(user("   "), "codex") == (None, "")


def test_a_turn_that_ended_in_tool_calls_is_not_an_agent_turn():
    """An assistant record with no text block answers nothing, which settles as "finished"
    -- the cheap direction to be wrong in."""
    empty = assistant("")
    assert cod.turn_of(empty, "codex") == (None, "")
    reasoning = {"type": "response_item",
                 "payload": {"type": "reasoning", "role": "assistant",
                             "content": [{"type": "output_text", "text": CONT}]}}
    assert cod.turn_of(reasoning, "codex") == (None, ""), (
        "a reasoning item is not the closing message, whatever it contains")


def test_an_event_record_is_never_a_turn():
    for entry in (token_count(1000), task_started(), task_complete(), compacted(), meta()):
        assert cod.turn_of(entry, "codex") == (None, "")


# --- the declaration: the one thing only the session can tell us -------------------------

def test_the_marker_on_the_closing_line_asks_for_the_seam(tmp_path):
    path = rollout(tmp_path, meta(), user("go"),
                   assistant("Pushed what I had.\n\n" + CONT))
    assert cod.asked_to_continue(path, "codex") is True


def test_silence_is_a_finished_delivery(tmp_path):
    path = rollout(tmp_path, meta(), user("go"), assistant("Landed and pushed. Done."))
    assert cod.asked_to_continue(path, "codex") is False, (
        "a finished delivery is left alone -- never ghost-compacted over the only copy "
        "of its conversation")


def test_the_marker_mid_sentence_declares_nothing(tmp_path):
    """The exact way this misfired on Claude: the report explaining the mechanism quoted
    the marker, and the substring version read that as its author asking for a seam."""
    path = rollout(tmp_path, meta(),
                   assistant(f"The hook reads `{CONT}` as the closing line.\n\nDone."))
    assert cod.asked_to_continue(path, "codex") is False


def test_only_the_last_turn_is_asked(tmp_path):
    """A session that parked once, got its resume and then finished is finished."""
    path = rollout(tmp_path, meta(),
                   assistant("Parking.\n" + CONT, ordinal=13),
                   user("carry on", ordinal=20),
                   assistant("Finished and pushed.", ordinal=30))
    assert cod.asked_to_continue(path, "codex") is False


def test_a_backticked_marker_still_declares(tmp_path):
    path = rollout(tmp_path, meta(), assistant("Parked.\n\n`" + CONT + "`"))
    assert cod.asked_to_continue(path, "codex") is True


def test_an_unreadable_rollout_settles_as_finished(tmp_path):
    assert cod.asked_to_continue(str(tmp_path / "nope.jsonl"), "codex") is False


# --- closing_text_after / settle_declaration: no test existed for either engine ----------

def test_the_closing_message_is_read_from_the_stop_offset(tmp_path):
    """The CLI writes the turn's last word AFTER the Stop hooks return, so a text line
    landing past the Stop-time offset is unambiguously the turn's own closing message. A
    full-file read answers with whatever mid-turn note was already on disk."""
    path = rollout(tmp_path, meta(), assistant("mid-turn status note", ordinal=5))
    offset = os.path.getsize(path)
    assert cod.closing_text_after(path, offset, "codex") is None, "nothing has landed yet"

    with open(path, "a") as fh:
        fh.write(json.dumps(assistant("Parked.\n" + CONT, ordinal=9)) + "\n")
    assert cod.closing_text_after(path, offset, "codex").endswith(CONT)


def test_the_newest_text_past_the_offset_wins(tmp_path):
    path = rollout(tmp_path, meta())
    offset = os.path.getsize(path)
    with open(path, "a") as fh:
        fh.write(json.dumps(assistant("first block", ordinal=9)) + "\n")
        fh.write(json.dumps(assistant("Parked.\n" + CONT, ordinal=10)) + "\n")
    assert cod.closing_text_after(path, offset, "codex").endswith(CONT), (
        "a sibling text block landing after the marker must not be read as the closing one")


def test_settle_moves_the_baseline_past_the_line_it_just_read(tmp_path):
    """The baseline has to move past the closing message, or `turn_moved` reads the turn's
    own last word as somebody new speaking and aborts the send it gates."""
    path = rollout(tmp_path, meta())
    offset = os.path.getsize(path)
    with open(path, "a") as fh:
        fh.write(json.dumps(assistant("Parked.\n" + CONT, ordinal=9)) + "\n")
    wants_resume, baseline = cod.settle_declaration(path, offset, "codex")
    assert wants_resume is True
    assert baseline >= os.path.getsize(path)


def test_a_declaration_written_before_stop_is_still_read(tmp_path):
    """Older CLI, different timing: nothing lands past the offset, so the newest text on
    file is the answer rather than a silent "finished"."""
    path = rollout(tmp_path, meta(), assistant("Parked.\n" + CONT))
    wants_resume, _ = cod.settle_declaration(path, os.path.getsize(path), "codex")
    assert wants_resume is True


# --- turn_state: the readiness signal codex's screen cannot give -------------------------

def test_the_rollout_says_working_between_start_and_complete(tmp_path):
    path = rollout(tmp_path, meta(), task_started(), assistant("thinking"))
    assert cod.turn_state(path, "codex") == "working"


def test_the_rollout_says_idle_once_the_task_completes(tmp_path):
    path = rollout(tmp_path, meta(), task_started(),
                   assistant("Parked.\n" + CONT),
                   task_complete(last="Parked.\n" + CONT))
    assert cod.turn_state(path, "codex") == "idle"


def test_claude_is_never_asked_this_question(tmp_path):
    """Claude's readiness is read off the pane; a non-empty answer here would add a second
    source of truth to a gate whose whole job is to have one."""
    path = rollout(tmp_path, meta(), task_started())
    assert cod.turn_state(path, "claude") == ""


# --- the pane gate, on codex's terms ----------------------------------------------------

CODEX_IDLE = "\n".join(["• Explored", "  └ Read notes.md", "",
                        "› Ask Codex to do anything", "",
                        "  GPT-5.6-Sol medium · ~/work/notes · Survey", ""])
CODEX_DRAFT = CODEX_IDLE.replace("Ask Codex to do anything", "and also check the")
CODEX_WORKING = "\n".join(["• Working (4m 18s • esc to interrupt)", "",
                           "› Ask Codex to do anything", ""])


def test_a_quiet_codex_screen_is_not_idle_until_the_rollout_agrees():
    """`screen_idle` is False for codex: its pane draws no footer and no working marker
    this gate can trust, so an idle-LOOKING screen with no rollout verdict must refuse.
    Unknown is not idle -- the cost of being wrong is `/compact` typed onto the end of
    somebody's half-written message."""
    assert cod.pane_is_ready(CODEX_IDLE, "codex", turn="idle") is True
    assert cod.pane_is_ready(CODEX_IDLE, "codex", turn="working") is False
    assert cod.pane_is_ready(CODEX_IDLE, "codex", turn="") is False


def test_the_invitation_is_an_empty_box_and_a_draft_is_not():
    """An empty codex box draws its own dim invitation. Reading that text as somebody's
    message makes every codex seam refuse; reading a real draft as empty types into it."""
    assert cod.pane_is_ready(CODEX_IDLE, "codex", turn="idle") is True
    assert cod.pane_is_ready(CODEX_DRAFT, "codex", turn="idle") is False


def test_a_working_codex_pane_is_refused_even_if_the_rollout_lags():
    assert cod.pane_is_ready(CODEX_WORKING, "codex", turn="working") is False


def test_a_codex_pane_mid_compaction_is_refused():
    busy = CODEX_IDLE.replace("• Explored", "· Compacting conversation…")
    assert cod.pane_is_ready(busy, "codex", turn="idle") is False


def test_an_empty_capture_is_never_ready():
    assert cod.pane_is_ready("", "codex", turn="idle") is False


# --- the reading, off codex's own accounting --------------------------------------------

def test_the_reading_is_the_last_per_call_input_not_the_session_total(tmp_path):
    """`total_token_usage` is cumulative across the session -- 5.8M on the rollout these
    fixtures come from. Reading that as the window puts every codex session past every cut
    from its second turn."""
    path = rollout(tmp_path, meta(), token_count(66369, ordinal=10),
                   token_count(240229, ordinal=14))
    assert cod.reading(path, engine="codex") == 240229


# --- end to end: the decision a codex session has never once received -------------------

def over_the_cut(tmp_path, closing, **kw):
    """A codex session past the heavy cut whose turn has ended, closing with `closing`."""
    return rollout(tmp_path, meta(), task_started(), user("do the thing"),
                   token_count(30000, ordinal=10),
                   token_count(cod.compaction.HEAVY + 18000, ordinal=14),
                   assistant(closing, ordinal=20),
                   task_complete(ordinal=21, last=closing), **kw)


@pytest.fixture
def managed_pane(monkeypatch):
    """`decide` refuses anything it cannot see a tmux pane for, so a codex session with no
    pane never reaches the cut arithmetic at all. Faked here because the point of these
    tests is the arithmetic and the marker, not tmux."""
    monkeypatch.setattr(cod, "pane", lambda name: CODEX_IDLE)
    return "jr-01a0ef3f"


def test_a_codex_session_that_asked_for_the_seam_is_granted_it(tmp_path, managed_pane):
    path = over_the_cut(tmp_path, "Pushed the fix. Parking here.\n\n" + CONT)
    name, threshold = cod.decide(path, "01a0ef3f", "codex", "agent/chat", wants_resume=True)
    assert name == managed_pane
    assert threshold == cod.compaction.HEAVY, (
        "a name with a threshold is compact-then-step-across; a name with None would hand "
        "it back where it stands")


def test_a_finished_codex_delivery_is_left_alone(tmp_path, managed_pane):
    name, reason = cod.decide(path_of := over_the_cut(tmp_path, "Landed, pushed. Done."),
                              "01a0ef3f", "codex", "agent/chat", wants_resume=False)
    assert name is None, path_of
    assert "without asking to be continued" in reason


def test_a_light_codex_session_that_asks_is_handed_back_not_compacted(tmp_path, managed_pane):
    """The marker asks for a seam; it does not override the cut. Under the cut a boundary
    spends a summary to reclaim nothing, so the session is handed back where it stands --
    a name with no threshold, which is neither a skip nor a compaction."""
    path = rollout(tmp_path, meta(), task_started(), user("go"),
                   token_count(20000, ordinal=10), token_count(42000, ordinal=14),
                   assistant("Parking.\n" + CONT, ordinal=20),
                   task_complete(ordinal=21))
    name, threshold = cod.decide(path, "01a0ef3f", "codex", "agent/chat", wants_resume=True)
    assert name == managed_pane and threshold is None


def test_a_codex_boundary_already_on_file_stops_a_second_one(tmp_path, managed_pane):
    """The compaction the session just took is the newest thing on file. Reading the marker
    from the message above it and compacting again is the loop this guard exists to stop --
    and on codex the guard hangs entirely on the `compacted` record that nothing tested."""
    path = over_the_cut(tmp_path, "Parking.\n\n" + CONT)
    with open(path, "a") as fh:
        fh.write(json.dumps(compacted(ordinal=99)) + "\n")
    name, reason = cod.decide(path, "01a0ef3f", "codex", "agent/chat", wants_resume=True)
    assert name is None
    assert "boundary is already the newest thing" in reason


def test_the_deliverys_own_codex_sends_wait_for_the_paste_before_enter(monkeypatch):
    # /compact and the continue were typed into a Codex box and never submitted: the
    # Enter landed mid-paste. Every Codex submit now carries the beat send_input had.
    import jstack_host.compact_delivery as cd
    seen = []
    monkeypatch.setattr(cd, "send_text", lambda name, text, delay=0.0: seen.append(delay))
    monkeypatch.setattr(cd, "pane", lambda name: "")
    monkeypatch.setattr(cd, "took_effect", lambda *a: True)
    monkeypatch.setattr(cd.time, "sleep", lambda s: None)
    assert cd.submit("jr-x", cd.COMPACT_CMD, "codex")
    assert cd.submit("jr-x", "go on", "claude")
    assert seen == [cd.CODEX_ENTER_DELAY, 0.0]


# --- a job_monitor completion event is not somebody new speaking (jStack#334) -------------

def _after_park(tmp_path, *entries):
    path = rollout(tmp_path, meta(), assistant("Parked.\n" + CONT, ordinal=9))
    offset = os.path.getsize(path)
    with open(path, "a") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")
    return path, offset


def test_a_job_event_and_its_reply_do_not_supersede_the_seam(tmp_path):
    """On 2026-10-01 one thread asked for its seam fifteen times and lost every one to a
    queued job event landing first; it sat at 217k and climbing."""
    path, offset = _after_park(
        tmp_path, task_started(10),
        user("[job-monitor:8117285eb6ce4729ae18870d469edc37] Background job succeeded; "
             "exit_code=0.", ordinal=11),
        assistant("Verified. No rerun.\n" + CONT, ordinal=12), task_complete(13))
    assert cod.turn_moved(path, offset, "codex") is False


def test_a_person_speaking_after_a_job_event_still_supersedes(tmp_path):
    path, offset = _after_park(
        tmp_path, user("[job-monitor:8117285eb6ce4729ae18870d469edc37] Background job "
                       "succeeded; exit_code=0.", ordinal=11),
        assistant("Verified.", ordinal=12), user("now do the next thing", ordinal=13))
    assert cod.turn_moved(path, offset, "codex") is True


def test_a_person_speaking_still_supersedes(tmp_path):
    path, offset = _after_park(tmp_path, user("stop, wrong VM", ordinal=11))
    assert cod.turn_moved(path, offset, "codex") is True


def test_the_job_event_prefix_is_the_one_job_monitor_writes():
    """The prefix lives in two files; a one-sided edit would bring #334 back silently."""
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "tools/job_monitor.py").read_text()
    assert 'f"[job-monitor:{job_id}] ' in source
    assert cod.JOB_NOTICE == "[job-monitor:"
