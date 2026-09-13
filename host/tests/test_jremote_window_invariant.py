"""The jRemote board invariant: a managed session lives exactly as long as its
claude process, and it is on the board the whole time. Every client — the
phone, the Mac app, an iTerm window — is a mirror that attaches and detaches
freely; none of them owns the session's life.

Each seam gets a test against a real tmux server, not a mock:

  * window truth stays an honest display fact: the phone's tagged client is
    never counted as an iTerm window;
  * detaching ends nothing — a session survives its last client leaving;
  * creation is windowless by default and never touches iTerm; the desk-side
    `window=True` path stays transactional (no window ⇒ no fresh session);
  * a takeover displaces the old holder only once the managed session exists,
    and only before `claude` opens the transcript;
  * `reconcile` disarms the legacy `destroy-unattached` and reaps sessions
    whose pane no longer runs a claude — and only those.

A dead pane is the only thing anything here ends on its own. Nothing closes a
session for being quiet: every autonomous path on this machine is
`claude --print`, which exits on its own and never holds a tmux, so a managed
session exists only because a hand made one — and a timer over those could only
ever close a window someone opened on purpose.

Everything runs on a throwaway socket; the live `jremote` socket is never
touched.
"""

import os
import pty as _pty
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import needs_embedding_tree

from jstack_host import managed

pytestmark = pytest.mark.skipif(not shutil.which(managed._TMUX),
                                reason="tmux not installed")

INFRA = Path(__file__).resolve().parents[1]
TEST_SOCK = "jrtest-invariant"
SID = "aaaaaaaa-1111-2222-3333-444444444444"


def _t(*a):
    return [managed._TMUX, "-L", TEST_SOCK, *a]


def _sessions() -> set[str]:
    r = subprocess.run(_t("list-sessions", "-F", "#{session_name}"),
                       capture_output=True, text=True)
    return {x.strip() for x in r.stdout.splitlines() if x.strip()}


@pytest.fixture
def sock(monkeypatch):
    """Point the module at a private tmux socket, and leave none behind.

    Also clears the transcript SID writes, because leaving it behind poisons the
    NEXT run: these tests spawn a real `claude --session-id SID`, and a second
    claim on an id that already has a transcript exits immediately. The pane
    dies on startup, and the failure surfaces as "a detach must never end a
    session" — a green suite that goes red on its own second run, blaming the
    detach path for a stale file.
    """
    monkeypatch.setattr(managed, "_SOCK", TEST_SOCK)

    def _wipe_transcript():
        for p in Path("~/.claude/projects").expanduser().glob(f"*/{SID}.jsonl"):
            p.unlink(missing_ok=True)

    _wipe_transcript()
    subprocess.run(_t("kill-server"), capture_output=True)
    yield
    subprocess.run(_t("kill-server"), capture_output=True)
    _wipe_transcript()


@pytest.fixture
def no_iterm(monkeypatch):
    """Creation must never reach for iTerm unless a window was asked for."""
    def _poisoned(sid):
        raise AssertionError("launch_terminal called on a windowless open")
    monkeypatch.setattr(managed, "launch_terminal", _poisoned)
    monkeypatch.setattr(managed, "_auto_accept_bypass", lambda name: None)


@pytest.fixture
def clients():
    """Spawns tmux clients in real PTYs; reaps whatever survives the test."""
    spawned = []

    def spawn(name: str, phone: bool):
        env = {"PATH": managed._PATH, "TERM": "xterm-256color",
               "HOME": os.path.expanduser("~")}
        if phone:
            env[managed.PHONE_CLIENT_ENV] = "1"
        pid, master = _pty.fork()
        if pid == 0:  # child — exec or die, never return into the test
            try:
                os.execve(managed._TMUX, _t("attach", "-t", name), env)
            finally:
                os._exit(127)
        spawned.append(pid)
        _wait_for(lambda: name in _clients_raw(), "client to attach")
        return pid

    def kill(pid):
        try:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass

    yield type("C", (), {"spawn": staticmethod(spawn), "kill": staticmethod(kill)})
    for pid in spawned:
        kill(pid)


def _clients_raw() -> set[str]:
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


def _new(name: str):
    subprocess.run(_t("new-session", "-d", "-s", name), check=True)


def _destroy_unattached(name: str) -> str:
    return subprocess.run(_t("show-options", "-t", name, "destroy-unattached"),
                          capture_output=True, text=True).stdout


