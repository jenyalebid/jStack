"""A managed pane cannot reach the control server (#257).

THE WHOLE STACK DIED TO ONE UNSCOPED `tmux kill-server` TYPED IN A PANE. tmux
puts `TMUX=<socket>,…` in every pane it spawns and a bare `tmux` follows it,
so every agent's scratch tmux — a parked build, an acceptance run — was a
sibling of every live agent on the one `jremote` server, and `kill-server`
there meant all of them. The fix is the pane's own environment: `TMUX`
cleared, `TMUX_TMPDIR` pointed at a scratch dir of the session's own, the
session's name exported as `JREMOTE_SESSION` for the one reader that needed
`TMUX` (a handoff learning where it was typed).

The tmux-backed tests spawn a pane the way `open_managed` does — same
`_pane_command`, same `default-command` — on a throwaway control server under
the suite's `TMUX_TMPDIR`, then type into it and read what its shell sees.
"""

import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from jstack_host import managed, spawn

pytestmark = pytest.mark.skipif(not shutil.which(managed._TMUX),
                                reason="tmux not installed")

SID = "deadbeef-0000-4000-8000-000000000257"


def _t(sock, *a):
    return [managed._TMUX, "-L", sock, *a]


def _type(sock, target, line):
    subprocess.run(_t(sock, "send-keys", "-t", target, "-l", line), check=True)
    subprocess.run(_t(sock, "send-keys", "-t", target, "Enter"), check=True)


