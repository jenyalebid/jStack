"""Unit tests for jRemote session→pid correlation (board.pids_holding).

Regression for the Take-CLI bug: the close/kill path resolved the wrong pids
for a raw (sid-less) interactive `claude` because it correlated by mtime and
the single newest transcript, disagreeing with `_proc_scan` (birth time, 1:1).
It both OVER-matched (every window in a shared cwd resolved to one sid, so a
Take-CLI close SIGKILLed sibling sessions) and UNDER-matched (a live session
that was not the newest-*modified* transcript resolved to no pid, so close
cleared nothing while the scan still saw it live and the PTY attach 4409'd —
"can't take CLI of an active session"). pids_holding must return exactly the
owner `_proc_scan` assigns.
"""

import time

import pytest

import jstack_host.board as board
import jstack_host.procscan as procscan


def _mk(pd, sid: str):
    f = pd / f"{sid}.jsonl"
    f.write_text("{}\n")
    return f


def _raw(pid, loc):
    return {"pid": pid, "session_id": None, "location": loc,
            "source": "cli", "uptime_minutes": 0}


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """A fake claude-projects root with one workspace dir; yields (loc, pd)."""
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(board, "_CLAUDE_PROJECTS", projects)
    loc = "/ws/chat"
    pd = projects / loc.replace("/", "-").replace(".", "-")
    pd.mkdir()
    return loc, pd


def _procs(monkeypatch, procs):
    monkeypatch.setattr(procscan, "get_claude_processes",
                        lambda: {"processes": procs})


def test_argv_sid_matches_directly(ws, monkeypatch):
    loc, pd = ws
    _mk(pd, "1111aaaa")
    _procs(monkeypatch, [
        {"pid": 10, "session_id": "1111aaaa", "location": loc,
         "source": "cli", "uptime_minutes": 1},
    ])
    assert board.pids_holding("1111aaaa") == [10]


def test_two_raw_windows_no_collateral(ws, monkeypatch):
    """Two raw claudes in one cwd, two transcripts: each sid resolves to exactly
    one pid, never both — the over-match that let a close kill siblings."""
    loc, pd = ws
    _mk(pd, "aaaa0001")            # older birth
    time.sleep(0.02)
    _mk(pd, "bbbb0002")            # newer birth
    _procs(monkeypatch, [_raw(10, loc), _raw(20, loc)])

    h_new = board.pids_holding("bbbb0002")
    h_old = board.pids_holding("aaaa0001")
    assert len(h_new) == 1 and len(h_old) == 1
    assert set(h_new).isdisjoint(h_old)          # no pid claimed by both sids


def test_live_session_not_newest_mtime_still_resolves(ws, monkeypatch):
    """The hard 'can't take CLI' case: a live raw session that is NOT the
    newest-*modified* transcript. The old mtime rule returned [], so close
    cleared nothing while the scan still saw it live and the PTY 4409'd."""
    loc, pd = ws
    _mk(pd, "aaaa0001")           # the session we want to take (older birth)
    time.sleep(0.02)
    b = _mk(pd, "bbbb0002")       # a newer session in the same cwd
    time.sleep(0.02)
    b_ref = _mk(pd, "aaaa0001")   # ...but 'aaaa0001' was modified most recently
    b_ref.write_text("{}\n{}\n")
    assert b_ref.stat().st_mtime > b.stat().st_mtime
    _procs(monkeypatch, [_raw(10, loc), _raw(20, loc)])

    # 'bbbb0002' is no longer the newest-mtime transcript, yet it still resolves
    # to exactly one live pid — the birth-time owner, so it can be taken.
    assert len(board.pids_holding("bbbb0002")) == 1


def test_unknown_session_resolves_to_nothing(ws, monkeypatch):
    loc, pd = ws
    _mk(pd, "aaaa0001")
    _procs(monkeypatch, [_raw(10, loc)])
    assert board.pids_holding("ffffdead") == []


# ── Window truth: "a terminal window is open" is observed, never inferred ──
#
# Regression for the home-screen bug: `window` was `source in ("cli","vscode")`
# — an argv shape meaning "launched interactively", which inside tmux stays
# true forever. Closing the iTerm window kills only the tmux *client*; the
# pane, its tty and the claude all survive, so every window the user closed kept
# reporting itself open and the app's board drifted from their screen.

_PANES = {"/dev/ttys002": "jr-aaaa0001", "/dev/ttys004": "jr-bbbb0002"}


def _windows(monkeypatch, attached):
    monkeypatch.setattr(board, "_window_truth", lambda: (_PANES, set(attached)))


def test_tmux_pane_without_client_is_not_a_window():
    """THE bug: the pane tty outlives the window. No client → no window."""
    assert board._has_window("/dev/ttys002", _PANES, set()) is False


def test_tmux_pane_with_client_is_a_window():
    assert board._has_window("/dev/ttys002", _PANES, {"jr-aaaa0001"}) is True


def test_other_sessions_client_does_not_lend_a_window():
    """Attachment is per-session — a client on a sibling proves nothing."""
    assert board._has_window("/dev/ttys002", _PANES, {"jr-bbbb0002"}) is False


def test_headless_worker_is_never_a_window():
    """No controlling tty → a cron/headless run. Exact, not an argv guess."""
    assert board._has_window(None, _PANES, {"jr-aaaa0001"}) is False


def test_raw_terminal_window_counts():
    """A tty that is no tmux pane is a real window — closing it SIGHUPs the
    process, so the process existing IS the window."""
    assert board._has_window("/dev/ttys099", _PANES, set()) is True


def test_tmux_failure_falls_back_to_raw_rule():
    """If tmux can't be read we report no panes; every tty then counts as a
    raw window. Fail toward showing a session, never toward inventing one."""
    assert board._has_window("/dev/ttys002", {}, set()) is True


def test_scan_marks_detached_pane_not_a_window(ws, monkeypatch):
    """End-to-end through _proc_scan: a claude in a client-less tmux pane is
    live-capable but carries window=False, so nothing downstream can render it
    as a terminal open on the Mac."""
    loc, pd = ws
    _mk(pd, "aaaa0001")
    _windows(monkeypatch, attached=set())
    _procs(monkeypatch, [
        {"pid": 10, "session_id": "aaaa0001", "location": loc,
         "source": "cli", "tty": "/dev/ttys002", "uptime_minutes": 5},
    ])
    sessions, orphans = board._proc_scan()
    assert sessions["aaaa0001"]["window"] is False
    assert sessions["aaaa0001"]["headless"] is False   # a terminal owns it
    assert orphans == []


def test_scan_marks_attached_pane_a_window(ws, monkeypatch):
    loc, pd = ws
    _mk(pd, "aaaa0001")
    _windows(monkeypatch, attached={"jr-aaaa0001"})
    _procs(monkeypatch, [
        {"pid": 10, "session_id": "aaaa0001", "location": loc,
         "source": "cli", "tty": "/dev/ttys002", "uptime_minutes": 5},
    ])
    sessions, _ = board._proc_scan()
    assert sessions["aaaa0001"]["window"] is True


def test_detached_session_is_not_pinned_live(ws, monkeypatch):
    """`_is_live` keys on headless, not on window. Keying it on window (which
    now means 'displayed') would make every closed-window session report live
    forever — the same lie, one field over."""
    stale = time.time() - 10_000
    assert board._is_live("s", False, stale, time.time(), set()) is False   # idle terminal
    assert board._is_live("s", True, 0.0, time.time(), set()) is True       # headless worker


def test_headless_worker_stays_live_mid_tool_call():
    """A worker writes nothing during a long tool call — it must not vanish."""
    stale = time.time() - 10_000
    assert board._is_live("s", True, stale, time.time(), set()) is True


# ── The working signal reads the transcript tail (_turn_open) ────────────────
#
# Long tool calls write nothing for minutes; the dot must not flicker off
# mid-build. But a session waiting on a permission prompt / question is
# waiting on the user, not working — the discriminator for an open tool_use is a
# child process younger than the tool_use line.

import datetime as _dt
import json
import os
import pathlib
import subprocess


