"""Desk-side managed spawns (jstack_host.spawn) and the board's view of
briefed sessions.

A handoff/splitoff invoked from inside a managed session lands managed too —
sid and title registered up front — instead of as a raw Mac-only window the
board can only show as an anonymous `pid-` placeholder. And a briefing
injected via `--append-system-prompt` is content: it lives in the process's
argv, never the transcript, so the pristine reap must ask the process before
calling a session empty.
"""

import json

import pytest

import jstack_host.board as board
import jstack_host.managed as managed
import jstack_host.spawn as spawn
import jstack_host.procscan as procscan


# ── the shell parts the spawn CLI hands open_managed ────────────────────────

def test_prompt_file_becomes_read_then_delete_idiom():
    prelude, extra = spawn.build_shell_parts("", "/tmp/brief file.md", "")
    assert prelude == "__JR_SP=\"$(cat '/tmp/brief file.md')\" && rm -f '/tmp/brief file.md' && "
    assert extra == '--append-system-prompt "$__JR_SP"'


def test_name_is_shell_quoted():
    _, extra = spawn.build_shell_parts("Handoff · reply engine", "", "")
    assert extra == "--name 'Handoff · reply engine'"


def test_claude_args_splice_verbatim():
    prelude, extra = spawn.build_shell_parts("T", "/tmp/b", '"Audit session."')
    assert extra.endswith('"Audit session."')
    assert "--append-system-prompt" in extra and "--name T" in extra
    assert prelude.startswith('__JR_SP=')


def test_first_prompt_is_positional_and_last():
    """`claude [options] [prompt]` — the prompt is positional, so anything
    appended after it would be read as part of the prompt rather than a flag.
    It is quoted, not spliced like claude_args: this string is user prose."""
    _, extra = spawn.build_shell_parts("T", "/tmp/b", "--foo",
                                       first_prompt="it's \"live\" now")
    assert extra.endswith(""" 'it'"'"'s "live" now'""")
    assert extra.index("--foo") < extra.index("'it'")


def test_first_prompt_absent_changes_nothing():
    """A spawn that is staged context, not a task, must be byte-identical to
    what it was before the flag existed — every handoff still goes this way."""
    assert spawn.build_shell_parts("T", "/tmp/b", "--foo") == \
        spawn.build_shell_parts("T", "/tmp/b", "--foo", first_prompt="")


def test_inner_command_wraps_prelude_and_extra():
    # `exec` binds the session's life to the claude process (board invariant);
    # the prelude still runs in the shell before it.
    inner = managed._inner_command("sid-1", False, extra='--name X',
                                   prelude='V="$(cat /b)" && ')
    assert inner == ('V="$(cat /b)" && exec claude --session-id sid-1 '
                     '--permission-mode bypassPermissions --name X')
    assert managed._inner_command("sid-2", True) == \
        "exec claude --resume sid-2 --permission-mode bypassPermissions"


def test_agent_base_resolution(tmp_path, monkeypatch):
    """Against a tree this test builds, never the machine's own.

    `agent_base_for` resolves a path through the profile's instance root, so
    a hardcoded `/Users/x/Agents/...` only ever answered on a machine whose
    agents happened to live at that path — which is to say the assertion was
    passing for a reason that had nothing to do with the function.
    """
    from jstack_host import hostenv
    root = tmp_path / "Agents"
    (root / "Nova" / "chat").mkdir(parents=True)
    monkeypatch.setenv("JREMOTE_INSTANCE_ROOT", str(root))
    hostenv.reset_profile()
    assert spawn.agent_base_for(str(root / "Nova" / "chat")) == "nova"
    assert spawn.agent_base_for("/tmp/nowhere") == ""


# ── the registry carries the spawn's title ──────────────────────────────────

@pytest.fixture
def reg(tmp_path, monkeypatch):
    monkeypatch.setattr(managed, "_REG", tmp_path / "jremote_open.json")
    return tmp_path / "jremote_open.json"


def test_record_open_stores_name(reg):
    managed.record_open("s1", "nova", name="Handoff · X")
    managed.record_open("s2", "orin")
    d = json.loads(reg.read_text())
    assert d["s1"] == {"agent": "nova", "name": "Handoff · X"}
    assert d["s2"] == {"agent": "orin"}
    managed.record_close("s1")
    assert "s1" not in json.loads(reg.read_text())


def test_registry_survives_interleaved_mutation(reg):
    """Two writers (dashboard + spawn CLI) must not lose each other's rows."""
    managed.record_open("a", "x")
    managed.record_open("b", "y", name="N")
    managed.record_close("a")
    assert json.loads(reg.read_text()) == {"b": {"agent": "y", "name": "N"}}


# ── a briefed session is never pristine ─────────────────────────────────────

def test_briefed_holder_is_not_pristine(monkeypatch, tmp_path):
    monkeypatch.setattr(board, "_CLAUDE_PROJECTS", tmp_path)  # no transcript
    monkeypatch.setattr(board, "_claude_procs",
                        lambda: [{"pid": 77, "session_id": "any-sid",
                                  "briefed": True}])
    assert board.transcript_pristine("any-sid") is False


