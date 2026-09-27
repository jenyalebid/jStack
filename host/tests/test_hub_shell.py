"""hub_shell: the hub's own bare-shell tmux process — reachable by any
client, and invisible to `managed.reconcile()`'s reaper by construction.

Staying off the `jr-` prefix is the whole point: a bare shell with no agent
in it is precisely the debris `reconcile` exists to remove (see its own
docstring), and a login shell caught under that prefix would be silently
closed on every dashboard restart past the 60s grace.

Runs against a real tmux server on a throwaway socket, never the live
`jremote` one — same convention as `test_jremote_window_invariant.py`.
"""

import shutil
import subprocess

import pytest

from jstack_host import hub_shell, managed

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