def _ts(offset=0.0):
    return _dt.datetime.fromtimestamp(
        time.time() + offset, _dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _line(kind, blocks, ts=None):
    return json.dumps({"type": kind, "timestamp": ts or _ts(),
                       "message": {"role": kind, "content": blocks}})


def _transcript(tmp_path, *lines):
    f = tmp_path / "t.jsonl"
    f.write_text("\n".join(lines) + "\n")
    return str(f)


def test_completed_turn_is_idle(tmp_path):
    p = _transcript(tmp_path,
                    _line("user", [{"type": "text", "text": "do it"}]),
                    _line("assistant", [{"type": "text", "text": "done."}]))
    assert board._turn_open(p, [os.getpid()]) is False


def test_trailing_system_lines_do_not_hide_the_verdict(tmp_path):
    # Idle tail in the wild: assistant text, then system bookkeeping lines.
    p = _transcript(tmp_path,
                    _line("assistant", [{"type": "text", "text": "done."}]),
                    json.dumps({"type": "system", "timestamp": _ts()}),
                    json.dumps({"type": "system", "timestamp": _ts()}))
    assert board._turn_open(p, [os.getpid()]) is False


def test_fresh_prompt_is_working(tmp_path):
    p = _transcript(tmp_path,
                    _line("assistant", [{"type": "text", "text": "done."}]),
                    _line("user", [{"type": "text", "text": "next thing"}]))
    assert board._turn_open(p, [os.getpid()]) is True


def test_string_content_prompt_is_working(tmp_path):
    p = _transcript(tmp_path, json.dumps(
        {"type": "user", "timestamp": _ts(),
         "message": {"role": "user", "content": "plain string prompt"}}))
    assert board._turn_open(p, [os.getpid()]) is True


def _user_str(text, ts=None, **extra):
    """A `user` line whose content is a bare string — how the harness writes
    a typed prompt and every one of its own bookkeeping lines alike."""
    return json.dumps({"type": "user", "timestamp": ts or _ts(),
                       "message": {"role": "user", "content": text}, **extra})


# A local slash command — one Claude Code runs itself, with no prompt to the
# model — closes with four `user` lines nobody owes a reply to. `/compact` is
# the one that matters: `compact_on_delivery.py` (a Stop hook) types it at the
# end of every heavy turn, so on the busiest sessions we run it is the LAST
# thing in the transcript, forever.

def test_a_delivery_compaction_leaves_the_session_idle(tmp_path):
    """The exact tail a Stop-hook `/compact` leaves, in file order.

    Read as prompts, the compaction summary and the command's own stdout mean
    "claude owes a reply", and the dot stayed green for the rest of the
    session's life — never orange, on exactly the deliveries the user is watching
    for. `/compact` submits no prompt, so no turn marker vouches for it
    either: the tail was the whole story, and it has to say idle by itself."""
    p = _transcript(
        tmp_path,
        _line("assistant", [{"type": "text", "text": "shipped."}]),
        json.dumps({"type": "system", "subtype": "compact_boundary",
                    "timestamp": _ts()}),
        _user_str("This session is being continued from a previous "
                  "conversation that ran out of context...",
                  isCompactSummary=True),
        _user_str("<local-command-caveat>Caveat: The messages below were "
                  "generated by the user while running local commands."
                  "</local-command-caveat>", isMeta=True),
        _user_str("<command-name>/compact</command-name>\n"
                  "<command-message>compact</command-message>"),
        _user_str("<local-command-stdout>Compacted (ctrl+o to see full "
                  "summary)</local-command-stdout>"))
    assert board._turn_sampled(p, [os.getpid()]) == "idle"


def test_a_compaction_does_not_bury_the_error_it_landed_on(tmp_path):
    """Skipping bookkeeping walks to the last real word, whatever it is — an
    API failure still outranks, because nothing moves until the user does."""
    p = _transcript(
        tmp_path,
        json.dumps({"type": "assistant", "timestamp": _ts(),
                    "isApiErrorMessage": True,
                    "message": {"role": "assistant",
                                "content": [{"type": "text",
                                             "text": "API Error: 500"}]}}),
        _user_str("<command-name>/compact</command-name>"),
        _user_str("<local-command-stdout>Compacted</local-command-stdout>"))
    assert board._turn_sampled(p, [os.getpid()]) == "error"


def test_a_typed_path_is_a_prompt_not_a_command(tmp_path):
    """The user pastes absolute paths as whole messages. The bookkeeping test is a
    closed list of tags for this reason — `startswith('/')` would read a real
    prompt as an echo and draw a working session at rest."""
    p = _transcript(tmp_path,
                    _line("assistant", [{"type": "text", "text": "done."}]),
                    _user_str("/Users/x/.claude/projects/x.jsonl"))
    assert board._turn_sampled(p, [os.getpid()]) == "working"


def test_a_task_notification_still_owes_a_reply(tmp_path):
    """Tag-wrapped and a bare string, exactly like the bookkeeping — but a
    finished background task re-invokes the model, so this one is a turn.
    `startswith('<')` would have swallowed it."""
    p = _transcript(tmp_path,
                    _line("assistant", [{"type": "text", "text": "spawned."}]),
                    _user_str("<task-notification>agent finished"
                              "</task-notification>"))
    assert board._turn_sampled(p, [os.getpid()]) == "working"


def test_a_custom_slash_command_reads_working_off_its_expanded_prompt(tmp_path):
    """`/push` and friends write `<command-name>` too, and skipping it is
    still right: the harness follows it immediately with the expansion as a
    list-content `user` line, which the backward walk reaches first."""
    p = _transcript(tmp_path,
                    _line("assistant", [{"type": "text", "text": "done."}]),
                    _user_str("<command-message>push</command-message>\n"
                              "<command-name>/push</command-name>"),
                    _line("user", [{"type": "text", "text": "Commit and push"}]))
    assert board._turn_sampled(p, [os.getpid()]) == "working"


def test_interrupt_is_idle(tmp_path):
    p = _transcript(tmp_path,
                    _line("user", [{"type": "text",
                                    "text": "[Request interrupted by user]"}]))
    assert board._turn_open(p, [os.getpid()]) is False


def test_thinking_tail_is_working(tmp_path):
    """The thinking block persists when the stream STARTS; a completed turn
    always ends in text or tool_use. An alive session on this tail is
    mid-generation — the state a takeover must not read as at-rest."""
    p = _transcript(tmp_path,
                    _line("user", [{"type": "text", "text": "long ask"}]),
                    _line("assistant", [{"type": "thinking", "thinking": ""}]))
    assert board._turn_open(p, [os.getpid()]) is True


def test_blockless_assistant_tail_is_working(tmp_path):
    p = _transcript(tmp_path,
                    _line("user", [{"type": "text", "text": "long ask"}]),
                    _line("assistant", []))
    assert board._turn_open(p, [os.getpid()]) is True


def test_string_content_assistant_tail_is_idle(tmp_path):
    p = _transcript(tmp_path, json.dumps(
        {"type": "assistant", "timestamp": _ts(),
         "message": {"role": "assistant", "content": "done."}}))
    assert board._turn_open(p, [os.getpid()]) is False


def test_tool_result_being_digested_is_working(tmp_path):
    p = _transcript(tmp_path,
                    _line("assistant", [{"type": "tool_use", "name": "Bash"}]),
                    _line("user", [{"type": "tool_result", "content": "ok"}]))
    assert board._turn_open(p, [os.getpid()]) is True


def test_stale_tool_result_is_a_dead_turn_not_a_working_one(tmp_path):
    """A tool_result is a `user` line, and a `user` line used to mean "claude
    owes a reply" with no clock on it. So a turn killed between the result and
    the next block — an interrupt the harness never recorded, a crash, a
    session torn down mid-tool — pinned the row green for as long as the
    session lived, and the app drew a working dot on a session sitting idle at
    a prompt. After a tool result claude's next block is one API round-trip
    away, never an hour, so age is the honest test."""
    p = _transcript(tmp_path,
                    _line("assistant", [{"type": "tool_use", "name": "Bash"}],
                          ts=_ts(-4000)),
                    _line("user", [{"type": "tool_result", "content": "ok"}],
                          ts=_ts(-3900)))
    assert board._turn_open(p, [os.getpid()]) is False


def test_a_leaked_turn_marker_cannot_hold_a_silent_transcript_working(tmp_path):
    """The marker is cleared by Stop/SessionEnd only, so a turn killed
    mid-flight leaks one — and its own sweep is 12 hours away. It may bridge a
    sampled gap (a model thinking between lines), never a transcript that has
    stopped moving, or a finished session shows a working dot all day."""
    p = _transcript(tmp_path,
                    _line("assistant", [{"type": "text", "text": "done."}]))
    marker = board._TURN_DIR / pathlib.Path(p).stem
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("")
    try:
        assert board._turn_marker_open(p) is True   # the marker IS set
        old = time.time() - board._TURN_MARKER_GAP - 60
        os.utime(p, (old, old))                     # ...but nothing has moved
        assert board._turn_state(p, [os.getpid()]) == "idle"
    finally:
        marker.unlink(missing_ok=True)


def test_a_turn_marker_still_bridges_a_live_sampled_gap(tmp_path):
    """The override's real job survives: a transcript reading idle while it is
    still being written is a gap mid-turn, not the end of one."""
    p = _transcript(tmp_path,
                    _line("assistant", [{"type": "text", "text": "thinking…"}]))
    marker = board._TURN_DIR / pathlib.Path(p).stem
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("")
    try:
        assert board._turn_state(p, [os.getpid()]) == "working"
    finally:
        marker.unlink(missing_ok=True)
        board._moved_sizes.clear()


def test_an_hourly_keepalive_touch_does_not_revive_a_leaked_marker(tmp_path):
    """An idle Claude Code session touches its own transcript once an hour —
    same bytes, fresh mtime. That must not read as a turn writing: paired with
    a marker leaked by an interrupted turn it would re-pin the dot green for
    ten minutes out of every hour, on a session that finished long ago."""
    p = _transcript(tmp_path,
                    _line("assistant", [{"type": "text", "text": "done."}]))
    marker = board._TURN_DIR / pathlib.Path(p).stem
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("")
    board._moved_sizes.clear()
    try:
        assert board._turn_state(p, [os.getpid()]) == "working"   # seen once
        now = time.time()
        os.utime(p, (now, now))          # the keep-alive: mtime moves, size not
        assert board._turn_state(p, [os.getpid()]) == "idle"
        os.utime(p, (now, now))          # and it stays dead, hour after hour
        assert board._turn_state(p, [os.getpid()]) == "idle"
    finally:
        marker.unlink(missing_ok=True)
        board._moved_sizes.clear()


def test_a_growing_transcript_still_reads_as_a_turn_in_flight(tmp_path):
    """The other side of the same guard: real work appends, and appending is
    exactly what separates a live turn from the keep-alive."""
    p = _transcript(tmp_path,
                    _line("assistant", [{"type": "text", "text": "thinking…"}]))
    marker = board._TURN_DIR / pathlib.Path(p).stem
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("")
    board._moved_sizes.clear()
    try:
        assert board._turn_state(p, [os.getpid()]) == "working"
        with open(p, "a") as f:
            f.write(_line("assistant", [{"type": "text", "text": "still on it"}]) + "\n")
        assert board._turn_state(p, [os.getpid()]) == "working"
    finally:
        marker.unlink(missing_ok=True)
        board._moved_sizes.clear()


def test_a_typed_prompt_owes_a_reply_however_long_it_waits(tmp_path):
    """The clock is on tool results ONLY. A prompt the user typed is owed a reply
    no matter how long claude has been thinking — that row stays working."""
    p = _transcript(tmp_path,
                    _line("user", [{"type": "text", "text": "do it"}],
                          ts=_ts(-3900)))
    assert board._turn_open(p, [os.getpid()]) is True


def test_open_tool_use_with_young_child_is_working(tmp_path):
    child = subprocess.Popen(["sleep", "5"])
    try:
        p = _transcript(tmp_path,
                        _line("assistant", [{"type": "tool_use", "name": "Bash"}],
                              ts=_ts(-30)))
        assert board._turn_open(p, [os.getpid()]) is True
    finally:
        child.kill()
        child.wait()


def test_open_tool_use_without_child_is_waiting_on_the_user(tmp_path):
    # Permission prompt / AskUserQuestion: tool_use at the tail, nothing
    # executing underneath. That's waiting, not working.
    p = _transcript(tmp_path,
                    _line("assistant", [{"type": "tool_use", "name": "Bash"}],
                          ts=_ts(-30)))
    assert board._turn_open(p, [4194311]) is False  # beyond macOS pid_max — never a live pid


def test_childless_tool_use_is_idle_never_waiting(tmp_path):
    # The tail must NOT be read as waiting-on-a-dialog: MCP and in-process
    # tools never fork a child, so a childless tool_use is routinely a
    # session hard at work. Dialogs come from the hook markers instead.
    for age in (-2, -30, -300):
        p = _transcript(tmp_path,
                        _line("assistant", [{"type": "tool_use", "name": "Bash"}],
                              ts=_ts(age)))
        assert board._turn_state(p, [4194311]) == "idle"


def _error_line(ts=None):
    return json.dumps({
        "type": "assistant", "timestamp": ts or _ts(), "isApiErrorMessage": True,
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "You've hit your session limit"}]}})