def _patch_claude_ttys(monkeypatch, ttys):
    """reconcile judges claude-ness by pane tty against the live claude scan —
    pin the scan's answer."""
    import jstack_host.procscan as procscan
    monkeypatch.setattr(procscan, "get_claude_processes",
                        lambda: {"processes": [{"tty": t} for t in ttys]})


# ── window truth: an honest display fact ────────────────────────────────────

def test_phone_client_is_not_a_window(sock, clients):
    """The phone/app mirror must never read as "an iTerm window is showing
    this" — the board's `open` flag would lie about the desk."""
    name = managed._name(SID)
    _new(name)
    clients.spawn(name, phone=True)

    assert name in _clients_raw(), "tmux should see the phone's client"
    assert managed.attached_names() == set()
    assert managed.has_window(SID) is False


def test_mac_client_is_a_window(sock, clients):
    name = managed._name(SID)
    _new(name)
    clients.spawn(name, phone=False)

    assert managed.attached_names() == {name}
    assert managed.has_window(SID) is True


def test_unreadable_client_counts_as_a_window(sock, monkeypatch):
    """Unknown answers say "window" — the honest default for a display fact
    (and for which ttys a close takes with it)."""
    monkeypatch.setattr(managed.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(
                            a[0], 0, stdout="jr-aaaaaaaa\t999999999\n", stderr=""))
    assert managed.attached_names() == {"jr-aaaaaaaa"}


# ── detaching ends nothing ──────────────────────────────────────────────────

def test_last_client_detach_leaves_the_session_running(sock, clients, no_iterm):
    """The board invariant's core: every client is a viewer. Closing the last
    window — iTerm included — detaches; the session's life is its process."""
    name = managed._name(SID)
    managed.open_managed(SID, os.path.expanduser("~"), resume=False)
    mac = clients.spawn(name, phone=False)

    clients.kill(mac)
    time.sleep(1.0)
    assert name in _sessions(), "a detach must never end a session"


def test_creation_does_not_arm_destroy_unattached(sock, no_iterm):
    """`destroy-unattached` was the old window invariant's enforcement; under
    the board invariant it would kill idle chats on their first detach."""
    managed.open_managed(SID, os.path.expanduser("~"), resume=False)
    assert "on" not in _destroy_unattached(managed._name(SID))


# ── creation: windowless by default, transactional when a window is asked ───

def test_open_is_windowless_and_starts_claude(sock, no_iterm):
    """Default creation never touches iTerm (the poisoned launch_terminal
    proves it) and execs claude, binding the session's life to the process."""
    sent = []
    real_run = managed.subprocess.run

    def spy(cmd, *a, **k):
        if isinstance(cmd, list) and "send-keys" in cmd:
            sent.append(" ".join(cmd))
        return real_run(cmd, *a, **k)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(managed.subprocess, "run", spy)
    try:
        managed.open_managed(SID, os.path.expanduser("~"), resume=False)
    finally:
        monkey.undo()

    assert managed.is_open(SID)
    # The command lives in the boot file now; the pane is only typed a source
    # line, so that is where `exec claude` has to be proven.
    boot = managed._BOOT_DIR / f"jremote-boot-{SID[:8]}.sh"
    assert boot.exists(), "the pane's boot command must be staged as a file"
    assert "exec claude" in boot.read_text(), \
        "the pane must exec claude so the session dies with the process"
    assert any("jremote-boot" in c and "source" in c for c in sent), \
        "the pane is typed a source line, never the command itself"


def test_nothing_typed_into_a_pane_scales_with_input(sock, no_iterm):
    """A fresh pane's tty is still in CANONICAL mode, where the line discipline
    drops everything past MAX_CANON (1024 bytes) without a word.

    The exports alone were ~820 of that budget, so a takeover whose focus text
    ran long crossed it: the line arrived cut mid-quote, zsh sat at a `quote>`
    continuation, and `claude` was never reached — a window with a briefing in
    it and no CLI. The payload belongs in a file; only a fixed-length `source`
    may be typed."""
    sent = []
    real_run = managed.subprocess.run

    def spy(cmd, *a, **k):
        if isinstance(cmd, list) and "send-keys" in cmd:
            sent.append(cmd)
        return real_run(cmd, *a, **k)

    huge = "z" * 8000
    monkey = pytest.MonkeyPatch()
    monkey.setattr(managed.subprocess, "run", spy)
    try:
        managed.open_managed(SID, os.path.expanduser("~"), resume=False,
                             extra=f"'{huge}'")
    finally:
        monkey.undo()

    for cmd in sent:
        for part in cmd:
            assert len(part.encode()) < managed._MAX_CANON, (
                f"typed {len(part.encode())} bytes into a pane — anything past "
                f"{managed._MAX_CANON} is silently dropped")
    boot = managed._BOOT_DIR / f"jremote-boot-{SID[:8]}.sh"
    assert huge in boot.read_text(), "the payload must survive, in the file"


