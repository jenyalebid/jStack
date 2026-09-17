"""Ending a session closes the window it was running in.

The window invariant already runs one way: closing the window on the Mac ends
the session. This is the other way, and it has to be the same event — a kill
from the phone that leaves an empty prompt on the desk leaves something that
looks like work, in the place work lives, with nothing behind it.

The tty is the only thing tying a process we just ended to a rectangle on the
desk, so what these tests pin down is *which* tty is collected and *when*:
before the kill (a dead process has no tty to ask for), never the phone's
mirror, and never at all when the kill did not take.

No test here ever reaches iTerm — the closer is recorded, not run.
"""

import os
import pty as _pty
import shutil
import signal
import subprocess
import time
import uuid

import pytest

from jstack_host import board, managed, router

pytestmark = pytest.mark.skipif(not shutil.which(managed._TMUX),
                                reason="tmux not installed")

TEST_SOCK = "jrtest-closewindow"
SID = "cccccccc-1111-2222-3333-444444444444"


def _t(*a):
    return [managed._TMUX, "-L", TEST_SOCK, *a]


@pytest.fixture
def sock(monkeypatch):
    monkeypatch.setitem(globals(), "TEST_SOCK", "jr-close-" + uuid.uuid4().hex)
    monkeypatch.setattr(managed, "_SOCK", TEST_SOCK)
    subprocess.run(_t("kill-server"), capture_output=True)
    yield
    subprocess.run(_t("kill-server"), capture_output=True)


@pytest.fixture
def closed(monkeypatch):
    """Record what would have been closed, instead of closing the user's windows."""
    seen = []
    monkeypatch.setattr(managed, "close_windows", lambda ttys: seen.append(list(ttys or [])))
    return seen


@pytest.fixture
def clients():
    """tmux clients in real PTYs — a Mac window, or the phone's mirror."""
    spawned = []

    def spawn(name: str, phone: bool) -> int:
        env = {"PATH": managed._PATH, "TERM": "xterm-256color",
               "HOME": os.path.expanduser("~")}
        if phone:
            env[managed.PHONE_CLIENT_ENV] = "1"
        pid, _master = _pty.fork()
        if pid == 0:  # child — exec or die, never return into the test
            try:
                os.execve(managed._TMUX, _t("attach", "-t", name), env)
            finally:
                os._exit(127)
        spawned.append(pid)
        _wait_for(lambda: name in _client_sessions(), "the client to attach")
        return pid

    yield type("C", (), {"spawn": staticmethod(spawn)})
    for pid in spawned:
        try:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass


def _client_sessions() -> set[str]:
    r = subprocess.run(_t("list-clients", "-F", "#{client_session}"),
                       capture_output=True, text=True)
    return {x.strip() for x in r.stdout.splitlines() if x.strip()}