def test_api_error_tail_is_error(tmp_path):
    # The red dot's second half: the session's last word is an API failure.
    p = _transcript(tmp_path,
                    _line("user", [{"type": "text", "text": "do it"}]),
                    _error_line())
    assert board._turn_state(p, [os.getpid()]) == "error"


def test_a_message_past_the_error_clears_it(tmp_path):
    # The user typed on — the tail moved past the failure, red dies by itself.
    p = _transcript(tmp_path,
                    _error_line(ts=_ts(-60)),
                    _line("user", [{"type": "text", "text": "try again"}]))
    assert board._turn_state(p, [os.getpid()]) == "working"


# ── The red dot's waiting source is Claude Code's own hook events ────────────
#
# attention_hook.py (wired in ~/.claude/settings.json) writes one marker file
# per session while a select dialog is up — the harness says a dialog exists,
# nothing is inferred from screen content or transcript shape. These tests run
# the real script as the hook runner does: a subprocess with JSON on stdin.

_HOOK = os.path.join(os.path.dirname(board.__file__), "attention_hook.py")


def _run_hook(mode, payload, marker_dir, turn_dir=None):
    env = dict(os.environ, JREMOTE_ATTENTION_DIR=str(marker_dir),
               JREMOTE_TURN_DIR=str(turn_dir or marker_dir / "turn"))
    subprocess.run([_HOOK, mode], input=json.dumps(payload).encode(),
                   env=env, check=True, timeout=10)


def test_dialog_tools_and_permission_prompts_set_markers(tmp_path):
    d = tmp_path / "markers"
    _run_hook("set", {"session_id": "s-q", "hook_event_name": "PreToolUse",
                      "tool_name": "AskUserQuestion"}, d)
    _run_hook("set", {"session_id": "s-p", "hook_event_name": "Notification",
                      "message": "Claude needs your permission to use Bash"}, d)
    assert json.loads((d / "s-q").read_text())["kind"] == "question"
    assert json.loads((d / "s-p").read_text())["kind"] == "permission"


def test_non_dialog_events_never_mark(tmp_path):
    # An idle-session notification and an ordinary tool are not dialogs.
    d = tmp_path / "markers"
    _run_hook("set", {"session_id": "s1", "hook_event_name": "Notification",
                      "message": "Claude is waiting for your input"}, d)
    _run_hook("set", {"session_id": "s2", "hook_event_name": "PreToolUse",
                      "tool_name": "Bash"}, d)
    assert not d.exists() or list(d.iterdir()) == []


def test_resolution_events_clear_the_marker(tmp_path):
    d = tmp_path / "markers"
    _run_hook("set", {"session_id": "s-q", "hook_event_name": "PreToolUse",
                      "tool_name": "AskUserQuestion"}, d)
    _run_hook("clear", {"session_id": "s-q", "hook_event_name": "PostToolUse",
                        "tool_name": "AskUserQuestion"}, d)
    assert list(d.iterdir()) == []
    # Clearing with no marker present is a quiet no-op.
    _run_hook("clear", {"session_id": "s-q", "hook_event_name": "Stop"}, d)


# ── The turn clock: the harness says when a turn opens and closes ────────────
#
# Mid-turn the transcript trails its last narration text while the model
# thinks before its next tool call; a tick sampled in that gap read idle,
# flapped the working dot, and fired a done-push per narration line of a long
# autonomous turn. attention_hook.py keeps a per-session turn marker from the
# harness's own events — UserPromptSubmit opens, Stop/SessionEnd closes — and
# _turn_state believes it over an idle-reading tail.

def test_prompt_submit_opens_the_turn_and_stop_closes_it(tmp_path):
    d, turn = tmp_path / "markers", tmp_path / "turn"
    _run_hook("clear", {"session_id": "s-t",
                        "hook_event_name": "UserPromptSubmit"}, d, turn)
    assert (turn / "s-t").exists()
    _run_hook("clear", {"session_id": "s-t", "hook_event_name": "Stop"}, d, turn)
    assert not (turn / "s-t").exists()


def test_open_turn_marker_outranks_an_idle_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(board, "_TURN_DIR", tmp_path / "turn")
    (tmp_path / "turn").mkdir()
    (tmp_path / "turn" / "t").write_text("open")
    p = _transcript(tmp_path, _line("assistant", [
        {"type": "text", "text": "step one landed — building next"}]))
    assert board._turn_open(p, [os.getpid()]) is True


def test_error_tail_outranks_an_open_turn_marker(tmp_path, monkeypatch):
    # An API failure is the session's last word, however open the turn.
    monkeypatch.setattr(board, "_TURN_DIR", tmp_path / "turn")
    (tmp_path / "turn").mkdir()
    (tmp_path / "turn" / "t").write_text("open")
    p = _transcript(tmp_path, _error_line())
    assert board._turn_state(p, [os.getpid()]) == "error"


def test_no_marker_keeps_the_sampled_idle_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(board, "_TURN_DIR", tmp_path / "turn")
    p = _transcript(tmp_path,
                    _line("assistant", [{"type": "text", "text": "done."}]))
    assert board._turn_open(p, [os.getpid()]) is False


def test_stale_turn_marker_is_swept_not_believed(tmp_path, monkeypatch):
    monkeypatch.setattr(board, "_TURN_DIR", tmp_path / "turn")
    (tmp_path / "turn").mkdir()
    marker = tmp_path / "turn" / "t"
    marker.write_text("open")
    old = time.time() - 13 * 3600
    os.utime(marker, (old, old))
    p = _transcript(tmp_path,
                    _line("assistant", [{"type": "text", "text": "done."}]))
    assert board._turn_open(p, [os.getpid()]) is False
    assert not marker.exists()


def test_dialog_sids_reads_markers_and_sweeps_ancient_ones(tmp_path, monkeypatch):
    d = tmp_path / "markers"
    d.mkdir()
    monkeypatch.setattr(board, "_ATTENTION_DIR", d)
    (d / "fresh").write_text('{"kind": "question"}')
    stale = d / "stale"
    stale.write_text('{"kind": "permission"}')
    old = time.time() - 7 * 3600
    os.utime(stale, (old, old))
    assert board._dialog_sids() == {"fresh"}
    assert not stale.exists()


def test_mcp_servers_do_not_count_as_tool_children(tmp_path):
    # A child older than the tool_use line (an MCP server, up since session
    # start) must not read as an executing tool.
    child = subprocess.Popen(["sleep", "5"])
    try:
        time.sleep(0.1)
        p = _transcript(tmp_path,
                        _line("assistant", [{"type": "tool_use", "name": "Bash"}],
                              ts=_ts(+30)))
        assert board._turn_open(p, [os.getpid()]) is False
    finally:
        child.kill()
        child.wait()


def test_is_live_consults_the_tail_when_the_transcript_goes_quiet(tmp_path):
    stale = time.time() - 10_000
    open_turn = _transcript(tmp_path,
                            _line("user", [{"type": "text", "text": "go"}]))
    assert board._is_live("s", False, stale, time.time(), set(),
                          open_turn, [os.getpid()]) is True
    # And without pids (no process knowledge) the stale verdict stands.
    assert board._is_live("s", False, stale, time.time(), set(),
                          open_turn, ()) is False


# ── ending the Mac's claude, and who is allowed to ask for it ───────────────
#
# One path signals the process holding a session (`end_raw_holders`), shared by
# the close endpoint and by the takeover that moves a session onto the phone.
# The takeover's whole correctness is an ordering: the window first, the kill
# second — so a take that can't get a terminal costs the user nothing.

import signal as _signal

import jstack_host.managed as managed
import jstack_host.router as router
import jstack_host.transcripts as transcripts


