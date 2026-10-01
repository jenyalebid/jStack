"""The desk capability, and the attribution rule it exists to enforce.

The defect this covers (jStack#239) is not a missing grant. macOS bills a
privacy request to the *responsible process* of the session that made it, so
the same window verb run over ssh asks for a grant on sshd and is denied while
jStack Hub holds the grant the whole time. What has to be pinned, then, is not
"does a window minimize" — it is that every path runs the verb inside the Hub's
own sealed helper, that nothing here ever becomes a shell, and that a refusal
from the wrong session reads as the wrong session rather than as a machine
nobody granted.
"""

import json
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jstack_host import build_hub, cli, doctor, router, windows

SOURCE = Path(__file__).resolve().parents[1] / "macos/Windows.swift"


def _done(code=0, out="", err=""):
    return subprocess.CompletedProcess(["JStackWindows"], code, out, err)


def _helper(monkeypatch, result):
    """Answer as the sealed helper without ever running one."""
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return result(argv) if callable(result) else result

    monkeypatch.setattr(windows, "helper", lambda: Path("/Applications/jStack Hub.app") / windows.HELPER)
    monkeypatch.setattr(subprocess, "run", run)
    return calls


# ── where the verb runs ─────────────────────────────────────────────────────

def test_a_machine_with_no_hub_has_no_permission_holder_to_ask_under(monkeypatch):
    monkeypatch.setattr(windows, "_bundle", lambda: None)
    with pytest.raises(windows.WindowsError) as raised:
        windows.helper()
    assert raised.value.reason == "absent"


def test_a_hub_older_than_the_capability_says_so_rather_than_failing(tmp_path, monkeypatch):
    app = tmp_path / "jStack Hub.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    monkeypatch.setattr(windows, "_bundle", lambda: None)
    monkeypatch.setattr("jstack_host.service_settings.read",
                        lambda: {"app": str(app)})
    with pytest.raises(windows.WindowsError) as raised:
        windows.helper()
    assert raised.value.reason == "absent" and "predates" in str(raised.value)


def test_the_installed_bundle_is_verified_before_its_helper_is_run(tmp_path, monkeypatch):
    app = tmp_path / "jStack Hub.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / windows.HELPER).write_text("#!/bin/sh\n")
    verified = []
    monkeypatch.setattr(windows, "_bundle", lambda: None)
    monkeypatch.setattr("jstack_host.service_settings.read", lambda: {"app": str(app)})
    monkeypatch.setattr("jstack_host.app_services.verify", lambda path: verified.append(path))
    assert windows.helper() == app / windows.HELPER
    assert verified == [app]


def test_a_hub_serving_a_request_asks_its_own_bundle_not_the_settings_file(monkeypatch):
    """The process about to spend the grant and the bundle that holds it are
    the same thing, so the path comes off `__file__`, never off a file that can
    point somewhere else."""
    called = []
    monkeypatch.setattr(windows, "_bundle", lambda: Path("/Applications/jStack Hub.app"))
    monkeypatch.setattr("jstack_host.service_settings.read",
                        lambda: called.append(True) or {})
    assert windows.helper() == Path("/Applications/jStack Hub.app") / windows.HELPER
    assert called == []


# ── what a refusal means ────────────────────────────────────────────────────

@pytest.mark.parametrize("code,reason", sorted(windows.REASONS.items()))
def test_every_helper_exit_code_carries_its_own_kind_of_refusal(monkeypatch, code, reason):
    _helper(monkeypatch, _done(code, err="no"))
    with pytest.raises(windows.WindowsError) as raised:
        windows.listing()
    assert raised.value.reason == reason


def test_an_ssh_session_is_a_misdirected_request_not_a_server_error():
    """421 and not 500: the machine is fine and retrying from that session
    never works, which is exactly what a 5xx invites a caller to do."""
    assert windows.STATUS["misattributed"] == 421
    assert windows.reason_for(421) == "misattributed"
    assert windows.reason_for(412) == "untrusted"