def test_unbriefed_holder_still_pristine_without_transcript(monkeypatch, tmp_path):
    monkeypatch.setattr(board, "_CLAUDE_PROJECTS", tmp_path)
    monkeypatch.setattr(board, "_claude_procs",
                        lambda: [{"pid": 77, "session_id": "any-sid",
                                  "briefed": False}])
    assert board.transcript_pristine("any-sid") is True


def test_a_correlated_briefing_never_blocks_the_reap(monkeypatch, tmp_path):
    """A raw briefed window carries no sid in argv — only a DIRECT holder may
    claim the briefing, or an unrelated empty session becomes unreapable."""
    monkeypatch.setattr(board, "_CLAUDE_PROJECTS", tmp_path)
    monkeypatch.setattr(board, "_claude_procs",
                        lambda: [{"pid": 77, "session_id": None,
                                  "briefed": True}])
    assert board.transcript_pristine("any-sid") is True


# ── the board names what it can't yet open ──────────────────────────────────

def test_orphan_row_carries_argv_name(monkeypatch):
    monkeypatch.setattr(board, "_window_truth", lambda: ({}, set()))
    procs = [{"pid": 5, "session_id": None, "location": "/tmp/nowhere",
              "tty": "ttys009", "uptime_minutes": 0,
              "window_name": "Handoff · reply engine"}]
    _, orphans = board._proc_scan(procs)
    assert orphans == [{"pid": 5, "pd": None, "label": "nowhere",
                        "name": "Handoff · reply engine",
                        # What the row is, beside what it's called: an orphan
                        # is any running agent CLI the scan can't name, so it
                        # carries the engine it runs and what holds it.
                        "engine": "", "window": True, "tty": True}]


def test_registry_row_carries_spawn_name(monkeypatch):
    monkeypatch.setattr(procscan, "get_claude_processes",
                        lambda: {"processes": []})
    monkeypatch.setattr(board, "_window_truth", lambda: ({}, set()))
    monkeypatch.setattr(board, "_dialog_sids", lambda: set())
    monkeypatch.setattr(board, "active_agents", lambda: {})
    monkeypatch.setattr(managed, "open_registry",
                        lambda: {"s9": {"agent": "", "name": "Handoff · X"}})
    monkeypatch.setattr(managed, "attached_names", lambda: {"jr-s9"})
    import jstack_host.notify as notify
    monkeypatch.setattr(notify, "unread_sids", lambda: set())

    rows = board.active_sessions()

    (row,) = [r for r in rows if r["session_id"] == "s9"]
    assert row["window_name"] == "Handoff · X"
    assert row["managed"] is True and row["open"] is True


# ── the CLI main path — the spawn's product is a jRemote thread window ──────

def _run_main(monkeypatch, tmp_path, app_exists=True, open_ok=True, argv=None,
              route=None):
    """Drive spawn.main with every side effect recorded, none performed.

    `route=None` pins origin_sid to '' (not inside a managed session), so
    the real _route_window short-circuits to "mac" offline — the historical
    path. A route string skips resolution and answers as the dashboard
    would."""
    from jstack_host import desk
    events = []
    monkeypatch.setattr(spawn, "origin_sid", lambda: "")
    if route is not None:
        monkeypatch.setattr(spawn, "_route_window",
                            lambda origin, new_sid, cwd: route)
    monkeypatch.setattr(spawn.Path, "is_dir", lambda self: True)
    real_exists = spawn.Path.exists
    monkeypatch.setattr(spawn.Path, "exists",
                        lambda self: app_exists if str(self) == desk.APP
                        else real_exists(self))
    monkeypatch.setattr(spawn, "agent_base_for", lambda cwd: "testy")
    monkeypatch.setattr(managed, "record_open",
                        lambda sid, agent, name="", **kw: events.append(("record", sid)))
    monkeypatch.setattr(managed, "open_managed",
                        lambda sid, cwd, resume=False, extra="", prelude="", **kw:
                        events.append(("open", sid, resume)))
    monkeypatch.setattr(desk, "open_thread",
                        lambda sid, cwd="": events.append(("window", sid)) or open_ok)
    rc = spawn.main(argv or ["--cwd", str(tmp_path), "--sid", "sid-cli"])
    return rc, events


def test_main_creates_managed_and_opens_app_window(monkeypatch, tmp_path):
    rc, events = _run_main(monkeypatch, tmp_path)
    assert rc == 0
    assert [e[0] for e in events] == ["record", "open", "window"]
    assert all(e[1] == "sid-cli" for e in events)


def test_main_without_the_app_refuses_before_anything_exists(monkeypatch, tmp_path):
    """No app ⇒ exit 75 with no session and the briefing untouched — the
    adapter's raw-window fallback still has everything it needs."""
    rc, events = _run_main(monkeypatch, tmp_path, app_exists=False)
    assert rc == 75
    assert events == []


