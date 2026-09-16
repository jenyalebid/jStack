"""The attachment registry — who is showing each session, and who drove it.

Every thread view holds a PTY WebSocket (pty.py); the registry ties that
live connection to an app instance. Two answers only it can give:

* `driver_for(sid)` — the attachment whose keystrokes arrived most recently,
  because a handoff typed on the iPad must open its window on the iPad, not
  the Mac. A driver that disconnected mid-turn is still remembered
  (`recent_driver`) so a device-driven spawn never falls back to a Mac
  window just because iOS dropped the socket.

* `close_others(sid, instance)` — order every *other* instance's view of the
  thread shut (code 4412: the view dismisses, the session lives).
"""

import pytest

from jstack_host import attach


@pytest.fixture(autouse=True)
def clean_registry():
    attach.reset()
    yield
    attach.reset()


def _att(sid="s1", instance="i-pad", platform="pad", desk=False):
    sent, closed = [], []
    a = attach.Attachment(
        sid=sid, instance=instance, platform=platform, desk=desk,
        send_text=lambda payload: sent.append(payload),
        order_close=lambda code, reason: closed.append((code, reason)),
    )
    a.test_sent, a.test_closed = sent, closed
    return a


# ── driver resolution ──────────────────────────────────────────────────────

def test_no_attachments_means_no_driver():
    assert attach.driver_for("s1") is None


def test_attachment_without_input_is_not_a_driver():
    a = _att()
    attach.register(a)
    assert attach.driver_for("s1") is None


def test_most_recent_input_wins():
    older, newer = _att(instance="i-a"), _att(instance="i-b")
    attach.register(older)
    attach.register(newer)
    attach.note_input(older, now=100.0)
    attach.note_input(newer, now=200.0)
    assert attach.driver_for("s1", now=210.0) is newer


def test_input_older_than_the_window_does_not_drive():
    a = _att()
    attach.register(a)
    attach.note_input(a, now=100.0)
    assert attach.driver_for("s1", now=100.0 + attach.DRIVER_WINDOW + 1) is None


def test_driver_is_scoped_to_its_session():
    a = _att(sid="s1")
    attach.register(a)
    attach.note_input(a, now=100.0)
    assert attach.driver_for("s2", now=101.0) is None


def test_disconnected_driver_is_remembered_as_recent():
    # The iPad typed the handoff, then iOS dropped the socket while the doc
    # was still being written. The live driver is gone — but the fact "a
    # device drove this" must survive, so the spawn creates quietly instead
    # of opening a Mac window over a device-driven handoff.
    a = _att(instance="i-pad", platform="pad")
    attach.register(a)
    attach.note_input(a, now=100.0)
    attach.unregister(a)
    assert attach.driver_for("s1", now=110.0) is None
    recent = attach.recent_driver("s1", now=110.0)
    assert recent == ("i-pad", "pad", False)


def test_recent_driver_expires_with_the_window():
    a = _att()
    attach.register(a)
    attach.note_input(a, now=100.0)
    attach.unregister(a)
    assert attach.recent_driver("s1", now=100.0 + attach.DRIVER_WINDOW + 1) is None


# ── open-thread delivery ───────────────────────────────────────────────────

def test_send_open_reaches_the_attachment():
    a = _att()
    attach.register(a)
    attach.send_open(a, "jremote://session/new-sid?agent=nova")
    assert a.test_sent == [
        {"type": "open", "url": "jremote://session/new-sid?agent=nova"}]


# ── close on other instances ───────────────────────────────────────────────

def test_close_others_spares_the_asker_and_other_sessions():
    mine = _att(sid="s1", instance="i-pad")
    other = _att(sid="s1", instance="i-mac", platform="mac")
    unrelated = _att(sid="s2", instance="i-mac", platform="mac")
    for a in (mine, other, unrelated):
        attach.register(a)
    n = attach.close_others("s1", "i-pad")
    assert n == 1
    assert mine.test_closed == []
    assert unrelated.test_closed == []
    assert other.test_closed == [(4412, "dismissed from another device")]


def test_close_others_hits_every_other_view_of_the_thread():
    # The same instance may hold two windows of one thread only as one
    # attachment each — every non-asker attachment gets the order.
    a1 = _att(sid="s1", instance="i-mac", platform="mac")
    a2 = _att(sid="s1", instance="i-phone", platform="phone")
    asker = _att(sid="s1", instance="i-pad")
    for a in (a1, a2, asker):
        attach.register(a)
    assert attach.close_others("s1", "i-pad") == 2
    assert a1.test_closed and a2.test_closed and not asker.test_closed


def test_unregister_removes_from_close_fanout():
    gone = _att(sid="s1", instance="i-mac", platform="mac")
    attach.register(gone)
    attach.unregister(gone)
    assert attach.close_others("s1", "i-pad") == 0
    assert gone.test_closed == []


# ── The registry as a scheduling signal ───────────────────────────────────────

def test_the_registry_knows_when_anyone_is_in_a_terminal():
    """`board_watch` asks this to decide how hard to work — an attached thread
    means the board is behind a full-screen terminal (#30)."""
    attach.reset()
    assert attach.any_live() is False
    a = _att()
    attach.register(a)
    assert attach.any_live() is True
    attach.unregister(a)
    assert attach.any_live() is False


def test_the_board_slows_to_idle_pace_while_a_terminal_is_open(monkeypatch):
    """The observation is ~90ms of psutil and `ps` in the same interpreter that
    owes the open terminal its next PTY frame. Paying it at the board's pace
    for a covered screen is how a keystroke queues behind a process scan.

    Notifications are unaffected: TICK_IDLE is the pace they are already
    dimensioned for, and `poke()` still recomputes on the next pass, so nothing
    a user *does* waits on this.
    """
    from jstack_host import board_watch
    attach.reset()
    q = object()
    monkeypatch.setattr(board_watch, "_subscribers", {q})

    assert board_watch._tick() == board_watch.TICK, \
        "somebody is on the board and nobody is in a terminal — full pace"

    a = _att()
    attach.register(a)
    try:
        assert board_watch._tick() == board_watch.TICK_IDLE
    finally:
        attach.unregister(a)
    assert board_watch._tick() == board_watch.TICK, \
        "the thread closed — the next pass is back at full pace"

    monkeypatch.setattr(board_watch, "_subscribers", set())
    assert board_watch._tick() == board_watch.TICK_IDLE, \
        "the original rule survives: no subscriber, idle pace"