def test_a_helper_that_never_answers_is_a_timeout_not_a_failure(monkeypatch):
    monkeypatch.setattr(windows, "helper", lambda: Path("/x"))

    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 30)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(windows.WindowsError) as raised:
        windows.trust()
    assert raised.value.reason == "timeout"


# ── the vocabulary ──────────────────────────────────────────────────────────

def test_only_the_named_verbs_exist(monkeypatch):
    _helper(monkeypatch, _done(out="{}"))
    for verb in ("osascript", "run", "exec", "close"):
        with pytest.raises(windows.WindowsError) as raised:
            windows.act(verb, 1, 0, "")
        assert raised.value.reason == "usage"


def test_a_verb_carries_the_title_the_caller_read(monkeypatch):
    calls = _helper(monkeypatch, _done(out='{"ok": true}'))
    windows.act("minimize", 42, 1, "J Notes – 16 notes")
    assert calls[0][1:] == ["minimize", "42", "1", "J Notes – 16 notes"]


def test_the_helper_is_never_handed_a_shell(monkeypatch):
    calls = _helper(monkeypatch, _done(out='{"apps": []}'))
    windows.listing()
    windows.set_hidden(7, True)
    for argv in calls:
        assert argv[0].endswith("JStackWindows")
        assert not any("sh" == Path(str(a)).name or ";" in str(a) or "|" in str(a)
                       for a in argv[1:])


# ── the source contract ─────────────────────────────────────────────────────

def test_the_helper_is_compiled_into_the_bundle():
    tree = Path(__file__).resolve().parents[2]
    assert build_hub.swift_executables(tree)["JStackWindows"] == "host/macos/Windows.swift"


def test_the_helper_spawns_nothing_and_scripts_nothing():
    """A capability that can run another program is a remote shell wearing a
    typed name — which is the build that was ruled out, not the one shipped."""
    text = SOURCE.read_text()
    for forbidden in ("NSAppleScript", "osascript", "Process(", "posix_spawn",
                      "NSTask", "system(", "popen"):
        assert forbidden not in text, forbidden


def test_only_the_hub_is_a_permission_holder():
    text = SOURCE.read_text()
    assert 'let HOLDER = "live.jstack.hub"' in text
    # The privileged network daemon runs as root and must never hold a desk
    # grant — it may be named in a comment saying so, never in code.
    code = [line for line in text.splitlines() if not line.strip().startswith("//")]
    assert not [line for line in code if "live.jstack.network" in line]
    assert 'fail(77, "window verbs must never run privileged")' in text


def test_the_prompt_is_raised_in_exactly_one_place():
    """`AXIsProcessTrustedWithOptions` is the call that puts a dialog on
    someone's screen. One occurrence, behind the session guards — an afternoon
    of jStack#239 ended with that call fired over ssh."""
    text = SOURCE.read_text()
    assert text.count("AXIsProcessTrustedWithOptions") == 1
    assert "kAXTrustedCheckOptionPrompt" in text


# ── the doctor rung ─────────────────────────────────────────────────────────

def _trust(monkeypatch, **fields):
    answer = {"trusted": False, "prompted": False, "sealed": True,
              "hub_session": True, "over_ssh": False, "bundle": "live.jstack.hub",
              "ancestry": [{"pid": 2, "path": "/bin/zsh", "bundle": ""}]}
    answer.update(fields)
    monkeypatch.setattr(windows, "trust", lambda: answer)


def test_doctor_is_quiet_on_a_machine_with_no_hub(monkeypatch):
    def absent():
        raise windows.WindowsError("no installed jStack Hub", "absent")

    monkeypatch.setattr(windows, "trust", absent)
    assert doctor.check_windows()["grade"] == doctor.OK


def test_doctor_names_the_hub_when_the_grant_is_held(monkeypatch):
    _trust(monkeypatch, trusted=True)
    result = doctor.check_windows()
    assert result["grade"] == doctor.OK and "jStack Hub holds" in result["detail"]