def test_windowed_open_without_a_window_leaves_no_session(sock, monkeypatch):
    """The desk-side spawn (`window=True`) stays transactional: a handoff that
    opens nowhere visible is a failed handoff, not an invisible session."""
    monkeypatch.setattr(managed, "WINDOW_TIMEOUT", 0.5)
    monkeypatch.setattr(managed, "launch_terminal", lambda sid: None)  # no window

    with pytest.raises(managed.WindowRequired):
        managed.open_managed(SID, os.path.expanduser("~"), resume=False,
                             window=True)

    assert _sessions() == set(), "no tmux session may survive a failed spawn"
    assert managed.is_open(SID) is False


def test_windowed_open_failure_does_not_start_claude(sock, monkeypatch):
    """On the window=True path claude starts only after the window is up, so
    the torn-down session never ran anything — the shell-only gap is inert."""
    monkeypatch.setattr(managed, "WINDOW_TIMEOUT", 0.5)
    monkeypatch.setattr(managed, "launch_terminal", lambda sid: None)
    sent = []
    real_run = managed.subprocess.run

    def spy(cmd, *a, **k):
        if isinstance(cmd, list) and "send-keys" in cmd:
            sent.append(cmd)
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(managed.subprocess, "run", spy)
    with pytest.raises(managed.WindowRequired):
        managed.open_managed(SID, os.path.expanduser("~"), resume=False,
                             window=True)

    assert not any("claude" in " ".join(c) for c in sent), \
        "claude must never start in a spawn whose window never arrived"


def test_windowed_open_attaches_without_arming(sock, monkeypatch, clients):
    """The happy window=True path gets its viewer and nothing more — the
    window's later close must detach, not end the session."""
    name = managed._name(SID)
    monkeypatch.setattr(managed, "WINDOW_TIMEOUT", 5.0)
    monkeypatch.setattr(managed, "launch_terminal",
                        lambda sid: clients.spawn(managed._name(sid), phone=False))
    monkeypatch.setattr(managed, "_auto_accept_bypass", lambda name: None)

    managed.open_managed(SID, os.path.expanduser("~"), resume=False, window=True)

    assert managed.has_window(SID) is True
    assert "on" not in _destroy_unattached(name)


# ── takeover: displace after the session exists, before claude ──────────────

def test_takeover_runs_between_the_session_and_claude(sock, no_iterm):
    """The old holder is ended only once the managed session it is moving to
    exists, and only before `claude --resume` opens the transcript — the gap
    where exactly one writer holds it."""
    order = []
    real_run = managed.subprocess.run

    def spy(cmd, *a, **k):
        if isinstance(cmd, list) and "send-keys" in cmd and "jremote-boot" in " ".join(cmd):
            order.append("claude")
        return real_run(cmd, *a, **k)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(managed.subprocess, "run", spy)

    def displace():
        order.append("session" if managed.is_open(SID) else "no-session")
        return True

    try:
        managed.open_managed(SID, os.path.expanduser("~"), resume=True,
                             displace=displace)
    finally:
        monkey.undo()

    assert order == ["session", "claude"], f"wrong order: {order}"


def test_refused_takeover_tears_the_new_session_back_down(sock, no_iterm):
    """If the old claude won't exit, the transcript is still held — so the
    session standing up for it is destroyed rather than becoming a second
    writer on one file."""
    sent = []
    real_run = managed.subprocess.run

    def spy(cmd, *a, **k):
        if isinstance(cmd, list) and "send-keys" in cmd:
            sent.append(cmd)
        return real_run(cmd, *a, **k)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(managed.subprocess, "run", spy)
    try:
        with pytest.raises(managed.TakeoverFailed):
            managed.open_managed(SID, os.path.expanduser("~"), resume=True,
                                 displace=lambda: False)
    finally:
        monkey.undo()

    assert not any("claude" in " ".join(c) for c in sent), \
        "a second claude must never open a transcript the first still holds"
    _wait_for(lambda: managed._name(SID) not in _sessions(),
              "the refused takeover's session to be destroyed")


# ── the takeover auto-continue types only at claude's prompt ─────────────────

