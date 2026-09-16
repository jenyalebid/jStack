"""desk.py — the dashboard buttons' face of the jRemote app.

A chat button's product is a managed session plus a thread window in the
app: created registered-first (the board row is the visibility), then shown
via a `jremote://session/…` link — `open`ed on the Mac, returned to the page
anywhere else. These tests pin the seams: the link's shape, the
registered-before-open ordering, and the /Applications pin on the Mac open.
"""

import subprocess
import types

import pytest

from conftest import needs_embedding_tree

from jstack_host import desk


# ── thread_url ──

@needs_embedding_tree
def test_thread_url_carries_agent_identity(monkeypatch):
    import lib.agents as agents
    monkeypatch.setattr(agents, "project_dir_to_agent",
                        lambda name: ("nova", "chat"))
    monkeypatch.setattr(agents, "active_agents",
                        lambda: {"nova": {"name": "Nova", "emoji": "⚡"}})
    url = desk.thread_url("sid-1", "/Users/x/Agents/Nova/chat")
    assert url.startswith("jremote://session/sid-1?")
    assert "agent=nova" in url and "name=Nova" in url
    assert "emoji=%E2%9A%A1" in url


@needs_embedding_tree
def test_thread_url_bare_for_non_agent_dirs(monkeypatch):
    import lib.agents as agents
    monkeypatch.setattr(agents, "project_dir_to_agent", lambda name: None)
    assert desk.thread_url("sid-2", "/tmp/somewhere") == "jremote://session/sid-2"
    assert desk.thread_url("sid-3") == "jremote://session/sid-3"


# ── create — registered-first, then open, then the board poke ──

def test_create_registers_before_opening(monkeypatch):
    from jstack_host import board_watch, managed
    order = []
    monkeypatch.setattr(desk, "_identity", lambda cwd: ("testy", "Testy", "🧪"))
    monkeypatch.setattr(managed, "record_open",
                        lambda sid, agent, name="": order.append(("record", sid, agent, name)))
    monkeypatch.setattr(managed, "open_managed",
                        lambda sid, cwd, resume=False, extra="", prelude="", **kw:
                        order.append(("open", sid, cwd, resume, extra, prelude)))
    monkeypatch.setattr(board_watch, "poke", lambda: order.append(("poke",)))

    sid = desk.create("/tmp/ws", sid="sid-9", resume=True,
                      extra="--model opus", prelude="X=1 ", name="Chat")
    assert sid == "sid-9"
    assert [o[0] for o in order] == ["record", "open", "poke"]
    assert order[0] == ("record", "sid-9", "testy", "Chat")
    assert order[1] == ("open", "sid-9", "/tmp/ws", True, "--model opus", "X=1 ")


def test_create_mints_a_sid_when_none_given(monkeypatch):
    from jstack_host import board_watch, managed
    monkeypatch.setattr(desk, "_identity", lambda cwd: ("", "", ""))
    monkeypatch.setattr(managed, "record_open", lambda *a, **k: None)
    monkeypatch.setattr(managed, "open_managed", lambda *a, **k: None)
    monkeypatch.setattr(board_watch, "poke", lambda: None)
    sid = desk.create("/tmp/ws")
    assert len(sid) == 36  # a uuid4


# ── open_thread — pinned to /Applications, honest fallback ──

def test_an_unmocked_launch_cannot_reach_the_live_desktop():
    with pytest.raises(AssertionError, match="real desktop app"):
        desk.open_url("jremote://pair?code=test-only&url=http://127.0.0.1:1")


def test_open_thread_pins_the_applications_copy(monkeypatch):
    calls = []
    monkeypatch.setattr(desk.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or
                        types.SimpleNamespace(returncode=0))
    assert desk.open_thread("sid-1") is True
    assert calls == [["open", "-a", desk.APP, "jremote://session/sid-1"]]


def test_open_thread_falls_back_to_launch_services(monkeypatch):
    calls = []
    def run(cmd, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=1 if "-a" in cmd else 0)
    monkeypatch.setattr(desk.subprocess, "run", run)
    assert desk.open_thread("sid-1") is True
    assert calls[1] == ["open", "jremote://session/sid-1"]


def test_open_thread_false_when_nothing_takes_it(monkeypatch):
    monkeypatch.setattr(desk.subprocess, "run",
                        lambda cmd, **kw: (_ for _ in ()).throw(OSError("no open")))
    assert desk.open_thread("sid-1") is False
