"""A host launched with no locale still reads its own tmux formats.

Work Main's hub runs with no LANG/LC_* at all. Its tmux escaped every tab in a
`-F` format to `_`, `pane_ttys()` parsed no pane, and each managed Codex
session showed twice: its registry row and a `pid-` window card for the same
pane. Driven against a real tmux on a throwaway socket.
"""

import shutil
import subprocess
import uuid

import pytest

from jstack_host import managed

pytestmark = pytest.mark.skipif(not shutil.which(managed._TMUX),
                                reason="tmux not installed")


@pytest.fixture
def bare_locale_server(monkeypatch):
    for var in ("LANG", "LC_ALL", "LC_CTYPE"):
        monkeypatch.delenv(var, raising=False)
    sock = f"jrtest-locale-{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr(managed, "_SOCK", sock)
    name = "jr-0123abcd"
    subprocess.run([managed._TMUX, "-L", sock, "new-session", "-d", "-s", name,
                    "sleep 60"], check=True)
    yield name
    subprocess.run([managed._TMUX, "-L", sock, "kill-server"],
                   capture_output=True)


def test_pane_map_survives_a_host_with_no_locale(bare_locale_server):
    panes = managed.pane_ttys()
    assert list(panes.values()) == [bare_locale_server]
    assert all(tty.startswith("/dev/tty") for tty in panes)
