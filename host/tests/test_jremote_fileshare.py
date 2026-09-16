"""Selected-folder SMB: one observed contract from CLI through API and audit."""

import asyncio
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jstack_host import cli, doctor, fileshare, router


def _done(argv, code=0, out="", err=""):
    return subprocess.CompletedProcess(argv, code, out, err)


def _machine(tmp_path, monkeypatch, *, actual=None, account=False,
             guest=False, service=True, acl=False, smb_hash=True):
    root = tmp_path / "stack"
    for name in fileshare.SHARE_NAMES:
        (root / name).mkdir(parents=True)
    monkeypatch.setattr(fileshare, "_stack_root", lambda: root)
    monkeypatch.setattr(fileshare, "_root_owner", lambda path: "hostuser")
    actual = actual or {}

    def run(argv):
        if argv[:3] == [fileshare.SHARING, "-l", "-f"]:
            import json
            return _done(argv, out=json.dumps(actual))
        if argv[0] == fileshare.LS:
            text = "drwxr-xr-x+ root\n"
            if acl:
                text += f" 0: user:{fileshare.ACCOUNT} allow {','.join(fileshare.ACL_RIGHTS)}\n"
                text += f" 1: user:hostuser allow {','.join(fileshare.ACL_RIGHTS)}\n"
            return _done(argv, out=text)
        if argv[0] == fileshare.DSCL:
            if account:
                return _done(argv, out=(f"NFSHomeDirectory: {fileshare.ACCOUNT_HOME}\n"
                                        f"UserShell: {fileshare.ACCOUNT_SHELL}\n"
                                        "UniqueID: 502\n"))
            return _done(argv, code=40, err="not found")
        if argv[0] == fileshare.DSEDITGROUP:
            return _done(argv, code=1, out="no jstackshare is NOT a member of admin")
        if argv[0] == fileshare.PWPOLICY:
            return _done(argv, out="SMB-NT\n" if smb_hash else
                         "SALTED-SHA512-PBKDF2\n")
        if argv[0] == fileshare.SYSADMINCTL:
            word = "enabled" if guest else "disabled"
            return _done(argv, err=f"SMB guest access {word}.")
        if argv[0] == fileshare.LAUNCHCTL:
            if argv[1:3] == ["print", "system/com.apple.smbd"]:
                return _done(argv, code=0 if service else 113)
            word = "enabled" if service else "disabled"
            return _done(argv, out=f'"com.apple.smbd" => {word}\n')
        raise AssertionError(argv)

    monkeypatch.setattr(fileshare, "_run", run)
    monkeypatch.setattr(fileshare, "_supported", lambda: True)
    return root


def _share(path, *, guest=0, read_only=0, sealed=1, shared=1):
    return {"path": str(path), "smb_guest_access": guest,
            "smb_read_only": read_only, "smb_sealed": sealed,
            "smb_shared": shared}


def test_status_rejects_home_and_accepts_exact_selected_roots(tmp_path, monkeypatch):
    root = tmp_path / "stack"
    actual = {name: _share(root / name) for name in fileshare.SHARE_NAMES}
    actual["home"] = _share(root, guest=1, sealed=0)
    _machine(tmp_path, monkeypatch, actual=actual, account=True, acl=True)

    observed = fileshare.status()

    assert [s["name"] for s in observed["shares"]] == list(fileshare.SHARE_NAMES)
    assert observed["unexpected"] == [{"name": "home", "path": str(root)}]
    assert observed["configured"] is True
    assert observed["secure"] is False
    assert observed["ready"] is False


def test_status_ready_means_every_security_property_was_observed(tmp_path, monkeypatch):
    root = tmp_path / "stack"
    actual = {name: _share(root / name) for name in fileshare.SHARE_NAMES}
    _machine(tmp_path, monkeypatch, actual=actual, account=True, acl=True)

    observed = fileshare.status()

    assert observed["unexpected"] == []
    assert observed["account"]["ok"] is True
    assert observed["guest_enabled"] is False
    assert observed["secure"] is True
    assert observed["ready"] is True


def test_setup_plan_is_allowlist_and_dry_run(tmp_path, monkeypatch):
    root = _machine(tmp_path, monkeypatch, actual={
        "jarvis": _share(tmp_path / "stack", guest=1, sealed=0),
        "Public": _share(tmp_path / "stack" / "Public", guest=1, sealed=0),
    }, guest=True)

    result = fileshare.setup()
    commands = result["commands"]

    assert result["applied"] is False
    assert any("-addUser jstackshare" in c and "-password -" in c for c in commands)
    assert any("pwpolicy -u jstackshare -sethashtypes SMB-NT on" in c
               for c in commands)
    assert any("dscl . -passwd /Users/jstackshare" in c for c in commands)
    assert any("sharing -r jarvis" in c for c in commands)
    assert any("sharing -r Public" in c for c in commands)
    for name in fileshare.SHARE_NAMES:
        assert any(f"sharing -a {root / name}" in c and "-g 000" in c and
                   "-E 1" in c for c in commands)
        assert any("chmod +a" in c and str(root / name) in c for c in commands)
        assert any("user:hostuser allow" in c and str(root / name) in c
                   for c in commands)
    assert any("-smbGuestAccess off" in c for c in commands)


