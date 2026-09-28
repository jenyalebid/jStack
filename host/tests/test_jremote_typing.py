"""What jRemote types into a managed session's input box.

Everything the user sends from the phone that isn't a live keystroke goes through
`_type_argv` — the share sheet's first message, the takeover's continue nudge,
`POST /sessions/{sid}/input`. A single `send-keys -l "$text"` loses two things
silently, and both were live: a newline is read as nothing (a share-sheet
comment and the path under it arrived as one glued word), and a line starting
with `-` is parsed as a tmux flag, so the send is dropped whole.

The M-Enter/`--` behaviour behind these expectations was verified against both
real CLIs in a throwaway tmux server, transcript read back — see the docstring
on `_type_argv`. What is asserted here is that we still emit that shape.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jstack_host.managed import _READY, _type_argv  # noqa: E402


def keys(text, name="s"):
    """Just the tmux arguments, with the socket boilerplate dropped."""
    return [a[3:] for a in _type_argv(name, text)]


def test_single_line_is_one_literal_send():
    assert keys("hello there") == [
        ["send-keys", "-t", "s", "-l", "--", "hello there"]]


def test_newline_becomes_the_insert_newline_key():
    # Never a bare Enter: that would submit the half-typed message.
    assert keys("one\ntwo") == [
        ["send-keys", "-t", "s", "-l", "--", "one"],
        ["send-keys", "-t", "s", "M-Enter"],
        ["send-keys", "-t", "s", "-l", "--", "two"],
    ]


def test_blank_line_survives_as_its_own_newline():
    # The share sheet's shape: The user's comment, a blank line, the file path.
    assert keys("look at this\n\n/tmp/a.png") == [
        ["send-keys", "-t", "s", "-l", "--", "look at this"],
        ["send-keys", "-t", "s", "M-Enter"],
        ["send-keys", "-t", "s", "M-Enter"],
        ["send-keys", "-t", "s", "-l", "--", "/tmp/a.png"],
    ]


def test_leading_dash_is_typed_not_parsed_as_a_flag():
    # Without `--` tmux answers "unknown flag -d" and types nothing at all.
    for line in ("- a bullet", "-d", "--force"):
        assert keys(line) == [["send-keys", "-t", "s", "-l", "--", line]]


def test_carriage_returns_are_normalised():
    assert keys("a\r\nb\rc") == keys("a\nb\nc")


def test_trailing_newline_leaves_the_cursor_on_a_new_line():
    assert keys("a\n") == [
        ["send-keys", "-t", "s", "-l", "--", "a"],
        ["send-keys", "-t", "s", "M-Enter"],
    ]


def test_empty_text_types_nothing():
    # The watcher splices these into a bash script; an empty splice is a
    # syntax error, so the nudge must bail before building one.
    assert keys("") == []


def test_every_engine_has_a_readiness_marker_and_a_blocker():
    # The nudge waits on the marker and refuses while the blocker is up; an
    # engine missing from here would type into whatever is on screen.
    from jstack_host import engines
    for e in engines.ENGINES:
        marker, blocker = _READY[e["id"]]
        assert marker and blocker and marker != blocker


# ── Typing after a compaction ───────────────────────────────────────────────
#
# `send_input_after_compact` is the delayed half of the same idea: type this,
# but not until the session has freed the window to read it in. It watches the
# transcript rather than the screen, because the footer that proves the TUI is
# up is drawn mid-turn too — it says the CLI is alive and nothing about what it
# is doing. These run the real detached watcher against a real file.

import json  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402

from jstack_host import managed  # noqa: E402

BOUNDARY = json.dumps({"type": "system", "subtype": "compact_boundary",
                       "compactMetadata": {"preTokens": 150000,
                                           "postTokens": 20000}})


@pytest.fixture
def watcher(tmp_path, monkeypatch):
    """Arms the real watcher with a harmless 'keystroke': touch a file.

    Nothing here reaches tmux. What is under test is *when* the argv fire, so
    the argv are the cheapest observable thing that proves they did."""
    typed = tmp_path / "typed"
    monkeypatch.setattr(managed, "is_open", lambda sid: True)
    monkeypatch.setattr(managed, "_type_argv",
                        lambda name, text: [["/usr/bin/touch", str(typed)]])
    monkeypatch.setattr(managed, "_t", lambda *a: ["/usr/bin/true"])

    def fired(within=12.0):
        end = time.time() + within
        while time.time() < end:
            if typed.exists():
                return True
            time.sleep(0.2)
        return False

    return fired


def test_a_boundary_already_on_file_is_not_the_one_it_waits_for(tmp_path, watcher):
    """The session's earlier compactions are the trap. Counting them first is
    the only way to tell this compaction from the ones before it — without it
    the message types instantly, into the window that was never freed."""
    t = tmp_path / "s.jsonl"
    t.write_text(BOUNDARY + "\n")
    managed.send_input_after_compact("sid", "hello", str(t), timeout=6)
    assert not watcher(within=6), "typed on a stale boundary"


def test_a_new_boundary_releases_the_message(tmp_path, watcher):
    t = tmp_path / "s.jsonl"
    t.write_text(BOUNDARY + "\n")
    managed.send_input_after_compact("sid", "hello", str(t), timeout=30)
    time.sleep(3)
    with open(t, "a") as f:
        f.write(BOUNDARY + "\n")
    assert watcher(), "a new boundary landed and the message never followed"


def test_the_message_arrives_even_if_the_compaction_never_does(tmp_path, watcher):
    """Bounded on purpose. A comment from the user that never arrives is a worse
    failure than one that arrives in a heavy window — on timeout it types, and
    that is exactly the behaviour this path had before compaction was in it."""
    t = tmp_path / "s.jsonl"
    t.write_text("")
    managed.send_input_after_compact("sid", "hello", str(t), timeout=3)
    assert watcher(within=15)


def test_a_closed_session_is_never_armed(tmp_path, monkeypatch):
    monkeypatch.setattr(managed, "is_open", lambda sid: False)
    assert managed.send_input_after_compact("s", "hi", str(tmp_path / "x")) is False


def test_empty_text_is_never_armed(tmp_path, monkeypatch):
    monkeypatch.setattr(managed, "is_open", lambda sid: True)
    assert managed.send_input_after_compact("s", "", str(tmp_path / "x")) is False


# ── `send_input`: an Enter is not a submission (#157) ───────────────────────
#
# The route behind `POST /sessions/{sid}/input` used to report success off the
# Enter alone — a busy pane HOLDS a send instead of running it, and the box
# then shows a dim placeholder that reads on screen exactly like a line that
# already ran. `send_input` now goes through `compact_delivery.submit`, the
# same type-then-verify already trusted for `/compact` and the continue
# nudge, so a send that never took is wiped back out and logged rather than
# left drawn in the box as a lie.

def test_send_input_reports_a_send_that_never_took(monkeypatch, capsys):
    from jstack_host import compact_delivery as cod

    monkeypatch.setattr(managed, "is_open", lambda sid: True)
    monkeypatch.setattr(managed, "open_registry",
                        lambda: {"aaaaaaaa": {"engine": "claude"}})
    monkeypatch.setattr(cod, "SUBMIT_CHECK_SECS", 0.2)
    monkeypatch.setattr(cod, "POLL_SECS", 0.05)
    monkeypatch.setattr(cod, "send_text", lambda name, text, delay=0.0: None)
    # A pane still showing exactly what we typed, the whole window: stuck.
    monkeypatch.setattr(cod, "pane",
                        lambda name: "❯ do the thing\n\n  bypass permissions on")
    wiped = []
    monkeypatch.setattr(cod, "clear_line", wiped.append)

    assert managed.send_input("aaaaaaaa", "do the thing") is False
    assert wiped == ["jr-aaaaaaaa"]
    assert "did not take" in capsys.readouterr().err


def test_send_input_reports_success_once_the_box_clears(monkeypatch):
    from jstack_host import compact_delivery as cod

    monkeypatch.setattr(managed, "is_open", lambda sid: True)
    monkeypatch.setattr(managed, "open_registry",
                        lambda: {"aaaaaaaa": {"engine": "claude"}})
    monkeypatch.setattr(cod, "SUBMIT_CHECK_SECS", 0.2)
    monkeypatch.setattr(cod, "POLL_SECS", 0.05)
    monkeypatch.setattr(cod, "send_text", lambda name, text, delay=0.0: None)
    # The box no longer holds our text: the CLI took it and cleared the line.
    monkeypatch.setattr(cod, "pane", lambda name: "❯ \n\n  bypass permissions on")

    assert managed.send_input("aaaaaaaa", "do the thing") is True
