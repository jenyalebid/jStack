"""hub_shell: the hub's own bare-shell tmux process — reachable by any
client, and invisible to `managed.reconcile()`'s reaper by construction.

Staying off the `jr-` prefix is the whole point: a bare shell with no agent
in it is precisely the debris `reconcile` exists to remove (see its own
docstring), and a login shell caught under that prefix would be silently
closed on every dashboard restart past the 60s grace.

Runs against a real tmux server on a throwaway socket, never the live
`jremote` one — same convention as `test_jremote_window_invariant.py`.
"""

import os
import select
import shutil
import subprocess
import time

import pytest

from jstack_host import hub_shell, managed
from jstack_host.store import SessionStore

pytestmark = pytest.mark.skipif(not shutil.which(managed._TMUX),
                                reason="tmux not installed")

TEST_SOCK = "jrtest-hubshell"


@pytest.fixture
def sock(monkeypatch):
    monkeypatch.setattr(managed, "_SOCK", TEST_SOCK)
    subprocess.run(managed._t("kill-server"), capture_output=True)
    yield
    subprocess.run(managed._t("kill-server"), capture_output=True)


def _sessions() -> set[str]:
    r = subprocess.run(managed._t("list-sessions", "-F", "#{session_name}"),
                       capture_output=True, text=True)
    return {x.strip() for x in r.stdout.splitlines() if x.strip()}


def test_ensure_is_idempotent_and_status_reports_it(sock):
    assert not hub_shell.is_open()
    hub_shell.ensure()
    assert hub_shell.is_open()
    assert hub_shell.NAME in _sessions()
    # A second ensure joins the process that already exists rather than
    # starting another — "single per hub by design".
    hub_shell.ensure()
    assert sum(1 for n in _sessions() if n == hub_shell.NAME) == 1


def test_close_ends_it_for_everyone(sock):
    hub_shell.ensure()
    assert hub_shell.is_open()
    hub_shell.close()
    assert not hub_shell.is_open()
    assert hub_shell.NAME not in _sessions()


def test_the_reaper_never_touches_it(sock, monkeypatch):
    """A bare shell has no claude tty to match, so a `jr-`-prefixed one would
    be reaped by `reconcile` on the very next dashboard restart — the harshest
    scan it can run. Staying off that prefix is what jRemote-Project#5's fix
    actually rests on."""
    hub_shell.ensure()
    import jstack_host.procscan as procscan
    monkeypatch.setattr(procscan, "get_claude_processes",
                        lambda: {"processes": []})
    managed.reconcile(grace=0.0)
    assert hub_shell.is_open()


def test_it_opens_at_the_install_root(sock, monkeypatch, tmp_path):
    """Not the dashboard's cwd — wherever launchd started the host is a place
    nobody chose to land in."""
    monkeypatch.setenv("JSTACK_ROOT", str(tmp_path))
    hub_shell.ensure()
    r = subprocess.run(managed._t("display-message", "-p", "-t", hub_shell.NAME,
                                  "#{pane_current_path}"),
                       capture_output=True, text=True)
    assert os.path.realpath(r.stdout.strip()) == os.path.realpath(tmp_path)


def test_a_typed_line_runs_through_the_attach_client(sock, monkeypatch):
    """The proof the shell is usable is a command's OUTPUT, not the echo of
    what was typed: `printf` prints a string that appears nowhere in the line
    itself, so seeing it means the line ran."""
    # The attach client runs on the host's minimal env, which carries no
    # TMUX_TMPDIR; a caller's scratch one would put the server elsewhere.
    monkeypatch.delenv("TMUX_TMPDIR", raising=False)
    hub_shell.ensure()
    pid, master = hub_shell._spawn_attach(100, 30)
    try:
        time.sleep(1.5)
        os.write(master, b"printf '%s-%s\\n' hubshell ran\r")
        seen, deadline = b"", time.time() + 10
        while b"hubshell-ran" not in seen and time.time() < deadline:
            r, _, _ = select.select([master], [], [], 0.2)
            if r:
                try:
                    seen += os.read(master, 65536)
                except OSError:
                    break
        assert b"hubshell-ran" in seen
    finally:
        os.close(master)
        os.kill(pid, 15)
        os.waitpid(pid, 0)


# ── inside the record ────────────────────────────────────────────────────────
#
# No board row, so the audit table is where a hub shell is accounted for:
# who opened it, from where, and when it was left or closed for everyone.

class _Socket:
    """What `audit.from_request` reads off a WebSocket or a Request."""

    def __init__(self, path="/api/jremote/v1/hub/shell", host="10.66.0.12", build="130"):
        self.url = type("U", (), {"path": path})()
        self.client = type("C", (), {"host": host})()
        self.headers = {"user-agent": "jRemote/1", "x-jremote-build": build}
        self.state = type("S", (), {"authorized_device": "dev-1"})()


@pytest.fixture
def audit_store(tmp_path, monkeypatch):
    s = SessionStore(db_path=tmp_path / "hub.sqlite")
    monkeypatch.setattr(hub_shell, "_store", lambda: s)
    monkeypatch.setattr("jstack_host.devices.row",
                        lambda device_id: {"id": device_id, "name": "Owner Phone"})
    return s


def test_open_leave_and_close_each_leave_a_row_naming_the_device(audit_store):
    hub_shell._record("hub_shell.open", "dev-1", _Socket(), started=True, cols=100, rows=30)
    hub_shell._record("hub_shell.leave", "dev-1", _Socket(), everyone=False)
    hub_shell._record("hub_shell.close", "dev-1", _Socket(), everyone=True)

    rows = audit_store.access_history(["dev-1"])
    assert [r["action"] for r in rows] == ["hub_shell.close", "hub_shell.leave", "hub_shell.open"]
    opened = rows[-1]
    assert opened["target_kind"] == "device"
    assert opened["actor_name"] == "Owner Phone"
    assert opened["origin"] == "10.66.0.12"
    assert opened["via"] == "ws /api/jremote/v1/hub/shell"
    assert opened["detail"] == {"build": "130", "started": True, "cols": 100, "rows": 30}
    assert rows[0]["detail"]["everyone"] is True


def test_the_close_route_records_who_ended_it_for_everyone(sock, audit_store):
    hub_shell.ensure()
    assert hub_shell.close_route(_Socket(path="/api/jremote/v1/hub/shell/close")) == {"open": False}
    rows = audit_store.access_history(["dev-1"])
    assert [r["action"] for r in rows] == ["hub_shell.close"]
    assert rows[0]["detail"]["via_route"] is True

    # Closing what is not open is not an event.
    hub_shell.close_route(_Socket())
    assert len(audit_store.access_history(["dev-1"])) == 1


def test_a_failed_record_never_costs_the_shell(monkeypatch):
    def boom():
        raise RuntimeError("store is away")
    monkeypatch.setattr(hub_shell, "_store", boom)
    hub_shell._record("hub_shell.open", "dev-1", _Socket())