def test_main_failed_window_keeps_the_session(monkeypatch, tmp_path):
    """The board row is the spawn's visibility — a window that could not open
    is not a reason to end a live, drivable session."""
    rc, events = _run_main(monkeypatch, tmp_path, open_ok=False)
    assert rc == 0
    assert ("open", "sid-cli", False) in events


# ── the window opens where it was driven ────────────────────────────────────
#
# A handoff typed on the iPad must not open a Mac window: main() resolves
# the origin session from the pane's own tmux env, asks the dashboard to
# route the window, and only the "mac" answer (desk-driven, unknown, or any
# failure) opens the desk window.

def test_main_device_route_skips_the_mac_window(monkeypatch, tmp_path):
    rc, events = _run_main(monkeypatch, tmp_path, route="device")
    assert rc == 0
    assert [e[0] for e in events] == ["record", "open"]


def test_main_none_route_creates_quietly(monkeypatch, tmp_path):
    rc, events = _run_main(monkeypatch, tmp_path, route="none")
    assert rc == 0
    assert [e[0] for e in events] == ["record", "open"]


def test_main_mac_route_opens_the_desk_window(monkeypatch, tmp_path):
    rc, events = _run_main(monkeypatch, tmp_path, route="mac")
    assert rc == 0
    assert [e[0] for e in events] == ["record", "open", "window"]


class _Resp:
    def __init__(self, status_code=200, route="device"):
        self.status_code = status_code
        self._route = route

    def json(self):
        return {"route": self._route}


def test_route_window_asks_the_dashboard(monkeypatch):
    calls = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.update(url=url, json=json, headers=headers)
        return _Resp(route="device")

    monkeypatch.setattr(spawn, "_dashboard_post", fake_post)
    monkeypatch.setattr("jstack_host.devices.internal_token",
                        lambda: "tok")
    assert spawn._route_window("orig-sid", "new-sid", "/tmp/x") == "device"
    assert calls["url"].endswith("/sessions/orig-sid/route-spawn")
    assert calls["json"] == {"new_sid": "new-sid", "cwd": "/tmp/x"}
    assert calls["headers"]["Authorization"] == "Bearer tok"


def test_route_window_without_origin_never_calls_out(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no origin — must not call the dashboard")
    monkeypatch.setattr(spawn, "_dashboard_post", boom)
    assert spawn._route_window("", "new-sid", "/tmp/x") == "mac"


def test_route_window_failure_falls_back_to_mac(monkeypatch):
    def down(*a, **k):
        raise OSError("dashboard not running")
    monkeypatch.setattr(spawn, "_dashboard_post", down)
    monkeypatch.setattr("jstack_host.devices.internal_token",
                        lambda: "tok")
    assert spawn._route_window("orig-sid", "new-sid", "/tmp/x") == "mac"


# ── origin resolution — the pane's own tmux env ─────────────────────────────

def _fake_tmux(monkeypatch, name="jr-abc-123", rc=0):
    calls = []

    class R:
        returncode = rc
        stdout = name + "\n"

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return R()

    monkeypatch.setattr(spawn.subprocess, "run", fake_run)
    return calls


def test_origin_sid_resolves_the_full_sid_via_the_registry(monkeypatch):
    # tmux names truncate (`jr-` + sid[:8]) — the registry holds the full sid.
    full = "ab12cd34-aff7-4b4b-865a-c2f502c1a66e"
    monkeypatch.setenv("TMUX", "/private/tmp/tmux-501/jremote,999,3")
    monkeypatch.setenv("TMUX_PANE", "%7")
    calls = _fake_tmux(monkeypatch, name="jr-ab12cd34")
    monkeypatch.setattr(managed, "open_registry", lambda: {full: {"agent": "x"}})
    assert spawn.origin_sid() == full
    assert "%7" in calls[0]


def test_origin_sid_unknown_to_the_registry_is_empty(monkeypatch):
    monkeypatch.setenv("TMUX", "/private/tmp/tmux-501/jremote,999,3")
    monkeypatch.setenv("TMUX_PANE", "%7")
    _fake_tmux(monkeypatch, name="jr-ab12cd34")
    monkeypatch.setattr(managed, "open_registry", lambda: {})
    assert spawn.origin_sid() == ""


def test_origin_sid_outside_tmux_is_empty(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    assert spawn.origin_sid() == ""


def test_origin_sid_on_a_foreign_socket_is_empty(monkeypatch):
    monkeypatch.setenv("TMUX", "/private/tmp/tmux-501/default,999,3")
    monkeypatch.setenv("TMUX_PANE", "%7")
    _fake_tmux(monkeypatch)
    assert spawn.origin_sid() == ""


def test_origin_sid_non_managed_session_is_empty(monkeypatch):
    monkeypatch.setenv("TMUX", "/private/tmp/tmux-501/jremote,999,3")
    monkeypatch.setenv("TMUX_PANE", "%7")
    _fake_tmux(monkeypatch, name="scratch")
    monkeypatch.setattr(managed, "open_registry", lambda: {})
    assert spawn.origin_sid() == ""