def _wait_for(path: Path, marker: str, timeout=15.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and marker in path.read_text():
            return path.read_text()
        time.sleep(0.2)
    raise AssertionError(f"{marker!r} never appeared in {path}: "
                         f"{path.read_text() if path.exists() else '<missing>'}")


def _sessions(argv) -> list[str]:
    r = subprocess.run(argv + ["list-sessions", "-F", "#S"],
                       capture_output=True, text=True)
    return r.stdout.split() if r.returncode == 0 else []


@pytest.fixture
def control(monkeypatch, tmp_path):
    """A throwaway control server with one managed pane spawned the way
    `open_managed` spawns it. Yields (sock_label, session_name, outfile)."""
    sock = "jr-iso-" + uuid.uuid4().hex[:12]
    monkeypatch.setattr(managed, "_SOCK", sock)
    name = managed._name(SID)
    managed._make_scratch(SID)
    cmd = managed._pane_command(SID)
    subprocess.run(_t(sock, "new-session", "-d", "-s", name, "-c", "/tmp", cmd),
                   check=True)
    subprocess.run(_t(sock, "set-option", "-t", name, "default-command", cmd),
                   check=True)
    out = tmp_path / "pane.txt"
    try:
        yield sock, name, out
    finally:
        for s in managed._scratch_sockets(managed.scratch_dir(SID)):
            subprocess.run([managed._TMUX, "-S", str(s), "kill-server"],
                           capture_output=True)
        subprocess.run(_t(sock, "kill-server"), capture_output=True)
        shutil.rmtree(managed.scratch_dir(SID), ignore_errors=True)


# ── the pane's environment ───────────────────────────────────────────────────

def test_pane_command_clears_tmux_and_names_the_session():
    cmd = managed._pane_command(SID)
    assert cmd.startswith("exec env -u TMUX ")
    assert f"JREMOTE_SESSION=jr-deadbeef" in cmd
    assert f"TMUX_TMPDIR={managed.scratch_dir(SID)}" in cmd
    assert cmd.endswith(" -l"), "the pane's shell is a login shell"
    assert "TMUX_PANE" not in cmd, "only TMUX is cleared; TMUX_PANE stays"


def test_scratch_dir_is_per_session_under_the_servers_tmux_dir(monkeypatch):
    monkeypatch.setenv("TMUX_TMPDIR", "/tmp/somewhere")
    assert managed.scratch_dir(SID) == Path("/tmp/somewhere/jremote-scratch/jr-deadbeef")
    monkeypatch.delenv("TMUX_TMPDIR")
    assert managed.scratch_dir(SID) == Path("/tmp/jremote-scratch/jr-deadbeef")


def test_pane_shell_has_no_tmux_after_its_profile_ran(control):
    sock, name, out = control
    _type(sock, name, f'echo "ENV TMUX=[$TMUX] PANE=[$TMUX_PANE] '
                      f'JR=[$JREMOTE_SESSION] TD=[$TMUX_TMPDIR]" >> {out}')
    got = _wait_for(out, "ENV ")
    assert "TMUX=[]" in got
    assert "PANE=[%" in got, "TMUX_PANE is kept"
    assert f"JR=[{name}]" in got
    assert f"TD=[{managed.scratch_dir(SID)}]" in got


def test_a_bare_tmux_in_the_pane_cannot_see_the_control_server(control):
    sock, name, out = control
    # What an agent parks a build in — bare `tmux`, no -L/-S — then `tmux ls`.
    _type(sock, name, f"tmux new-session -d -s scratchrun -c /tmp; "
                      f"tmux ls >> {out}; echo LS-DONE >> {out}")
    got = _wait_for(out, "LS-DONE")
    assert "scratchrun" in got
    assert name not in got, "the control session is invisible to the pane"
    # The control server has exactly what it had; the scratch server is private.
    assert _sessions(_t(sock)) == [name]
    socks = managed._scratch_sockets(managed.scratch_dir(SID))
    assert [s.name for s in socks] == ["default"]
    assert _sessions([managed._TMUX, "-S", str(socks[0])]) == ["scratchrun"]


def test_a_bare_kill_server_in_the_pane_leaves_the_control_server_alive(control):
    sock, name, out = control
    _type(sock, name, f"tmux new-session -d -s scratchrun -c /tmp; "
                      f"tmux kill-server; echo KS=$? >> {out}")
    got = _wait_for(out, "KS=")
    assert "KS=0" in got, "the pane's kill-server succeeded — against its own server"
    assert _sessions(_t(sock)) == [name]
    socks = managed._scratch_sockets(managed.scratch_dir(SID))
    assert not any(managed._server_answers(s) for s in socks)


def test_new_windows_and_respawns_run_under_the_same_wrapper(control):
    sock, name, out = control
    subprocess.run(_t(sock, "new-window", "-t", name), check=True)
    _type(sock, f"{name}:1", f'echo "W1 TMUX=[$TMUX] JR=[$JREMOTE_SESSION]" >> {out}')
    got = _wait_for(out, "W1 ")
    assert "W1 TMUX=[] JR=[%s]" % name in got
    subprocess.run(_t(sock, "respawn-pane", "-k", "-t", f"{name}:1"), check=True)
    _type(sock, f"{name}:1", f'echo "RS TMUX=[$TMUX] JR=[$JREMOTE_SESSION]" >> {out}')
    got = _wait_for(out, "RS ")
    assert "RS TMUX=[] JR=[%s]" % name in got


# ── a jstack_host process inside a pane leaves the scratch ──────────────────

def test_a_process_under_a_scratch_tmpdir_returns_to_the_root_it_came_from():
    env = {"TMUX_TMPDIR": "/tmp/jsh-tmux-abc/jremote-scratch/jr-deadbeef"}
    managed._leave_scratch(env)
    assert env["TMUX_TMPDIR"] == "/tmp/jsh-tmux-abc"


def test_a_process_under_a_tmp_scratch_unsets_the_variable():
    env = {"TMUX_TMPDIR": "/tmp/jremote-scratch/jr-deadbeef"}
    managed._leave_scratch(env)
    assert "TMUX_TMPDIR" not in env


def test_a_non_scratch_tmpdir_is_left_alone():
    env = {"TMUX_TMPDIR": "/tmp/jsh-tmux-abc"}
    managed._leave_scratch(env)
    assert env == {"TMUX_TMPDIR": "/tmp/jsh-tmux-abc"}
    env = {}
    managed._leave_scratch(env)
    assert env == {}


def test_the_control_client_never_inherits_a_scratch_tmpdir(monkeypatch):
    # A handoff typed in a pane runs open_managed in-process; its -L client
    # must resolve the same dir the Hub's server lives in, not the pane's.
    monkeypatch.setenv("TMUX_TMPDIR", "/tmp/jsh-tmux-abc/jremote-scratch/jr-deadbeef")
    managed._leave_scratch()
    assert managed._server_env()["TMUX_TMPDIR"] == "/tmp/jsh-tmux-abc"


# ── the scratch goes with the session ───────────────────────────────────────

@pytest.fixture
def short_tmp(monkeypatch):
    """A socket path is capped at 104 bytes; pytest's tmp_path is over it."""
    d = tempfile.mkdtemp(prefix="jsh-257-", dir="/tmp")
    monkeypatch.setenv("TMUX_TMPDIR", d)
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


def test_dispose_removes_a_scratch_holding_only_dead_sockets(short_tmp):
    d = managed._make_scratch(SID)
    sub = d / "tmux-501"
    sub.mkdir()
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(sub / "default"))
    s.close()
    assert (sub / "default").is_socket()
    assert managed.dispose_scratch(SID) is True
    assert not d.exists()