def test_nudge_lands_only_once_the_prompt_is_ready(sock):
    """The continue nudge must never fall into the shell still launching
    claude: the watcher waits for the TUI's status-bar marker, then types."""
    name = "jr-nudge-rdy"
    _new(name)
    managed._nudge_when_ready(name, "CONTINUE-NUDGE")
    time.sleep(2.0)  # several watcher polls with no marker on screen
    pane = subprocess.run(_t("capture-pane", "-p", "-t", name),
                          capture_output=True, text=True).stdout
    assert "CONTINUE-NUDGE" not in pane, "keys landed before the prompt was up"
    # The marker appearing (echoed here by the shell) is the green light.
    subprocess.run(_t("send-keys", "-t", name, "-l",
                      'printf "bypass permissions on\\n"'), check=True)
    subprocess.run(_t("send-keys", "-t", name, "Enter"), check=True)
    _wait_for(lambda: "CONTINUE-NUDGE" in subprocess.run(
        _t("capture-pane", "-p", "-t", name),
        capture_output=True, text=True).stdout, "the nudge to land", timeout=8.0)


def test_nudge_waits_out_the_startup_warning(sock):
    """While the bypass warning is on screen the accept watcher owns the keys —
    the nudge must not race text into that dialog."""
    name = "jr-nudge-wrn"
    _new(name)
    subprocess.run(_t("send-keys", "-t", name, "-l",
                      'printf "bypass permissions on\\nYes, I accept\\n"'),
                   check=True)
    subprocess.run(_t("send-keys", "-t", name, "Enter"), check=True)
    managed._nudge_when_ready(name, "CONTINUE-NUDGE")
    time.sleep(2.0)
    pane = subprocess.run(_t("capture-pane", "-p", "-t", name),
                          capture_output=True, text=True).stdout
    assert "CONTINUE-NUDGE" not in pane, "typed into the warning dialog"
    # Warning answered: only the ready marker remains on screen.
    subprocess.run(_t("send-keys", "-t", name, "-l",
                      'clear; printf "bypass permissions on\\n"'), check=True)
    subprocess.run(_t("send-keys", "-t", name, "Enter"), check=True)
    _wait_for(lambda: "CONTINUE-NUDGE" in subprocess.run(
        _t("capture-pane", "-p", "-t", name),
        capture_output=True, text=True).stdout, "the nudge to land", timeout=8.0)


# ── the startup pass: disarm legacy arming, reap only the claude-less ───────

def test_reconcile_disarms_legacy_destroy_unattached(sock, clients, monkeypatch):
    """A session armed under the old window invariant must not die on its next
    detach — reconcile turns the option off, and the detach that would have
    killed it now merely detaches. (Arming needs a client attached: tmux
    destroys a clientless session the moment the option lands.)"""
    name = managed._name(SID)
    _new(name)
    mac = clients.spawn(name, phone=False)
    subprocess.run(_t("set-option", "-t", name, "destroy-unattached", "on"),
                   check=True)
    pane_tty = subprocess.run(_t("list-panes", "-t", name, "-F", "#{pane_tty}"),
                              capture_output=True, text=True).stdout.strip()
    _patch_claude_ttys(monkeypatch, [pane_tty])  # claude alive → spared

    assert managed.reconcile(grace=0.0) == []
    assert "on" not in _destroy_unattached(name)

    clients.kill(mac)
    time.sleep(1.0)
    assert name in _sessions(), "the disarmed session must survive its detach"


def test_reconcile_reaps_claudeless_and_spares_working(sock, monkeypatch):
    """Life is bound to the process: a pane with no claude is a dead session
    still occupying the board; one whose pane tty the claude scan reports is
    working and must never be swept — windows don't enter into it."""
    alive = managed._name(SID)
    dead = "jr-bbbbbbbb"
    _new(alive)
    _new(dead)
    alive_tty = subprocess.run(_t("list-panes", "-t", alive, "-F", "#{pane_tty}"),
                               capture_output=True, text=True).stdout.strip()
    _patch_claude_ttys(monkeypatch, [alive_tty])

    reaped = managed.reconcile(grace=0.0)

    assert reaped == ["bbbbbbbb"], f"swept the wrong set: {reaped}"
    assert alive in _sessions(), "a session with a live claude must never be swept"


def test_reconcile_spares_fresh_sessions(sock, monkeypatch):
    """The grace window: a just-created session hasn't started its claude yet
    and is not a corpse."""
    name = managed._name(SID)
    _new(name)
    _patch_claude_ttys(monkeypatch, [])

    assert managed.reconcile(grace=60.0) == []
    assert name in _sessions()