def test_end_raw_holders_signals_the_owner_and_waits_for_it(ws, monkeypatch):
    loc, pd = ws
    _mk(pd, "1111aaaa")
    _procs(monkeypatch, [_raw(10, loc)])
    killed = []
    monkeypatch.setattr(board.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(board, "_alive", lambda pid: False)  # it exited

    assert board.end_raw_holders("1111aaaa", _signal.SIGKILL) is True
    assert killed == [(10, _signal.SIGKILL)]


def test_end_raw_holders_reports_a_process_that_would_not_die(ws, monkeypatch):
    loc, pd = ws
    _mk(pd, "1111aaaa")
    _procs(monkeypatch, [_raw(10, loc)])
    monkeypatch.setattr(board.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(board, "_alive", lambda pid: True)  # ignores the signal

    assert board.end_raw_holders("1111aaaa", _signal.SIGKILL, timeout=0.6) is False


def test_a_dead_holder_is_not_kept_alive_by_a_sibling_in_the_same_workspace(
        ws, monkeypatch):
    """The live bug: the process scan re-assigns an orphaned transcript to
    another `claude` running in the same directory, so the moment the real
    holder dies the session reads live *again* — forever. A wait on that never
    returns, and the caller is told the kill failed after it already succeeded:
    session gone, terminal never delivered. Freedom is judged from the pids we
    signalled, so the sibling is irrelevant."""
    loc, pd = ws
    _mk(pd, "1111aaaa")
    _procs(monkeypatch, [_raw(10, loc)])
    monkeypatch.setattr(board.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(board, "_alive", lambda pid: False)   # the holder died
    monkeypatch.setattr(board, "_live_session_ids",
                        lambda: {"1111aaaa"})                 # a sibling claimed it

    assert board.end_raw_holders("1111aaaa", _signal.SIGKILL, timeout=0.6) is True


def _open_route(monkeypatch, *, live: bool, open_managed, mid_turn=None):
    """Call the /open route with the host faked out; returns nothing but lets
    `open_managed` record how it was called."""
    sid = "1111aaaa-2222-3333-4444-555555555555"
    monkeypatch.setattr(transcripts, "_find_session_cwd", lambda s: "/ws/chat")
    monkeypatch.setattr(managed, "is_open", lambda s: False)
    monkeypatch.setattr(managed, "record_open", lambda s, b: None)
    monkeypatch.setattr(managed, "open_managed", open_managed)
    monkeypatch.setattr(board, "mid_turn", mid_turn or (lambda s: False))
    monkeypatch.setattr(board, "_live_session_ids",
                        lambda: {sid} if live else set())
    return router.open_session_managed(sid)


def test_open_displaces_only_a_session_a_mac_window_still_holds(monkeypatch):
    seen = {}
    def fake(sid, cwd, resume=True, displace=None, nudge=None):
        seen["displace"] = displace
    _open_route(monkeypatch, live=True, open_managed=fake)
    assert seen["displace"] is not None, "a live raw session must be displaced"
    _open_route(monkeypatch, live=False, open_managed=fake)
    assert seen["displace"] is None, "an idle session has nothing to displace"


def test_failed_open_never_touches_the_mac_session(monkeypatch):
    """The regression this whole path exists for: the phone used to end the
    Mac's claude first and only then discover it couldn't stand up the managed
    session — work gone, nothing gained. The displace hook is handed to
    `open_managed` and is its to call once the managed session exists; an open
    that fails before that must never have run it."""
    called = []
    def fake(sid, cwd, resume=True, displace=None, nudge=None):
        raise RuntimeError("tmux is down")
    monkeypatch.setattr(board, "end_raw_holders",
                        lambda *a, **k: called.append(True) or True)
    with pytest.raises(RuntimeError):
        _open_route(monkeypatch, live=True, open_managed=fake)
    assert called == [], "nothing may be killed for a session that never stood up"


def test_takeover_closes_the_window_the_raw_claude_left_behind(monkeypatch):
    """Displacing a raw holder empties the iTerm window it lived in — a bare
    prompt that reads as work on the desk. The takeover owns that window's
    close, with the tty read before the kill (a dead process can't name it)."""
    events = []
    monkeypatch.setattr(board, "pids_holding", lambda s: [42])
    monkeypatch.setattr(board, "window_ttys",
                        lambda pids: events.append("ttys") or ["/dev/ttys007"])
    monkeypatch.setattr(board, "end_raw_holders",
                        lambda s, sig: events.append("kill") or True)
    monkeypatch.setattr(managed, "close_windows",
                        lambda ttys: events.append(("close", tuple(ttys))))
    def fake(sid, cwd, resume=True, displace=None, nudge=None):
        assert displace() is True
    _open_route(monkeypatch, live=True, open_managed=fake)
    assert events == ["ttys", "kill", ("close", ("/dev/ttys007",))], \
        "tty read before the kill, window closed after it"


def test_takeover_that_fails_leaves_the_raw_window_alone(monkeypatch):
    """A holder that won't exit keeps its session — and its window."""
    closed = []
    monkeypatch.setattr(board, "pids_holding", lambda s: [42])
    monkeypatch.setattr(board, "window_ttys", lambda pids: ["/dev/ttys007"])
    monkeypatch.setattr(board, "end_raw_holders", lambda s, sig: False)
    monkeypatch.setattr(managed, "close_windows", closed.append)
    def fake(sid, cwd, resume=True, displace=None, nudge=None):
        assert displace() is False
    _open_route(monkeypatch, live=True, open_managed=fake)
    assert closed == [], "a window whose claude survived is not garbage"


# ── a session taken while working keeps working ──────────────────────────────
#
# Take CLI SIGKILLs the raw claude and `--resume`s it in tmux — which parks a
# mid-turn session at a waiting prompt with its work dead. The takeover judges
# the working dot's own signal (mid_turn) BEFORE the kill and hands open_managed
# a continue nudge to type once the resumed claude is back at its prompt.

def test_takeover_of_a_working_session_carries_the_continue_nudge(monkeypatch):
    seen = {}
    def fake(sid, cwd, resume=True, displace=None, nudge=None):
        seen["nudge"] = nudge
    _open_route(monkeypatch, live=True, open_managed=fake,
                mid_turn=lambda s: True)
    assert seen["nudge"] == router.TAKEOVER_CONTINUE, \
        "a working session must be told to continue after the move"


def test_takeover_of_an_idle_session_stays_quiet(monkeypatch):
    """A session at rest comes up waiting, unchanged — no phantom prompt."""
    seen = {}
    def fake(sid, cwd, resume=True, displace=None, nudge=None):
        seen["nudge"] = nudge
    _open_route(monkeypatch, live=True, open_managed=fake,
                mid_turn=lambda s: False)
    assert seen["nudge"] is None
    _open_route(monkeypatch, live=False, open_managed=fake,
                mid_turn=lambda s: True)  # not a takeover: nothing to continue
    assert seen["nudge"] is None


def test_working_is_judged_before_the_kill(monkeypatch):
    """mid_turn's open-tool_use branch asks the old process for a live tool
    child — judged after the kill, every mid-tool session would read idle."""
    events = []
    monkeypatch.setattr(board, "pids_holding", lambda s: [42])
    monkeypatch.setattr(board, "window_ttys", lambda pids: [])
    monkeypatch.setattr(board, "end_raw_holders",
                        lambda s, sig: events.append("kill") or True)
    monkeypatch.setattr(managed, "close_windows", lambda ttys: None)
    def fake(sid, cwd, resume=True, displace=None, nudge=None):
        displace()
    _open_route(monkeypatch, live=True, open_managed=fake,
                mid_turn=lambda s: events.append("judge") or True)
    assert events == ["judge", "kill"], f"wrong order: {events}"


def test_mid_turn_reads_this_sessions_transcript(tmp_path, monkeypatch):
    sid = "cafe0000-1111-2222-3333-444444444444"
    pd = tmp_path / "-ws-chat"
    pd.mkdir()
    (pd / f"{sid}.jsonl").write_text(
        _line("user", [{"type": "text", "text": "go"}]) + "\n")
    monkeypatch.setattr(board, "_CLAUDE_PROJECTS", tmp_path)
    monkeypatch.setattr(board, "pids_holding", lambda s: [os.getpid()])
    assert board.mid_turn(sid) is True


def test_mid_turn_completed_turn_is_false(tmp_path, monkeypatch):
    sid = "cafe0000-1111-2222-3333-444444444444"
    pd = tmp_path / "-ws-chat"
    pd.mkdir()
    (pd / f"{sid}.jsonl").write_text(
        _line("assistant", [{"type": "text", "text": "done."}]) + "\n")
    monkeypatch.setattr(board, "_CLAUDE_PROJECTS", tmp_path)
    monkeypatch.setattr(board, "pids_holding", lambda s: [os.getpid()])
    assert board.mid_turn(sid) is False


def test_mid_turn_without_a_holder_is_false(tmp_path, monkeypatch):
    """No live pids = no children to ask; a working-looking tail alone must
    not claim mid-turn (same discipline as `_is_live`)."""
    sid = "cafe0000-1111-2222-3333-444444444444"
    pd = tmp_path / "-ws-chat"
    pd.mkdir()
    (pd / f"{sid}.jsonl").write_text(
        _line("user", [{"type": "text", "text": "go"}]) + "\n")
    monkeypatch.setattr(board, "_CLAUDE_PROJECTS", tmp_path)
    monkeypatch.setattr(board, "pids_holding", lambda s: [])
    assert board.mid_turn(sid) is False


# ── window truth must not rot with daemon uptime ────────────────────────────

def test_a_terminal_opened_after_startup_still_has_a_tty():
    """The board's whole window story rests on the controlling tty, and
    `psutil.Process.terminal()` resolves it through a map of `/dev` built once
    per process and memoized for its lifetime. macOS creates `/dev/ttysNNN`
    nodes on demand, so in a daemon that has been up for a while *every window
    opened since it started* resolves to None: real windows read as headless,
    fall out of On Mac into Headless, and show a working dot that never goes
    out (a headless run has no idle state).

    So the resolver is asked about a terminal created after it has already
    run once — exactly the daemon's situation, and the one psutil gets wrong."""
    import os
    import pty as _pty
    import psutil

    procscan._tty_map()                     # warm anything warmable
    psutil.Process(os.getpid()).terminal()   # and psutil's own memoized map

    pid, _master = _pty.fork()
    if pid == 0:
        try:
            os.execv("/bin/sleep", ["sleep", "5"])
        finally:
            os._exit(127)
    try:
        deadline = time.time() + 5
        tty = None
        while time.time() < deadline and not tty:
            tty = procscan._tty_map().get(pid)
            if not tty:
                time.sleep(0.05)
        assert tty and tty.startswith("/dev/tty"), \
            f"a terminal opened after startup resolved to {tty!r}"
    finally:
        os.kill(pid, 9)
        os.waitpid(pid, 0)


def test_a_headless_process_has_no_tty():
    """The other half: a worker with no controlling terminal must never
    acquire one, or every cron run would show up as a window on the desk."""
    import subprocess
    proc = subprocess.Popen(["/bin/sleep", "5"], start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        assert procscan._tty_map().get(proc.pid) is None, \
            "a session-leader with no terminal was reported as owning one"
    finally:
        proc.kill()
        proc.wait()


# ── a live window never adopts a dead session's transcript ──────────────────
#
# The incident this section exists for: The user closed a chat, and the Nova
# window still open beside it took over the closed session's transcript. On their
# phone the closed session sat in On Mac with a working dot, and the session they
# was actually typing into was nowhere on the board. Both facts wrong, from one
# rule: "the newest transcript born since this process started" answers a
# question about the workspace, not about this process.

def _birth_times(monkeypatch, births: dict):
    """Fake creation times by transcript stem (seconds from now, negative =
    in the past). Real files are all born now; ownership is about *when*."""
    import time as _t
    now = _t.time()
    monkeypatch.setattr(board, "_birth",
                        lambda f: now + births.get(f.stem, 0.0))


def test_a_closed_sessions_transcript_is_not_adopted(ws, monkeypatch):
    """One live window, two transcripts: its own (born just after it started)
    and a newer one from a session that has since been closed. The live window
    keeps its own; the dead session is claimed by nobody and leaves the board."""
    loc, pd = ws
    _mk(pd, "mine0001")
    _mk(pd, "dead0002")
    # The window has been up 30 min; its transcript appeared 2 min later, the
    # closed session's 16 min later.
    _birth_times(monkeypatch, {"mine0001": -28 * 60, "dead0002": -14 * 60})
    _procs(monkeypatch, [{"pid": 10, "session_id": None, "location": loc,
                          "source": "cli", "uptime_minutes": 30}])

    assert board.pids_holding("mine0001") == [10]
    assert board.pids_holding("dead0002") == [], \
        "a closed session was handed to a living window"

    sessions, orphans = board._proc_scan()
    assert set(sessions) == {"mine0001"}
    assert orphans == [], "the live window is not an orphan — it has its session"


def test_each_window_keeps_its_own_session(ws, monkeypatch):
    """Two windows in one workspace: neither may end up wearing the other's
    conversation, whichever order they started in."""
    loc, pd = ws
    _mk(pd, "older001")
    _mk(pd, "newer002")
    _birth_times(monkeypatch, {"older001": -60 * 60, "newer002": -5 * 60})
    _procs(monkeypatch, [
        {"pid": 10, "session_id": None, "location": loc, "source": "cli",
         "uptime_minutes": 61},      # the long-running window
        {"pid": 20, "session_id": None, "location": loc, "source": "cli",
         "uptime_minutes": 6},       # the one opened five minutes ago
    ])

    assert board.pids_holding("older001") == [10]
    assert board.pids_holding("newer002") == [20]


def test_a_transcript_older_than_the_process_is_never_claimed(ws, monkeypatch):
    """A session cannot predate the process running it. Without this, a fresh
    window with nothing typed in it yet inherits the last conversation left in
    that workspace — the board shows old work as live."""
    loc, pd = ws
    _mk(pd, "yesterd1")
    _birth_times(monkeypatch, {"yesterd1": -8 * 3600})
    _windows(monkeypatch, attached=set())
    _procs(monkeypatch, [{"pid": 10, "session_id": None, "location": loc,
                          "source": "cli", "tty": "/dev/ttys099",
                          "uptime_minutes": 2}])

    assert board.pids_holding("yesterd1") == []
    sessions, orphans = board._proc_scan()
    assert sessions == {} and [o["pid"] for o in orphans] == [10], \
        "a window with no session of its own belongs on the board as itself"


def test_the_close_path_and_the_board_name_the_same_owner(ws, monkeypatch):
    """`pids_holding` (what a kill signals) and `_proc_scan` (what the board
    shows) must resolve identically — a disagreement kills a session the board
    wasn't showing."""
    loc, pd = ws
    _mk(pd, "aaaa0001")
    _mk(pd, "bbbb0002")
    _birth_times(monkeypatch, {"aaaa0001": -50 * 60, "bbbb0002": -3 * 60})
    _procs(monkeypatch, [
        {"pid": 10, "session_id": None, "location": loc, "source": "cli",
         "uptime_minutes": 51},
        {"pid": 20, "session_id": None, "location": loc, "source": "cli",
         "uptime_minutes": 4},
    ])

    sessions, _ = board._proc_scan()
    for sid, info in sessions.items():
        assert board.pids_holding(sid) == info["pids"], f"{sid} disagrees"


# ── a session that exists before its transcript does ────────────────────────
#
# Claude Code writes the JSONL on the **first message**, not at startup. Every
# session spawned from the phone therefore runs at an empty prompt — process
# up, window on the desk, no transcript — until the user types into it. The board
# has to place that row somewhere sane the whole time, or the phone shows a
# nameless session with no age and no position.


def test_a_session_with_no_transcript_is_dated_by_its_process(ws, monkeypatch):
    """The live bug: a phone-spawned session sat three minutes between
    `claude` starting and the user's first message, and every board frame in that
    window carried `spawned: ""` — so the row had no age and no place in a
    board sorted by spawn. The process is the only thing that knows the
    session exists yet; date the row by it."""
    loc, pd = ws
    started = time.time() - 180
    monkeypatch.setattr(board, "_proc_start", lambda pids: started if pids else 0.0)
    _windows(monkeypatch, attached=set())
    _procs(monkeypatch, [
        {"pid": 10, "session_id": "1111aaaa", "location": loc, "source": "cli",
         "tty": "/dev/ttys099", "uptime_minutes": 3},
    ])

    rows = {r["session_id"]: r for r in board.active_sessions()}
    row = rows["1111aaaa"]
    assert row["spawned"], "a running session with no transcript still spawned"
    assert row["spawned"].startswith(
        _dt.datetime.fromtimestamp(started).isoformat()[:19])
    # And it stays honest about what it does not have yet: no transcript means
    # no title, no stats, no activity — the app reads `path` to say so.
    assert (row["path"], row["preview"], row["tokens"]) == ("", "", 0)
    assert row["last_activity"] == "" and row["live"] is False


def test_a_transcript_still_wins_the_spawn_stamp(ws, monkeypatch):
    """Once the transcript exists it is the session's birth — the process
    fallback must not override it (a resumed session's process is younger
    than the session it holds)."""
    loc, pd = ws
    _mk(pd, "1111aaaa")
    _birth_times(monkeypatch, {"1111aaaa": -3600})
    monkeypatch.setattr(board, "_proc_start", lambda pids: time.time())
    _windows(monkeypatch, attached=set())
    _procs(monkeypatch, [
        {"pid": 10, "session_id": "1111aaaa", "location": loc, "source": "cli",
         "tty": "/dev/ttys099", "uptime_minutes": 2},
    ])

    row = {r["session_id"]: r for r in board.active_sessions()}["1111aaaa"]
    # Tolerance, not a string prefix: the fixture and the assert each read the
    # clock, and a second boundary between the two reads must not flake this.
    born = _dt.datetime.fromisoformat(row["spawned"]).timestamp()
    assert abs(born - (time.time() - 3600)) < 5, "the transcript's birth is the spawn"


# ── /sessions (history) payload ────────────────────────────────────────────
# The app's history list sorts by spawn date; the stamp must come from the
# history endpoint itself, not only the board — a session that has left the
# board would otherwise never get one.

def test_history_rows_carry_spawned(ws, monkeypatch):
    loc, pd = ws
    _mk(pd, "hist0001")
    monkeypatch.setattr(board, "project_dir_to_agent",
                        lambda name: ("nova", "chat"))
    monkeypatch.setattr(board, "get_session_summary",
                        lambda f: {"total_tokens": 3, "last_context": 1})
    _birth_times(monkeypatch, {"hist0001": -3600})
    _windows(monkeypatch, attached=set())
    _procs(monkeypatch, [])

    rows = board.list_sessions("nova")
    assert [r["session_id"] for r in rows] == ["hist0001"]
    born = _dt.datetime.fromisoformat(rows[0]["spawned"]).timestamp()
    assert abs(born - (time.time() - 3600)) < 5, "spawned is the transcript's birth"


# ── a short scan must never read as an idle Mac ─────────────────────────────
#
# One board tick whose process scan came back empty rebuilt every managed
# session from the open registry with no pids. entry() then never consulted
# the turn clock, every mid-turn row read turn_open=False for that single
# tick, and the notify watcher fired a done-push for each — narration bodies,
# three agents, one second. Absence of evidence is not idleness:
#   scan raises      → the pass raises, board_watch skips the tick;
#   scan comes short → a registry row with an open turn marker stays working.


def _short_scan_row(ws, tmp_path, monkeypatch, sid, marker):
    """Board row for a managed session the process scan failed to see."""
    loc, pd = ws
    (pd / f"{sid}.jsonl").write_text(
        _line("user", [{"type": "text", "text": "go"}]) + "\n" +
        _line("assistant", [{"type": "text", "text": "step landed — next"}]) + "\n")
    _procs(monkeypatch, [])                      # the scan saw no claude at all
    _windows(monkeypatch, attached=set())
    turn = tmp_path / "turn"
    turn.mkdir(exist_ok=True)
    if marker:
        (turn / sid).write_text("open")
    monkeypatch.setattr(board, "_TURN_DIR", turn)
    from jstack_host import managed
    monkeypatch.setattr(managed, "open_registry",
                        lambda: {sid: {"agent": "x", "name": ""}})
    monkeypatch.setattr(managed, "attached_names", lambda: set())
    return {r["session_id"]: r for r in board.active_sessions()}[sid]


def test_scan_missed_session_with_open_turn_stays_working(ws, tmp_path, monkeypatch):
    row = _short_scan_row(ws, tmp_path, monkeypatch, "feedbeef", marker=True)
    assert row["managed"] is True
    assert row["turn"] == "working", "the harness's turn clock outranks a blind scan"


def test_scan_missed_session_without_marker_reads_idle(ws, tmp_path, monkeypatch):
    # The bound on the claim: no marker, no working verdict — crash residue
    # must not animate a dead row (the reason marker checks were once scoped
    # to sessions with a live process).
    row = _short_scan_row(ws, tmp_path, monkeypatch, "feedbeef", marker=False)
    assert row["turn"] == "idle"


def test_scan_failure_raises_instead_of_fabricating_a_board(monkeypatch):
    def boom():
        raise RuntimeError("psutil transient")
    monkeypatch.setattr(procscan, "get_claude_processes", boom)
    with pytest.raises(RuntimeError):
        board.active_sessions()


# ── a blind tty read must never read as an idle Mac ─────────────────────────
#
# The same law one observation over. A session's tty comes from `ps`, and a
# `ps` that fails or comes back short reports no terminal — which reads as
# headless, and headless skipped entry()'s whole turn-state block, so the row
# asserted turn_open=False having observed nothing at all. A managed session
# always owns a pane; a blank tty is a failed look, never a fact about it.


def _blind_tty_row(ws, tmp_path, monkeypatch, sid, marker, tty=None):
    """Board row for a managed session the scan saw but read no tty for."""
    loc, pd = ws
    (pd / f"{sid}.jsonl").write_text(
        _line("user", [{"type": "text", "text": "go"}]) + "\n" +
        _line("assistant", [{"type": "text", "text": "step landed — next"}]) + "\n")
    _procs(monkeypatch, [{"pid": os.getpid(), "session_id": sid, "location": loc,
                          "source": "cli", "uptime_minutes": 1, "tty": tty}])
    _windows(monkeypatch, attached=set())
    turn = tmp_path / "turn"
    turn.mkdir(exist_ok=True)
    if marker:
        (turn / sid).write_text("open")
    monkeypatch.setattr(board, "_TURN_DIR", turn)
    from jstack_host import managed
    monkeypatch.setattr(managed, "open_registry",
                        lambda: {sid: {"agent": "x", "name": ""}})
    monkeypatch.setattr(managed, "attached_names", lambda: set())
    return {r["session_id"]: r for r in board.active_sessions()}[sid]


def test_blind_tty_session_with_open_turn_stays_working(ws, tmp_path, monkeypatch):
    row = _blind_tty_row(ws, tmp_path, monkeypatch, "beadfeed", marker=True)
    assert row["managed"] is True
    assert row["turn"] == "working", "the harness's turn clock outranks a blind tty"


def test_blind_tty_session_without_marker_reads_idle(ws, tmp_path, monkeypatch):
    # The bound on the claim, same as the short scan's: no marker, no verdict.
    row = _blind_tty_row(ws, tmp_path, monkeypatch, "beadfeed", marker=False)
    assert row["turn"] == "idle"


def test_tty_map_failure_raises_instead_of_blinding_every_session(monkeypatch):
    # A ps that cannot be run is not a Mac where nothing owns a terminal.
    def boom(*a, **k):
        raise OSError("fork failed")
    monkeypatch.setattr(procscan.subprocess, "run", boom)
    with pytest.raises(OSError):
        procscan._tty_map()


# ── the idle verdict and the text it is paired with ─────────────────────────
#
# A row reads the transcript twice: once for the session summary (which is
# where `last_reply` comes from) and again, further down, for the turn state.
# The turn's final text landing between the two reads gave the watcher a
# fresh idle married to the PREVIOUS turn's answer — and the done-push told
# The user something they had already been told. The reply must never be older than
# the verdict it ships with.


def test_a_closed_turn_reports_the_reply_its_verdict_saw(ws, tmp_path,
                                                         monkeypatch):
    sid = "cafe1234"
    loc, pd = ws
    (pd / f"{sid}.jsonl").write_text(
        _line("user", [{"type": "text", "text": "go"}]) + "\n" +
        _line("assistant", [{"type": "text", "text": "the new answer"}]) + "\n")
    _procs(monkeypatch, [{"pid": os.getpid(), "session_id": sid,
                          "location": loc, "source": "cli",
                          "uptime_minutes": 1}])
    _windows(monkeypatch, attached=set())
    turn = tmp_path / "turn"
    turn.mkdir(exist_ok=True)
    monkeypatch.setattr(board, "_TURN_DIR", turn)
    from jstack_host import managed
    monkeypatch.setattr(managed, "open_registry",
                        lambda: {sid: {"agent": "x", "name": ""}})
    monkeypatch.setattr(managed, "attached_names", lambda: set())

    # The first read lands before the turn's closing text is flushed; every
    # read after it sees the finished transcript.
    reads = {"n": 0}

    def summary(path):
        reads["n"] += 1
        return {"last_msg": "the previous answer" if reads["n"] == 1
                else "the new answer", "last_prompt": "go"}

    monkeypatch.setattr(board, "get_session_summary", summary)
    row = {r["session_id"]: r for r in board.active_sessions()}[sid]
    assert row["turn"] == "idle"
    assert row["last_reply"] == "the new answer", (
        "a done-push must not carry text older than the idle that fired it")


# ── Coverage: a process spending tokens is a row, no exceptions ────────────
#
# The user's rule, in their words: "if it's using tokens, it must be shown." The
# board used to keep an unidentified process only `if window`, so three real
# shapes ran on this Mac while the app showed nothing: a raw `claude` in a
# detached tmux, a headless worker whose argv carries no sid, and every Codex
# not spawned through jRemote. Identity is how well a row is shown; it is
# never the price of being shown at all.
#
# The half-truth this comment used to carry — "Codex writes no Claude
# transcript, so there is nothing for `correlate_raw` to pair it with" — is
# how the phantom below got in. Correlation never asked who WROTE a file; it
# paired any sid-less process with the nearest unowned JSONL in the workspace,
# and Codex is sid-less too. See the correlation tests at the end of this file.

def _reg(monkeypatch, registry=None, attached=()):
    from jstack_host import managed
    monkeypatch.setattr(managed, "open_registry", lambda: registry or {})
    monkeypatch.setattr(managed, "attached_names", lambda: set(attached))


def test_unnamed_process_in_a_detached_pane_is_still_a_row(ws, monkeypatch):
    """THE bug, reproduced: a raw claude nobody is watching, with no transcript
    to be named by. It was dropped by the scan entirely."""
    loc, _ = ws
    _windows(monkeypatch, attached=set())
    _reg(monkeypatch)
    _procs(monkeypatch, [_raw(10, loc) | {"tty": "/dev/ttys002"}])

    sessions, orphans = board._proc_scan()
    assert sessions == {}
    assert [o["pid"] for o in orphans] == [10], "a running claude, dropped"
    assert orphans[0]["tty"] is True and orphans[0]["window"] is False


def test_headless_unnamed_worker_is_still_a_row(ws, monkeypatch):
    """No tty, no sid, no transcript — a worker burning tokens with nothing
    to identify it. The old `elif window` swallowed it too."""
    loc, _ = ws
    _windows(monkeypatch, attached=set())
    _reg(monkeypatch)
    _procs(monkeypatch, [_raw(11, loc)])

    _, orphans = board._proc_scan()
    assert [o["pid"] for o in orphans] == [11]
    assert orphans[0]["tty"] is False


def test_orphan_carries_the_engine_the_scan_saw(ws, monkeypatch):
    """A Codex that jRemote didn't spawn has no registry row to name its CLI.
    The scan knew — the board used to throw it away and default to claude."""
    loc, _ = ws
    _windows(monkeypatch, attached=set())
    _reg(monkeypatch)
    _procs(monkeypatch, [_raw(12, loc) | {"tty": "/dev/ttys002",
                                          "engine": "codex"}])

    rows = {r["session_id"]: r for r in board.active_sessions()}
    row = rows["pid-12"]
    assert row["engine"] == "codex", "never assert an engine nobody observed"
    assert row["running"] is True
    assert row["hold"] == "terminal", "a detached pane is not a headless worker"


def test_every_running_row_says_running(ws, monkeypatch):
    loc, pd = ws
    _mk(pd, "aaaa0001")
    _windows(monkeypatch, attached=set())
    _reg(monkeypatch)
    _procs(monkeypatch, [
        {"pid": 10, "session_id": "aaaa0001", "location": loc, "source": "cli",
         "tty": "/dev/ttys002", "uptime_minutes": 5},
        _raw(11, loc),
    ])

    rows = {r["session_id"]: r for r in board.active_sessions()}
    assert rows["aaaa0001"]["running"] is True
    assert rows["pid-11"]["running"] is True
    assert rows["pid-11"]["hold"] == "headless", "no terminal anywhere"


def test_a_managed_pane_names_its_own_codex_process(ws, monkeypatch):
    """A Codex session's only identity. Without it the pane's process reads as
    anonymous and the board carries TWO rows for one session — the registry's
    and a pid- row beside it."""
    loc, _ = ws
    _windows(monkeypatch, attached=set())
    sid = "aaaa0001-1111-2222-3333-444444444444"
    _reg(monkeypatch, {sid: {"agent": "nova", "engine": "codex",
                             "name": "", "transcript": ""}})
    _procs(monkeypatch, [_raw(13, loc) | {"tty": "/dev/ttys002",
                                          "engine": "codex"}])

    sessions, orphans = board._proc_scan()
    assert list(sessions) == [sid], "the pane named it"
    assert orphans == [], "and so it is not also an anonymous row"

    rows = {r["session_id"]: r for r in board.active_sessions()}
    assert len(rows) == 1
    assert rows[sid]["engine"] == "codex" and rows[sid]["running"] is True


def test_pids_holding_resolves_a_codex_pane(ws, monkeypatch):
    """The kill path must agree. A Codex sid matches no argv and correlates to
    no transcript, so without pane identity every caller asking who holds it
    got [] — a kill that signalled nothing and reported success."""
    loc, _ = ws
    _windows(monkeypatch, attached=set())
    sid = "aaaa0001-1111-2222-3333-444444444444"
    _reg(monkeypatch, {sid: {"agent": "nova", "engine": "codex"}})
    _procs(monkeypatch, [_raw(13, loc) | {"tty": "/dev/ttys002",
                                          "engine": "codex"}])

    assert board.pids_holding(sid) == [13]


# ── Correlation may only name what the process could have written ─────────
#
# The user's report: a blank card in Active, no agent, no messages, 188k ctx —
# the 01:30 social-control cron round, which had exited at 01:57. The comment
# above ("Codex ... can NEVER be named that way") was the wrong half of the
# truth. `correlate_raw` does not ask whether THIS process wrote the file; it
# pairs any sid-less process with the nearest unowned JSONL in the workspace.
# Codex carries no sid in argv, so it landed in that pool and adopted a dead
# cron transcript — and because `by_pid` outranks pane identity, the Codex
# that actually held the pane went to running=False in the same stroke. One
# bad pair, two lying rows.

def _mk_entry(pd, sid, entrypoint="cli"):
    """A transcript that states who wrote it, the way Claude Code does."""
    f = pd / f"{sid}.jsonl"
    f.write_text(json.dumps({"type": "summary", "sessionId": sid}) + "\n"
                 + json.dumps({"type": "user", "sessionId": sid,
                               "entrypoint": entrypoint}) + "\n")
    return f


def test_codex_never_adopts_a_claude_transcript(ws, monkeypatch):
    """THE bug. A Codex writes no Claude transcript, so it can own none."""
    loc, pd = ws
    _mk(pd, "dead0001")
    _windows(monkeypatch, attached=set())
    _reg(monkeypatch)
    _procs(monkeypatch, [_raw(13, loc) | {"tty": "/dev/ttys003",
                                          "engine": "codex"}])

    assert board.correlate_raw(board._claude_procs(), time.time()) == {}
    sessions, orphans = board._proc_scan()
    assert sessions == {}, "a codex named as a claude session that isn't it"
    assert [o["pid"] for o in orphans] == [13], "still shown, just honestly"


def test_the_pane_keeps_its_codex_when_a_stray_transcript_sits_beside_it(
        ws, monkeypatch):
    """The other half of the same bug: the bad pair also cost the real session
    its identity, so the one process actually running reported running=False."""
    loc, pd = ws
    _mk(pd, "dead0001")
    _windows(monkeypatch, attached=set())
    sid = "aaaa0001-1111-2222-3333-444444444444"
    _reg(monkeypatch, {sid: {"agent": "nova", "engine": "codex"}})
    _procs(monkeypatch, [_raw(13, loc) | {"tty": "/dev/ttys002",
                                          "engine": "codex"}])

    sessions, _ = board._proc_scan()
    assert list(sessions) == [sid], "pane identity, not a transcript guess"
    rows = {r["session_id"]: r for r in board.active_sessions()}
    assert rows[sid]["running"] is True
    assert "dead0001" not in rows, "the exited session is nobody's row"


def test_a_headless_transcript_is_never_adopted_by_a_live_claude(
        ws, monkeypatch):
    """Same shape, claude-on-claude — what the engine filter alone misses. A
    cron round names its sid in argv, so a transcript of its in the candidate
    pool means its process has exited. The raw claude beside it is not it."""
    loc, pd = ws
    _mk_entry(pd, "cron0001", entrypoint="sdk-cli")
    _windows(monkeypatch, attached=set())
    _reg(monkeypatch)
    _procs(monkeypatch, [_raw(14, loc) | {"tty": "/dev/ttys002"}])

    assert board.correlate_raw(board._claude_procs(), time.time()) == {}
    sessions, orphans = board._proc_scan()
    assert sessions == {}
    assert [o["pid"] for o in orphans] == [14]


def test_an_interactive_transcript_is_still_correlated(ws, monkeypatch):
    """The guard must not cost the case correlation exists for."""
    loc, pd = ws
    _mk_entry(pd, "live0001", entrypoint="cli")
    _windows(monkeypatch, attached=set())
    _reg(monkeypatch)
    _procs(monkeypatch, [_raw(15, loc) | {"tty": "/dev/ttys002"}])

    sessions, orphans = board._proc_scan()
    assert list(sessions) == ["live0001"], "a raw claude still gets named"
    assert orphans == []


def test_an_unreadable_transcript_stays_eligible(ws, monkeypatch):
    """A file we cannot look at is not evidence of anything. Excluding it on a
    failed read would drop a live session's row on an I/O blink."""
    loc, pd = ws
    f = _mk(pd, "blind001")          # no entrypoint line at all
    assert board._headless_transcript(f) is False
    _windows(monkeypatch, attached=set())
    _reg(monkeypatch)
    _procs(monkeypatch, [_raw(16, loc) | {"tty": "/dev/ttys002"}])

    sessions, _ = board._proc_scan()
    assert list(sessions) == ["blind001"]


def test_an_unregistered_codex_is_a_terminal_row_not_a_managed_one(
        ws, monkeypatch):
    """A Codex nobody spawned through jRemote has no pane to name it, so it is
    an honest orphan — running and shown, but never claiming a phone-drivable
    hold it has no registry entry for. Section placement keys on `hold`, and
    'managed' is what parked the phantom card in Active."""
    loc, pd = ws
    _mk(pd, "dead0001")             # the stray transcript it must not adopt
    _windows(monkeypatch, attached=set())
    _reg(monkeypatch)
    _procs(monkeypatch, [_raw(17, loc) | {"tty": "/dev/ttys002",
                                          "engine": "codex"}])

    rows = {r["session_id"]: r for r in board.active_sessions()}
    assert list(rows) == ["pid-17"]
    assert rows["pid-17"]["managed"] is False
    assert rows["pid-17"]["hold"] == "terminal"
    assert rows["pid-17"]["running"] is True


def test_a_pane_that_closes_mid_scan_does_not_leave_a_managed_row(
        ws, monkeypatch):
    """`_pane_sids` and `active_sessions` read the registry separately, so a
    session that ends between the two reads puts a sid in `procs` that `reg`
    no longer has. `managed: True` was hardcoded on codex rows, so that race
    published a card claiming a tmux nobody could observe — exactly the shape
    the user saw. The row may be wrong about being alive; it must not be wrong
    about what holds it."""
    from jstack_host import managed
    loc, _ = ws
    sid = "aaaa0001-1111-2222-3333-444444444444"
    entry = {sid: {"agent": "nova", "engine": "codex"}}
    reads = []

    def registry():
        reads.append(1)
        return entry if len(reads) == 1 else {}   # closed after the scan

    monkeypatch.setattr(managed, "open_registry", registry)
    monkeypatch.setattr(managed, "attached_names", lambda: set())
    _windows(monkeypatch, attached=set())
    _procs(monkeypatch, [_raw(18, loc) | {"tty": "/dev/ttys002",
                                          "engine": "codex"}])

    rows = {r["session_id"]: r for r in board.active_sessions()}
    assert rows[sid]["managed"] is False, "a tmux nobody observed"
    assert rows[sid]["hold"] == "terminal", "it has a tty, not a pane"


# ── the turn clock is three-valued, because it can go unread ────────────────
#
# `turn_open` was a bool, and its False said two different things: the clock
# was read and the turn is closed, or nobody read a clock at all. Every reader
# had to guess which — the app's dot guessed "closed" and drew grey over a
# session that was mid-turn, the notify engine guessed "closed" and would have
# pushed a done for it. "" is the third answer, and it is the honest one.

def test_an_unmanaged_row_never_claims_a_turn_verdict(ws, monkeypatch):
    """A raw desk window's turn clock is never read — the tail read is scoped
    to managed rows because it costs. The row must SAY that, not report idle."""
    from jstack_host import managed
    loc, pd = ws
    _mk(pd, "cafe0001")
    monkeypatch.setattr(managed, "open_registry", lambda: {})
    monkeypatch.setattr(managed, "attached_names", lambda: set())
    monkeypatch.setattr(board, "get_session_summary",
                        lambda f: {"total_tokens": 1, "last_context": 1})
    _windows(monkeypatch, attached=set())
    _procs(monkeypatch, [{"pid": 21, "session_id": "cafe0001", "location": loc,
                          "source": "cli", "uptime_minutes": 1,
                          "tty": "/dev/ttys002"}])

    row = {r["session_id"]: r for r in board.active_sessions()}["cafe0001"]
    assert row["managed"] is False
    assert row["turn"] == "", "a row nobody read a clock for claimed a verdict"


def test_a_codex_row_never_claims_a_turn_verdict(ws, monkeypatch):
    """A Codex pane writes no Claude transcript, so there is no turn clock to
    read here at all. Asserting "idle" would hand the app a resting session
    that is mid-turn — and the app's dot would go grey on live work."""
    from jstack_host import managed
    loc, _ = ws
    sid = "bbbb0002-1111-2222-3333-444444444444"
    monkeypatch.setattr(managed, "open_registry",
                        lambda: {sid: {"agent": "nova", "engine": "codex"}})
    monkeypatch.setattr(managed, "attached_names", lambda: set())
    _windows(monkeypatch, attached=set())
    _procs(monkeypatch, [_raw(22, loc) | {"tty": "/dev/ttys002",
                                          "engine": "codex"}])

    rows = {r["session_id"]: r for r in board.active_sessions()}
    assert rows[sid]["engine"] == "codex"
    assert rows[sid]["turn"] == ""


# ── Tags on the card ────────────────────────────────────────────────────────
#
# The timeline's third axis, read onto every board row. What is guarded here is
# the read contract, not the vocabulary: an unreachable or absent store must
# cost the tags and never the board, because a card that fails to render is a
# session the user cannot reach from their phone.

def _tag_db(tmp_path, rows=(), broken=False):
    """A timeline store holding `rows` of (session_id, tag)."""
    import sqlite3
    d = tmp_path / "Timeline"
    d.mkdir(exist_ok=True)
    db = d / "timeline.db"
    if broken:
        db.write_bytes(b"this is not a database")
        return d
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE tags (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "name TEXT NOT NULL UNIQUE, description TEXT NOT NULL DEFAULT '', "
                "created_at TEXT NOT NULL)")
    con.execute("CREATE TABLE session_tags (session_id TEXT NOT NULL, "
                "tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE, "
                "created_at TEXT NOT NULL, PRIMARY KEY (session_id, tag_id))")
    for sid, tag in rows:
        con.execute("INSERT OR IGNORE INTO tags (name, created_at) VALUES (?,'t')",
                    (tag,))
        tid = con.execute("SELECT id FROM tags WHERE name=?", (tag,)).fetchone()[0]
        con.execute("INSERT OR IGNORE INTO session_tags VALUES (?,?,'t')", (sid, tid))
    con.commit()
    con.close()
    return d


def test_session_tags_reads_the_relation(tmp_path, monkeypatch):
    d = _tag_db(tmp_path, [("sid-a", "infra"), ("sid-b", "issue")])
    monkeypatch.setenv("JSTACK_TIMELINE_DIR", str(d))
    assert board._session_tags() == {"sid-a": ["infra"], "sid-b": ["issue"]}


def test_a_session_can_carry_several_tags(tmp_path, monkeypatch):
    d = _tag_db(tmp_path, [("sid-a", "issue"), ("sid-a", "infra")])
    monkeypatch.setenv("JSTACK_TIMELINE_DIR", str(d))
    # Sorted by name, so a card's chips do not reshuffle between polls.
    assert board._session_tags() == {"sid-a": ["infra", "issue"]}


def test_untagged_sessions_are_absent_not_empty(tmp_path, monkeypatch):
    """The row builders default to [] themselves; the reader reports only what
    the store actually holds, so "no tag" never arrives as a claim."""
    d = _tag_db(tmp_path, [("sid-a", "infra")])
    monkeypatch.setenv("JSTACK_TIMELINE_DIR", str(d))
    assert "sid-b" not in board._session_tags()


def test_a_missing_store_costs_the_tags_not_the_board(tmp_path, monkeypatch):
    monkeypatch.setenv("JSTACK_TIMELINE_DIR", str(tmp_path / "nowhere"))
    assert board._session_tags() == {}


def test_a_corrupt_store_costs_the_tags_not_the_board(tmp_path, monkeypatch):
    d = _tag_db(tmp_path, broken=True)
    monkeypatch.setenv("JSTACK_TIMELINE_DIR", str(d))
    assert board._session_tags() == {}


def test_the_read_is_read_only(tmp_path, monkeypatch):
    """One writer for the store. A board build must not be able to change it —
    a second writer here would fork the timeline's source of truth."""
    import sqlite3
    d = _tag_db(tmp_path, [("sid-a", "infra")])
    monkeypatch.setenv("JSTACK_TIMELINE_DIR", str(d))
    board._session_tags()
    con = sqlite3.connect(f"file:{d / 'timeline.db'}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO tags (name, created_at) VALUES ('x','t')")
    con.close()


def test_every_board_builder_carries_the_field(tmp_path, monkeypatch):
    """`tags` is on all three payloads. A card that shows tags on the board and
    loses them in history would read as the session having been retagged."""
    monkeypatch.setenv("JSTACK_TIMELINE_DIR", str(tmp_path / "nowhere"))
    for rows in (board.active_sessions(), board.open_sessions(),
                 board.list_sessions()):
        for r in rows:
            assert isinstance(r.get("tags"), list), r.get("session_id")


def test_the_history_route_carries_tags_too(tmp_path, monkeypatch):
    """The board builders are not the app's summary source — `/sessions/history`
    is, and it answers from the session index, which has no tag column. The
    device's upsert writes every host-owned field it receives, so a payload
    that omits `tags` does not read as "unchanged": it lands as [] and wipes
    the chips the board sync just set. Assert the route, not the builder.
    """
    from fastapi.testclient import TestClient

    from jstack_host.server import create_app
    app = create_app()
    from jstack_host import auth, store

    d = _tag_db(tmp_path, [("sid-a", "infra"), ("sid-a", "jremote")])
    monkeypatch.setenv("JSTACK_TIMELINE_DIR", str(d))
    monkeypatch.setattr(auth, "_expected_token", lambda: "test-token")
    monkeypatch.setattr(
        store, "get_store",
        lambda: type("S", (), {"query_sessions": lambda self, **kw: [
            {"session_id": "sid-a"}, {"session_id": "sid-untagged"}]})())

    c = TestClient(app)
    c.headers.update({"Authorization": "Bearer test-token"})
    rows = c.get("/api/jremote/v1/sessions/history").json()["sessions"]
    by_sid = {r["session_id"]: r["tags"] for r in rows}
    assert by_sid == {"sid-a": ["infra", "jremote"], "sid-untagged": []}


# ── The spawn's own title and model, read out of its argv ────────────────────
#
# A headless worker (`claude --print`) has no tmux, and `managed.open_registry`
# is gated on one — so for the whole of its run the registry has nothing to say
# about it. Everything the board can know about a worker beyond its pid is what
# the process states in its own command line: `--name` and `--model`. Both used
# to be read and then dropped on the floor for any session `_proc_scan` could
# identify, which is every worker (its sid is in its argv, so it is never an
# orphan). Named workers rendered anonymous, with a blank model.


def test_the_scan_carries_the_argv_title_onto_the_session_row(ws, monkeypatch):
    loc, pd = ws
    _mk(pd, "bbbb0001")
    _windows(monkeypatch, attached=set())
    _procs(monkeypatch, [
        {"pid": 10, "session_id": "bbbb0001", "location": loc, "source": "cli-pipe",
         "tty": None, "uptime_minutes": 1, "window_name": "nova - #6",
         "model": "opus"},
    ])
    sessions, orphans = board._proc_scan()
    assert sessions["bbbb0001"]["name"] == "nova - #6"
    assert sessions["bbbb0001"]["model"] == "opus"
    assert orphans == [], "an identified worker must never also be an orphan"


def test_a_resume_that_omits_the_title_does_not_blank_it(ws, monkeypatch):
    """First non-empty wins. A session is resumed by a second process that
    states `--resume <sid>` and nothing else; last-wins would erase the title
    the still-running first process is displaying."""
    loc, pd = ws
    _mk(pd, "bbbb0002")
    _windows(monkeypatch, attached=set())
    _procs(monkeypatch, [
        {"pid": 10, "session_id": "bbbb0002", "location": loc, "source": "cli-pipe",
         "tty": None, "uptime_minutes": 9, "window_name": "nova - #6",
         "model": "opus"},
        {"pid": 11, "session_id": "bbbb0002", "location": loc, "source": "cli-pipe",
         "tty": None, "uptime_minutes": 1, "window_name": "", "model": ""},
    ])
    sessions, _ = board._proc_scan()
    assert sessions["bbbb0002"]["name"] == "nova - #6"
    assert sessions["bbbb0002"]["model"] == "opus"


def test_a_worker_row_shows_its_argv_title_with_no_registry(ws, monkeypatch):
    """The regression, at the surface the user actually looks at. `open_registry`
    is emptied here on purpose — that is exactly what it answers for a headless
    run, and the title still has to arrive."""
    from jstack_host import managed
    loc, pd = ws
    _mk(pd, "bbbb0003")
    _windows(monkeypatch, attached=set())
    monkeypatch.setattr(managed, "open_registry", lambda: {})
    _procs(monkeypatch, [
        {"pid": 10, "session_id": "bbbb0003", "location": loc, "source": "cli-pipe",
         "tty": None, "uptime_minutes": 1, "window_name": "nova - #6",
         "model": "opus"},
    ])
    row = next(r for r in board.active_sessions() if r["session_id"] == "bbbb0003")
    assert row["window_name"] == "nova - #6"
    assert row["model"] == "opus"
    assert row["hold"] == "headless", "the shape this had to work for"


def test_codex_card_shows_transcript_facts_after_reattach(ws, monkeypatch, tmp_path):
    import json
    from jstack_host import managed
    loc, _ = ws
    sid = 'aaaa0001-1111-2222-3333-444444444444'
    path = tmp_path / 'rollout-card.jsonl'
    records = [
        {'type': 'session_meta', 'payload': {'session_id': sid, 'cwd': loc}},
        {'type': 'turn_context', 'payload': {'model': 'gpt-6-astra'}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
            'content': [{'type': 'input_text', 'text': 'Validate the local stack'}]}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
            'content': [{'type': 'output_text', 'text': 'Checking the setup'}]}},
        {'type': 'event_msg', 'payload': {'type': 'task_started'}},
        {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
            'last_token_usage': {'input_tokens': 2500},
            'total_token_usage': {'total_tokens': 5000}}}},
    ]
    path.write_text(''.join(json.dumps(r) + '\n' for r in records))
    managed.record_open(sid, 'nova', engine='codex', model='gpt-5.6-sol')
    managed.record_transcript(sid, str(path))
    managed.record_open(sid, 'nova')
    _windows(monkeypatch, attached=set())
    _reg(monkeypatch, managed._reg_load())
    _procs(monkeypatch, [_raw(33, loc) | {'tty': '/dev/ttys002', 'engine': 'codex'}])
    row = next(r for r in board.active_sessions() if r['session_id'] == sid)
    assert row['preview'] == 'Validate the local stack'
    assert row['last_reply'] == 'Checking the setup'
    assert (row['last_context'], row['tokens']) == (2500, 5000)
    assert row['model'] == 'gpt-6-astra' and row['turn'] == 'working'


def test_codex_board_uses_native_title(ws, monkeypatch, tmp_path):
    from jstack_host import managed, codex_transcript, messages
    loc, _ = ws
    sid = "bbbb0002-1111-2222-3333-444444444444"
    rollout = tmp_path / "rollout-native.jsonl"
    rollout.write_text('{}\n')
    monkeypatch.setattr(managed, "open_registry", lambda: {
        sid: {"agent": "nova", "engine": "codex", "transcript": str(rollout)}})
    monkeypatch.setattr(managed, "attached_names", lambda: set())
    monkeypatch.setattr(codex_transcript, "summary", lambda p: {"title": "Repair session titles"})
    monkeypatch.setattr(messages, "parse_session", lambda sid: {"messages": [
        {"role": "user", "text": "initial input"}]})
    _windows(monkeypatch, attached=set())
    _procs(monkeypatch, [_raw(22, loc) | {"tty": "/dev/ttys002", "engine": "codex"}])
    row = next(r for r in board.active_sessions() if r["session_id"] == sid)
    assert row["preview"] == "Repair session titles"
    assert row["last_prompt"] == "initial input"