def test_dispose_keeps_a_scratch_whose_server_still_answers(short_tmp):
    d = managed._make_scratch(SID)
    argv = [managed._TMUX, "-S", str(d / "tmux-x" / "parked")]
    (d / "tmux-x").mkdir()
    subprocess.run(argv + ["new-session", "-d", "-s", "build", "-c", "/tmp"], check=True)
    try:
        assert managed.dispose_scratch(SID) is False
        assert d.exists()
        assert _sessions(argv) == ["build"], "nobody named that server; it lives"
    finally:
        subprocess.run(argv + ["kill-server"], capture_output=True)
    assert managed.dispose_scratch(SID) is True


def test_dispose_of_a_missing_scratch_is_a_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    assert managed.dispose_scratch(SID) is True


def test_sweep_disposes_only_the_dirs_of_dead_sessions(monkeypatch, tmp_path):
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    live = managed._make_scratch("aaaaaaaa-live")
    dead = managed._make_scratch("bbbbbbbb-dead")
    (managed._scratch_root() / "not-ours").mkdir()
    gone = managed.sweep_scratch({"jr-aaaaaaaa"})
    assert gone == ["jr-bbbbbbbb"]
    assert live.exists() and not dead.exists()
    assert (managed._scratch_root() / "not-ours").exists()


def test_close_managed_disposes_the_scratch(monkeypatch, tmp_path):
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    d = managed._make_scratch(SID)
    monkeypatch.setattr(managed, "is_open", lambda sid: True)
    monkeypatch.setattr(managed, "client_ttys", lambda sid: [])
    monkeypatch.setattr(managed, "record_close", lambda sid: None)
    monkeypatch.setattr(managed, "close_windows", lambda ttys: None)
    monkeypatch.setattr(managed.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    assert managed.close_managed(SID, review=False) is True
    assert not d.exists()


# ── origin_sid: the one reader of $TMUX, now reading JREMOTE_SESSION ────────

def test_origin_sid_resolves_via_jremote_session_without_tmux(monkeypatch):
    full = "ab12cd34-aff7-4b4b-865a-c2f502c1a66e"
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setenv("JREMOTE_SESSION", "jr-ab12cd34")
    calls = []
    monkeypatch.setattr(spawn.subprocess, "run",
                        lambda *a, **k: calls.append(a) or None)
    monkeypatch.setattr(managed, "open_registry", lambda: {full: {"agent": "x"}})
    assert spawn.origin_sid() == full
    assert calls == [], "no tmux is asked — the pane already knows its name"


def test_origin_sid_jremote_session_wins_over_a_legacy_tmux(monkeypatch):
    full = "ab12cd34-aff7-4b4b-865a-c2f502c1a66e"
    monkeypatch.setenv("TMUX", "/private/tmp/tmux-501/jremote,999,3")
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setenv("JREMOTE_SESSION", "jr-ab12cd34")
    monkeypatch.setattr(spawn.subprocess, "run",
                        lambda *a, **k: pytest.fail("legacy path consulted"))
    monkeypatch.setattr(managed, "open_registry", lambda: {full: {}})
    assert spawn.origin_sid() == full


def test_origin_sid_jremote_session_unknown_to_the_registry_is_empty(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("JREMOTE_SESSION", "jr-ab12cd34")
    monkeypatch.setattr(managed, "open_registry", lambda: {})
    assert spawn.origin_sid() == ""


def test_origin_sid_still_resolves_a_pane_from_an_older_hub(monkeypatch):
    # No JREMOTE_SESSION — a pane created before #257 — so $TMUX + $TMUX_PANE
    # are asked, exactly as before.
    full = "ab12cd34-aff7-4b4b-865a-c2f502c1a66e"
    monkeypatch.delenv("JREMOTE_SESSION", raising=False)
    monkeypatch.setenv("TMUX", "/private/tmp/tmux-501/jremote,999,3")
    monkeypatch.setenv("TMUX_PANE", "%7")
    calls = []

    class R:
        returncode = 0
        stdout = "jr-ab12cd34\n"

    monkeypatch.setattr(spawn.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or R())
    monkeypatch.setattr(managed, "open_registry", lambda: {full: {}})
    assert spawn.origin_sid() == full
    assert "%7" in calls[0]
