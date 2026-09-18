"""The compose lift — the CLI hands over its input buffer, whole.

What these pin is the one property the screen-reading version could not have:
a lift either returns the entire buffer or reports that it did not happen, and
a report that it did not happen means nothing in the CLI was touched.
"""

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from jstack_host import composer, managed

# Asked of the package, never recomputed here — see `managed.compose_shim`.
SHIM = managed.compose_shim()


def test_managed_sessions_are_spawned_with_the_shim_as_visual():
    """VISUAL, not EDITOR — a machine's settings.json may set EDITOR for the
    user's own ctrl+G, and the CLI resolves VISUAL first, so the managed pane
    gets the shim without taking their desk editor away."""
    exports = managed._compose_exports("sid-1234")
    assert "export VISUAL=" in exports
    assert str(SHIM) in exports
    assert "export JREMOTE_SID=" in exports
    assert "export JREMOTE_COMPOSE_DIR=" in exports
    assert "EDITOR=" not in exports.replace("JREMOTE_EDITOR", "")


def test_the_shim_only_parks_the_agents_prompt_file(tmp_path):
    """VISUAL is inherited by everything else in that pane. A `git commit`
    there must reach a real editor, not block forever on a host that is not
    watching for it."""
    other = tmp_path / "COMMIT_EDITMSG"
    other.write_text("subject")
    r = subprocess.run([str(SHIM), str(other)],
                       env={**os.environ,
                            "JREMOTE_EDITOR_FALLBACK": "/bin/echo",
                            "JREMOTE_COMPOSE_DIR": str(tmp_path / "park")},
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 0
    assert str(other) in r.stdout          # the fallback ran, on that file
    assert not (tmp_path / "park").exists()  # nothing parked


def test_the_shim_parks_the_path_and_waits_to_be_released(tmp_path):
    prompt = tmp_path / "claude-prompt-abc.md"
    prompt.write_text("every word, including the ones off screen")
    park_dir = tmp_path / "park"
    env = {**os.environ, "JREMOTE_COMPOSE_DIR": str(park_dir),
           "JREMOTE_SID": "sid-1234"}
    proc = subprocess.Popen([str(SHIM), str(prompt)], env=env,
                            stdout=subprocess.DEVNULL)
    try:
        park = park_dir / "sid-1234.park"
        for _ in range(100):
            if park.exists():
                break
            time.sleep(0.05)
        assert park.exists(), "the shim never parked"
        assert park.read_text().strip() == str(prompt)
        assert proc.poll() is None, "the shim exited without waiting"

        (park_dir / "sid-1234.release").touch()
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    # Its leavings go with it, so the next lift can't read a stale answer.
    assert not park.exists()
    assert not (park_dir / "sid-1234.release").exists()


def reference_block(*lines: str) -> str:
    """The CLI's own preamble, byte-for-byte (`chat:externalEditor`).

    Its last response goes above the buffer, commented out, and comes back off
    on save — so a lift that hands the file over whole is handing over words
    the user never typed.

    Copied off a real handoff, not reconstructed: 2.1.220 puts a blank line
    between the marker and the buffer, and reading that blank as the user's
    was this fix's own first miss.
    """
    body = "\n".join(f"# {ln}" if ln else "#" for ln in lines)
    return ("# ─── Claude's last response (for reference; "
            "removed on save) ───\n" + body + "\n"
            "# ─── Write your reply below this line "
            + "─" * 26 + "\n\n")


def test_the_clis_commented_last_response_is_not_part_of_the_buffer():
    """What was reported: compose opening onto a wall of `#` lines nobody
    typed, and Send posting them back as a prompt (2026-09-09)."""
    typed = "and the carousel renderer is still wrong"
    text = composer._without_reference_block(
        reference_block("Right on both counts.", "", "No hub on the laptop.") + typed)
    assert text == typed


def test_an_empty_box_under_the_block_lifts_as_empty():
    """The common case — nothing typed yet. The sheet has to open blank, not
    holding a copy of the last answer for editing."""
    assert composer._without_reference_block(reference_block("DONE")) == ""


def test_a_buffer_that_quotes_the_block_keeps_every_word():
    """The header is at position 0 or it is not a header. Pasting one *into*
    the box — which is exactly what this bug taught the box to hold — must
    survive the lift, because a strip that hunts anywhere in the file would
    eat the words in front of it."""
    quoted = "why does this keep happening:\n" + reference_block("DONE")
    assert composer._without_reference_block(quoted) == quoted


def test_a_file_with_no_block_is_returned_untouched():
    """A first message has no last response, so the CLI writes the bare
    buffer. Nothing to strip, and nothing that may be stripped anyway."""
    plain = "# a markdown heading the user typed\nand the rest of it"
    assert composer._without_reference_block(plain) == plain


def test_a_lift_that_cannot_happen_says_so_and_touches_nothing(monkeypatch):
    """The refusal is the safe answer, and it has to stay reachable: an
    unmanaged session gets `lifted: false` rather than a ctrl+G fired blind
    into whatever is on screen."""
    monkeypatch.setattr(managed, "is_open", lambda sid: False)
    sent = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: sent.append(a) or pytest.fail("keys sent"))
    out = composer.lift("no-such-session")
    assert out["lifted"] is False
    assert out["reason"]
    assert not sent