def test_reconcile_reaps_nothing_when_the_scan_fails(sock, monkeypatch):
    """A failed claude scan must never read as "no claudes anywhere" — that
    verdict would reap every live session on the socket."""
    name = managed._name(SID)
    _new(name)
    import jstack_host.procscan as procscan
    monkeypatch.setattr(procscan, "get_claude_processes",
                        lambda: (_ for _ in ()).throw(RuntimeError("scan down")))

    assert managed.reconcile(grace=0.0) == []
    assert name in _sessions()


def test_reconcile_ignores_foreign_sessions(sock, monkeypatch):
    """Only our own `jr-` sessions are ours to touch."""
    _new("someones-work")
    _patch_claude_ttys(monkeypatch, [])

    assert managed.reconcile(grace=0.0) == []
    assert "someones-work" in _sessions()


@needs_embedding_tree
def test_importing_the_dashboard_closes_nothing():
    """The startup pass belongs to the serving process, never to module import.

    Regression: it once ran at import time, so the pytest suite — which imports
    `dashboard.app` — ended live sessions on the Mac as a side effect. Anything
    that so much as imports the dashboard (a test run, a REPL, a one-off
    script) would do the same. Import must be inert."""
    sock = "jrtest-import"
    tmux = [managed._TMUX, "-L", sock]
    subprocess.run(tmux + ["kill-server"], capture_output=True)
    subprocess.run(tmux + ["new-session", "-d", "-s", "jr-cccccccc"], check=True)
    try:
        r = subprocess.run([sys.executable, "-c", "import dashboard.app"],
                           cwd=str(INFRA), capture_output=True, text=True,
                           env={**os.environ, "JREMOTE_TMUX_SOCK": sock})
        assert r.returncode == 0, f"import failed: {r.stderr[-2000:]}"
        alive = subprocess.run(tmux + ["has-session", "-t", "jr-cccccccc"],
                               capture_output=True).returncode == 0
        assert alive, "importing dashboard.app closed a claude-less session"
    finally:
        subprocess.run(tmux + ["kill-server"], capture_output=True)


# ── registry hygiene ────────────────────────────────────────────────────────

@pytest.fixture
def reg(monkeypatch, tmp_path):
    """The open registry is real state on this Mac — never let a test write
    the user's copy."""
    monkeypatch.setattr(managed, "_REG", tmp_path / "jremote_open.json")


def test_reconcile_prunes_dead_registry_rows(sock, reg, monkeypatch):
    """A session whose claude exited on its own never reaches `record_close`,
    so its registry row outlives it. Left alone the file grows forever."""
    name = managed._name(SID)
    _new(name)
    _patch_claude_ttys(monkeypatch, list(managed.pane_ttys()))
    managed.record_open(SID, "nova")
    managed.record_open("eeeeeeee-0000-0000-0000-000000000000", "nova")

    managed.reconcile(grace=0.0)

    assert set(managed._reg_load()) == {SID}, "kept a row for a dead session"


def test_a_malformed_registry_reads_as_empty_not_a_crash(sock, reg, monkeypatch,
                                                         capsys):
    """The registry is a JSON object. A file holding valid JSON of any other
    shape used to reach the callers verbatim, and each raised its own unrelated
    error far from the cause — a list got `reconcile` a `list.pop(str)`
    TypeError. `reconcile` runs at dashboard startup, so that took the whole
    pass down: no legacy destroy-unattached disarmed, no dead pane reaped."""
    name = managed._name(SID)
    _new(name)
    _patch_claude_ttys(monkeypatch, list(managed.pane_ttys()))
    managed._REG.parent.mkdir(parents=True, exist_ok=True)
    managed._REG.write_text('["not-an-object"]')

    assert managed._reg_load() == {}
    assert "not an object" in capsys.readouterr().err, "corruption went silent"

    managed.reconcile(grace=0.0)  # the crash: TypeError out of duty 3


def test_reconcile_keeps_the_registry_when_tmux_goes_dark(sock, reg, monkeypatch):
    """`open_names()` returns an empty set for "no sessions" AND for a socket
    that failed to answer. Reading the second as the first would erase the
    registry for every live session on the machine."""
    managed.record_open(SID, "nova")
    monkeypatch.setattr(managed, "open_names", lambda: set())
    _patch_claude_ttys(monkeypatch, [])

    managed.reconcile(grace=0.0)

    assert set(managed._reg_load()) == {SID}, "a dark socket wiped the registry"