def _wait_for(pred, what, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


# ── which windows a managed session is living in ────────────────────────────

def test_client_ttys_names_the_mac_window(sock, clients):
    name = managed._name(SID)
    subprocess.run(_t("new-session", "-d", "-s", name), check=True)
    clients.spawn(name, phone=False)

    ttys = managed.client_ttys(SID)

    assert len(ttys) == 1 and ttys[0].startswith("/dev/tty"), ttys


def test_the_phones_mirror_is_never_closed(sock, clients):
    """Closing "the window" a phone client sits in would close the phone's own
    attach — the mirror is not a window here, in either direction."""
    name = managed._name(SID)
    subprocess.run(_t("new-session", "-d", "-s", name), check=True)
    clients.spawn(name, phone=True)

    assert managed.client_ttys(SID) == []


def test_close_managed_closes_the_window_it_was_in(sock, clients, closed):
    """The kill and the window close are one action."""
    name = managed._name(SID)
    subprocess.run(_t("new-session", "-d", "-s", name), check=True)
    clients.spawn(name, phone=False)
    expected = managed.client_ttys(SID)

    assert managed.close_managed(SID, review=False) is True

    assert closed == [expected], f"closed {closed}, expected {[expected]}"


def test_close_managed_reads_the_ttys_before_the_teardown(sock, clients, closed):
    """Order is the whole trick: once the session is gone tmux can no longer
    name the clients it had, and the window would be orphaned for good."""
    name = managed._name(SID)
    subprocess.run(_t("new-session", "-d", "-s", name), check=True)
    clients.spawn(name, phone=False)

    managed.close_managed(SID, review=False)

    assert closed and closed[0], "no window was identified to close"


# ── raw Mac windows (a claude the user started themselves) ─────────────────────────

def test_window_ttys_reads_a_live_process(sock):
    pid, master = _pty.fork()
    if pid == 0:
        try:
            os.execv("/bin/sleep", ["sleep", "30"])
        finally:
            os._exit(127)
    try:
        _wait_for(lambda: board.window_ttys([pid]), "the child's tty")
        assert board.window_ttys([pid])[0].startswith("/dev/tty")
    finally:
        os.kill(pid, 9)
        os.waitpid(pid, 0)


def test_window_ttys_ignores_the_dead_and_the_headless():
    assert board.window_ttys([]) == []
    assert board.window_ttys([999999999]) == []


def test_raw_close_takes_the_window_with_it(monkeypatch, closed):
    """A raw window is where most of the user's sessions live: SIGTERM ends the
    claude but the shell — and the window — would sit there without this."""
    monkeypatch.setattr(managed, "is_open", lambda sid: False)
    monkeypatch.setattr(board, "pids_holding", lambda sid: [4242])
    monkeypatch.setattr(board, "window_ttys", lambda pids: ["/dev/ttys099"])
    monkeypatch.setattr(board, "end_raw_holders", lambda sid, sig: True)

    out = router.close_session_managed(SID, review=False)

    assert out["closed"] is True and out["mode"] == "raw"
    assert closed == [["/dev/ttys099"]]


def test_a_kill_that_failed_leaves_the_window_alone(monkeypatch, closed):
    """The claude wouldn't exit, so the session is still running in that
    window. Closing it would take the session with it — by SIGHUP, unasked."""
    monkeypatch.setattr(managed, "is_open", lambda sid: False)
    monkeypatch.setattr(board, "pids_holding", lambda sid: [4242])
    monkeypatch.setattr(board, "window_ttys", lambda pids: ["/dev/ttys099"])
    monkeypatch.setattr(board, "end_raw_holders", lambda sid, sig: False)

    with pytest.raises(Exception) as e:
        router.close_session_managed(SID, review=False)

    assert getattr(e.value, "status_code", None) == 504
    assert closed == [], "a failed kill must not close the window"


def test_an_idle_session_closes_no_window(monkeypatch, closed):
    """Nothing is holding it — there is no window of ours to close, and the
    row is already gone from the board."""
    monkeypatch.setattr(managed, "is_open", lambda sid: False)
    monkeypatch.setattr(board, "pids_holding", lambda sid: [])

    out = router.close_session_managed(SID, review=True)

    assert out["mode"] == "idle"
    assert closed == []


def test_fresh_window_row_closes_its_own_window(monkeypatch, closed):
    """A `pid-<n>` row IS a window — killing it must not leave the very thing
    the row was named after. Signalled for real, against a real process, so the
    wait-for-exit leg is the one that ships."""
    # Orphaned on purpose: this process must not be the target's parent, or a
    # SIGTERMed child lingers as a zombie and `kill(pid, 0)` still succeeds —
    # the test would fail on its own reaping, not on the code.
    # …and its output goes nowhere: an orphan holding the capture pipe open
    # makes `subprocess.run` wait out the whole sleep before returning.
    pid = int(subprocess.run(
        ["/bin/sh", "-c", "/bin/sleep 30 >/dev/null 2>&1 </dev/null & echo $!"],
        capture_output=True, text=True).stdout.strip())
    monkeypatch.setattr("jstack_host.procscan.get_claude_processes",
                        lambda: {"processes": [{"pid": pid}]})
    monkeypatch.setattr(board, "window_ttys", lambda pids: ["/dev/ttys077"])
    try:
        out = router.close_session_managed(f"pid-{pid}", review=False)
    finally:
        subprocess.run(["kill", "-9", str(pid)], capture_output=True)

    assert out["closed"] is True and out["mode"] == "window"
    assert closed == [["/dev/ttys077"]]


def test_an_unknown_pid_row_signals_nothing(monkeypatch, closed):
    """A stale row must never reach an unrelated process — only pids the scan
    currently reports are ever signalled."""
    monkeypatch.setattr("jstack_host.procscan.get_claude_processes",
                        lambda: {"processes": []})

    out = router.close_session_managed("pid-999999999", review=False)

    assert out == {"ok": True, "closed": False, "mode": "gone"}
    assert closed == []


def test_kill_on_an_unnamed_row_is_a_real_kill(monkeypatch, closed):
    """Kill must mean SIGKILL on a `pid-<n>` row too. This branch always sent
    SIGTERM, whatever the caller asked for — so the hard end was the soft end
    wearing its label, and the one case that makes anyone reach for Kill (a
    process that will not take a TERM) survived it.

    The target ignores SIGTERM, so only a real kill can end it. `trap '' TERM`
    sets SIG_IGN, which survives the fork and the exec into `sleep`."""
    pid = int(subprocess.run(
        ["/bin/sh", "-c",
         "trap '' TERM; /bin/sleep 30 >/dev/null 2>&1 </dev/null & echo $!"],
        capture_output=True, text=True).stdout.strip())
    monkeypatch.setattr("jstack_host.procscan.get_claude_processes",
                        lambda: {"processes": [{"pid": pid}]})
    monkeypatch.setattr(board, "window_ttys", lambda pids: [])
    try:
        out = router.close_session_managed(f"pid-{pid}", review=False)
    finally:
        subprocess.run(["kill", "-9", str(pid)], capture_output=True)

    assert out["closed"] is True and out["mode"] == "window"