def test_doctor_sends_the_missing_grant_to_the_one_action_that_asks_for_it(monkeypatch):
    _trust(monkeypatch)
    result = doctor.check_windows()
    assert result["grade"] == doctor.WARN
    assert "does not hold Accessibility" in result["detail"]
    assert "windows authorize" in result["hint"]


def test_doctor_never_reports_a_wrong_session_as_a_missing_grant(monkeypatch):
    """The check that would have cost the least to get wrong. Asked from a
    terminal or an ssh session, `trusted` is false because *that* session holds
    nothing — saying "the Hub does not hold Accessibility" there sends someone
    to re-grant a permission that was never missing."""
    _trust(monkeypatch, hub_session=False,
           ancestry=[{"pid": 9, "path": "/usr/libexec/sshd-keygen-wrapper", "bundle": ""}])
    result = doctor.check_windows()
    assert result["grade"] == doctor.WARN
    assert "does not hold" not in result["detail"]
    assert "sshd-keygen-wrapper" in result["detail"]


def test_doctor_calls_a_grant_held_outside_the_hub_a_second_holder(monkeypatch):
    """Observed on a real managed Mac while this was built: Accessibility
    answered `true` over ssh, because a prompt fired from an ssh session had
    been approved and sshd now held the desk. A green line there is the
    unification running hollow — the grant is real and it is on the wrong
    holder."""
    _trust(monkeypatch, trusted=True, hub_session=False, over_ssh=True,
           ancestry=[{"pid": 9, "path": "/usr/libexec/sshd-session", "bundle": ""}])
    result = doctor.check_windows()
    assert result["grade"] == doctor.WARN
    assert "second permission holder" in result["detail"]
    assert "sshd-session" in result["detail"]


def test_a_minimized_window_is_never_reported_as_gone():
    """Some applications drop a window out of `AXWindows` the moment it is
    minimized — Notes does. `list` carries the Dock's own list beside the
    applications' so a caller cannot read that as the window disappearing."""
    text = SOURCE.read_text()
    assert "AXMinimizedWindowDockItem" in text
    assert '"minimized": dockMinimized()' in text


def test_every_verb_that_suppresses_has_the_verb_that_undoes_it():
    text = SOURCE.read_text()
    for pair in (("minimize", "unminimize"), ("hide", "unhide")):
        assert all(f'"{verb}"' in text for verb in pair)
    assert '("restore", 3)' in text


def test_an_ax_setter_is_never_reported_as_the_outcome():
    """An AX setter returns when the application received the message. Reading
    straight back reported the state *before* the change — on a real desk that
    answered `minimized: false` from the call that minimized the window."""
    text = SOURCE.read_text()
    assert "func settle(" in text
    assert "if settle(wanted, { attribute(window, kAXMinimizedAttribute as String) as? Bool })" in text
    assert "_ = settle(wanted) { NSRunningApplication(processIdentifier: pid)?.isHidden }" in text


def test_restore_is_its_own_route_and_verb(monkeypatch):
    monkeypatch.setattr(windows, "restore", lambda index, title: {"ok": True, "restored": title})
    body = router.WindowRestoreRequest(index=0, title="J Notes")
    assert _route("/windows/restore").endpoint(body)["restored"] == "J Notes"


def test_restore_reaches_the_helper_by_index_and_title(monkeypatch):
    calls = _helper(monkeypatch, _done(out='{"ok": true}'))
    windows.restore(0, "J Notes")
    assert calls[0][1:] == ["restore", "0", "J Notes"]


def test_doctor_lists_the_rung(monkeypatch):
    assert doctor.check_windows in doctor.CHECKS


# ── the routes ──────────────────────────────────────────────────────────────

def _route(path):
    return next(r for r in router.router.routes if r.path == f"/api/jremote/v1{path}")


def test_the_window_routes_are_authenticated():
    app = FastAPI()
    app.include_router(router.router)
    with TestClient(app) as client:
        for path in ("/windows/list", "/windows/trust", "/windows/authorize",
                     "/windows/act", "/windows/restore", "/windows/hide"):
            assert client.post(f"/api/jremote/v1{path}", json={}).status_code == 401