def test_a_session_without_the_shim_is_never_sent_the_key(monkeypatch):
    """ctrl+G is not safe to press on spec. Without the shim the agent runs
    whatever $EDITOR resolves to — `cot -w` on this Mac — which puts a window
    on the desk and blocks the CLI inside it until a hand closes it."""
    monkeypatch.setattr(managed, "is_open", lambda sid: True)
    monkeypatch.setattr(managed, "open_names", lambda: {managed._name("sid-1234")})
    monkeypatch.setattr(composer, "_pane_has_shim", lambda name: False)
    sent = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: sent.append(a) or pytest.fail("keys sent"))
    out = composer.lift("sid-1234")
    assert out["lifted"] is False
    assert "restart" in out["reason"]
    assert not sent


def stand_in_for_the_shim(tmp_path, monkeypatch, prompt: Path) -> None:
    """A managed session whose ctrl+G is answered, without a CLI in the room.

    The pane's key produces a shim that parks the path and waits for release,
    exactly as `bin/jremote-compose-editor` does against a real one.
    """
    monkeypatch.setattr(composer, "COMPOSE_DIR", tmp_path / "park")
    monkeypatch.setattr(managed, "is_open", lambda sid: True)
    monkeypatch.setattr(managed, "open_names", lambda: {managed._name("sid-1234")})
    monkeypatch.setattr(composer, "_pane_has_shim", lambda name: True)

    def fake_run(argv, **kw):
        park = tmp_path / "park" / "sid-1234.park"
        ready = threading.Event()

        def shim():
            park.write_text(str(prompt) + "\n")
            ready.set()
            deadline = time.monotonic() + composer.PARK_TIMEOUT + composer.RELEASE_TIMEOUT + 5
            while time.monotonic() < deadline:
                if (tmp_path / "park" / "sid-1234.release").exists():
                    park.unlink()
                    return
                time.sleep(0.01)
        threading.Thread(target=shim, daemon=True).start()
        assert ready.wait(timeout=10), "fixture shim failed to park"
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_a_lift_returns_the_whole_file_and_leaves_the_replacement(tmp_path, monkeypatch):
    """The end-to-end shape with the shim stood in for: whatever the CLI wrote
    comes back entire, and the box is left holding exactly what was asked."""
    prompt = tmp_path / "claude-prompt-xyz.md"
    whole = " ".join(f"word{i:03d}" for i in range(1, 201))
    prompt.write_text(whole)
    stand_in_for_the_shim(tmp_path, monkeypatch, prompt)

    out = composer.lift("sid-1234", replacement="")
    assert out["lifted"] is True
    assert out["text"] == whole          # all 200 words, not the visible tail
    assert prompt.read_text() == ""      # the box is emptied, once


def test_a_lift_hands_over_the_buffer_and_not_the_clis_own_preamble(tmp_path, monkeypatch):
    """The same shape against a file with a last response above it — the state
    every message after the first is composed in."""
    prompt = tmp_path / "claude-prompt-xyz.md"
    typed = "one more thing before you push"
    prompt.write_text(reference_block("Right on both counts.", "", "Build 45 is out.")
                      + typed)
    stand_in_for_the_shim(tmp_path, monkeypatch, prompt)

    out = composer.lift("sid-1234", replacement="")
    assert out["lifted"] is True
    assert out["text"] == typed
    assert prompt.read_text() == ""