def test_setup_refuses_to_repurpose_an_existing_account(tmp_path, monkeypatch):
    _machine(tmp_path, monkeypatch)
    bad = fileshare.status()
    bad["account"] = {"exists": True, "ok": False}
    with pytest.raises(fileshare.FileShareError, match="refusing to repurpose"):
        fileshare.setup_plan(bad)


def test_existing_account_without_smb_hash_is_rearmed(tmp_path, monkeypatch):
    root = tmp_path / "stack"
    actual = {name: _share(root / name) for name in fileshare.SHARE_NAMES}
    _machine(tmp_path, monkeypatch, actual=actual, account=True, acl=True,
             smb_hash=False)
    commands = fileshare.setup_plan()
    assert [fileshare.PWPOLICY, "-u", fileshare.ACCOUNT, "-sethashtypes",
            "SMB-NT", "on"] in commands
    assert [fileshare.DSCL, ".", "-passwd", f"/Users/{fileshare.ACCOUNT}"] in commands


def test_acl_accepts_macos_directory_rights_normalization(tmp_path, monkeypatch):
    root = _machine(tmp_path, monkeypatch)
    text = ("drwxr-xr-x+ root\n 0: user:jstackshare allow " +
            ",".join(fileshare.ACL_OBSERVED_RIGHTS) + "\n")
    monkeypatch.setattr(fileshare, "_run", lambda argv: _done(argv, out=text))
    assert fileshare._acl_ok(root / "Agents") is True


def test_audit_alerts_once_per_distinct_unsafe_state(monkeypatch):
    alerts = []
    states = [
        {"unexpected": [{"name": "home", "path": "/Users/me"}]},
        {"unexpected": [{"name": "home", "path": "/Users/me"}]},
        {"unexpected": []},
    ]
    monkeypatch.setattr(fileshare, "status", lambda: states.pop(0))
    monkeypatch.setattr(fileshare.hostenv, "security_alert", alerts.append)
    monkeypatch.setattr(fileshare, "_last_alert", "")

    fileshare.audit_once()
    fileshare.audit_once()
    fileshare.audit_once()

    assert len(alerts) == 1
    assert "home=/Users/me" in alerts[0]


def test_secure_configuration_is_not_ready_until_service_is_loaded(tmp_path, monkeypatch):
    root = tmp_path / "stack"
    actual = {name: _share(root / name) for name in fileshare.SHARE_NAMES}
    _machine(tmp_path, monkeypatch, actual=actual, account=True, acl=True,
             service=False)
    observed = fileshare.status()
    assert observed["secure"] is True
    assert observed["ready"] is False


def test_router_lifespan_runs_and_cancels_the_package_audit(monkeypatch):
    events = []

    async def watch():
        events.append("started")
        try:
            await asyncio.Event().wait()
        finally:
            events.append("stopped")

    monkeypatch.setattr(fileshare, "audit_loop", watch)
    app = FastAPI()
    app.include_router(router.router)
    with TestClient(app):
        assert events == ["started"]
    assert events == ["started", "stopped"]


def test_authenticated_status_route_and_feature_probe(tmp_path, monkeypatch):
    root = tmp_path / "stack"
    actual = {name: _share(root / name) for name in fileshare.SHARE_NAMES}
    _machine(tmp_path, monkeypatch, actual=actual, account=True, acl=True)
    monkeypatch.setattr(router, "require_token", lambda: "device")

    app = FastAPI()
    app.include_router(router.router)
    route = next(r for r in app.routes if r.path == "/api/jremote/v1/files/share")
    assert route.endpoint()["ready"] is True
    assert router._probe("file_sharing") is True


def test_doctor_fails_an_enabled_server_with_undeclared_shares(monkeypatch):
    monkeypatch.setattr(fileshare, "status", lambda: {
        "available": True, "configured": False, "ready": False,
        "unexpected": [{"name": "home", "path": "/Users/me"}],
        "service_enabled": True,
    })
    result = doctor.check_file_sharing()
    assert result["grade"] == doctor.FAIL
    assert "home" in result["detail"]


def test_cli_files_subcommands_dispatch(monkeypatch, capsys):
    monkeypatch.setattr(fileshare, "status", lambda: {"ready": True})
    assert cli.main(["files", "status"]) == 0
    assert '"ready": true' in capsys.readouterr().out