def test_a_route_maps_each_refusal_onto_its_own_status(monkeypatch):
    from fastapi import HTTPException

    for reason, status in windows.STATUS.items():
        def broken():
            raise windows.WindowsError("no", reason)

        monkeypatch.setattr(windows, "listing", broken)
        with pytest.raises(HTTPException) as raised:
            _route("/windows/list").endpoint()
        assert raised.value.status_code == status


def test_the_list_route_answers_what_the_helper_saw(monkeypatch):
    monkeypatch.setattr(windows, "listing", lambda: {"apps": [{"pid": 3}]})
    assert _route("/windows/list").endpoint() == {"apps": [{"pid": 3}]}


def test_the_act_route_refuses_a_verb_outside_the_vocabulary(monkeypatch):
    from fastapi import HTTPException

    body = router.WindowActRequest(verb="quit", pid=1, window=0, title="")
    with pytest.raises(HTTPException) as raised:
        _route("/windows/act").endpoint(body)
    assert raised.value.status_code == windows.STATUS["usage"]


def test_the_parent_asks_the_leafs_hub_and_never_a_shell_on_it(monkeypatch):
    """The whole hub→leaf leg in one assertion: a credential minted on the
    leaf, a POST to the leaf's own window route, and the verb running inside
    the leaf's bundle under the leaf's own grant."""
    posted = {}

    monkeypatch.setattr("jstack_host.store.get_store", lambda: type(
        "S", (), {"host_row": staticmethod(lambda key: {"key": key, "deleted": False})})())
    monkeypatch.setattr("jstack_host.grants.mint_on",
                        lambda row, name, poster=None, owner_id="": {
                            "address": "10.66.0.2", "port": 9090, "token": "t"})

    def poster(url, payload, token):
        posted.update(url=url, payload=payload, token=token)
        return 200, {"apps": []}

    assert windows.on_host("leaf-key", "list", {}, poster=poster) == {"apps": []}
    assert posted["url"] == "http://10.66.0.2:9090/api/jremote/v1/windows/list"
    assert posted["token"] == "t"


def test_an_unknown_machine_is_a_404_not_an_attempt(monkeypatch):
    monkeypatch.setattr("jstack_host.store.get_store", lambda: type(
        "S", (), {"host_row": staticmethod(lambda key: None)})())
    with pytest.raises(windows.WindowsError) as raised:
        windows.on_host("nope", "list", {})
    assert raised.value.reason == "gone"


def test_the_leaf_leg_is_console_only():
    from fastapi import HTTPException

    route = _route("/hosts/{key}/windows")
    body = router.HostWindowRequest(action="list")

    class Request:
        client = type("C", (), {"host": "203.0.113.9"})()
        headers = {}

    with pytest.raises(HTTPException):
        route.endpoint("leaf", body, Request())


# ── the CLI ─────────────────────────────────────────────────────────────────

def test_cli_windows_subcommands_dispatch(monkeypatch, capsys):
    monkeypatch.setattr(windows, "trust", lambda: {"trusted": True})
    args = cli.build_parser().parse_args(["windows", "trust"])
    assert args.fn(args) == 0
    assert json.loads(capsys.readouterr().out)["trusted"] is True


def test_cli_windows_act_passes_the_title_through(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(windows, "act", lambda *a: (seen.update(a=a), {"ok": True})[1])
    args = cli.build_parser().parse_args(["windows", "minimize", "42", "1", "J Notes"])
    assert args.fn(args) == 0
    assert seen["a"] == ("minimize", 42, 1, "J Notes")


def test_cli_reports_a_refusal_as_a_refusal(monkeypatch, capsys):
    def broken():
        raise windows.WindowsError("jStack Hub does not hold Accessibility", "untrusted")

    monkeypatch.setattr(windows, "listing", broken)
    args = cli.build_parser().parse_args(["windows", "list"])
    assert args.fn(args) == 2
    assert "does not hold Accessibility" in capsys.readouterr().err
