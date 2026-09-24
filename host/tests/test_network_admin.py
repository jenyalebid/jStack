import copy
import hashlib
import json
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace

import pytest

from jstack_host import network_admin as admin


def plan():
    return {"schema": 1, "action": "stage", "transaction": "a" * 32,
            "candidate": "/Applications/jStack Network.app", "candidateSeal": "b" * 64,
            "candidateBinary": "c" * 64,
            "policy": {"owner": 501, "configuration": "/private/network.conf",
                       "address": "10.66.0.1/24", "subnet": "10.66.0.0/24",
                       "nameFile": "/var/run/wireguard/network.name", "forwarding": True, "active": True},
            "legacy": [{"label": "example.network", "sha256": "d" * 64, "mode": 0o644, "group": 0, "loaded": True,
                        "disabled": False, "sources": [{"path": "/private/network.sh", "sha256": "e" * 64, "owner": 501}]}]}


@pytest.mark.parametrize("change", [
    lambda p: p["policy"].update(subnet="10.67.0.0/24"),
    lambda p: p["policy"].update(owner=0),
    lambda p: p["policy"].update(active="true"),
    lambda p: p["legacy"][0].update(disabled=True),
    lambda p: p["legacy"][0].update(loaded=False),
    lambda p: p["legacy"][0].update(sources=[]),
    lambda p: p["legacy"][0].update(mode=0o666),
    lambda p: p["legacy"].append(copy.deepcopy(p["legacy"][0])),
    lambda p: p.update(candidateSeal="unsigned"),
    lambda p: p.update(command="/bin/sh"),
])
def test_malformed_or_off_plan_is_rejected_before_approval(change, monkeypatch, tmp_path):
    request = plan()
    change(request)
    monkeypatch.setattr(admin.os, "geteuid", lambda: 501)
    monkeypatch.setattr(admin.subprocess, "run", lambda *a, **k: pytest.fail("unexpected OS approval"))
    with pytest.raises(ValueError):
        admin.approve(Path(request["candidate"]), request, tmp_path / "private")
    assert not (tmp_path / "private").exists()


def test_a_client_that_still_sends_the_retired_hub_grant_can_still_install():
    """The watchdog the `recovery` field armed is deleted and the installer
    ignores the field. A jRemote client from the era that sends it must not be
    refused on it — that refusal is what strands a machine (#123)."""
    admin.validate_request({**plan(), "recovery": {"bundle": "/Applications/jStack Hub.app",
                                                   "state": "/Users/x/state"}})
    with pytest.raises(ValueError, match="invalid Network staging request"):
        admin.validate_request({**plan(), "unknown": 1})


def test_recovery_cannot_override_protected_plan():
    for action in ("activate", "rollback", "uninstall"):
        request = {"schema": 1, "action": action, "transaction": "a" * 32}
        admin.validate_request(request)
        with pytest.raises(ValueError, match="protected staged"):
            admin.validate_request({**request, "policy": plan()["policy"]})
    admin.validate_request(plan())


def test_bootstrap_quotes_paths_and_checks_copies_before_execution(monkeypatch, tmp_path):
    app = tmp_path / "app '$HOME `touch marker` $(touch marker).app"
    installer = app / "Contents/MacOS/JStackNetworkInstaller"
    installer.parent.mkdir(parents=True)
    installer.write_bytes(b"signed native fixture")
    request = tmp_path / "request ' $(touch marker).json"
    request.write_text("{}")
    observations = []
    monkeypatch.setattr(admin, "protected_ancestry", lambda path: observations.append(("ancestry", path)))
    monkeypatch.setattr(admin.app_services, "verify", lambda *args: observations.append(("verify", args)))
    monkeypatch.setattr(admin, "command", lambda args: observations.append(("command", args)))
    invocation = "a" * 32
    script = admin.bootstrap_script(app, request, invocation)
    lines = script.splitlines()
    assert observations[0] == ("ancestry", admin.ROOT / "invocations")
    assert observations[2][1][:3] == ["/usr/sbin/spctl", "--assess", "--type"]
    assert any(shlex.split(line) == ["/usr/bin/install", "-d", "-o", "root", "-g", "wheel", "-m", "755", str(admin.ROOT.parent)] for line in lines)
    assert sum("0:700:Directory" in line for line in lines) == 2
    assert "check_acl " + shlex.quote(str(admin.ROOT / "invocations")) in lines
    assert shlex.split(lines[-6])[-2] == str(installer)
    assert shlex.split(lines[-5])[-2] == str(request)
    assert hashlib.sha256(installer.read_bytes()).hexdigest() in lines[-4]
    assert hashlib.sha256(request.read_bytes()).hexdigest() in lines[-3]
    assert shlex.split(lines[-2])[:3] == ["/usr/bin/codesign", "--verify", "--strict"]
    protected = admin.ROOT / "invocations" / invocation
    assert shlex.split(lines[-1]) == [str(protected / "Installer"), str(protected / "request.json")]


def test_denied_administrator_approval_does_not_retry(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(admin.os, "geteuid", lambda: 501)
    monkeypatch.setattr(admin, "bootstrap_script", lambda *args: 'echo "quoted"\nexit 1')
    def denied(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=1, stderr="User canceled. (-128)", stdout="")
    monkeypatch.setattr(admin.subprocess, "run", denied)
    with pytest.raises(ValueError, match="User canceled"):
        admin.approve(Path("/Applications/jStack Network.app"), plan(), tmp_path / "private")
    assert len(calls) == 1
    assert calls[0][0] == ["/usr/bin/osascript", "-"]
    assert "with administrator privileges" in calls[0][1]["input"]
    assert '\\"quoted\\"\\nexit 1' in calls[0][1]["input"]


def test_activation_persists_protected_transaction_for_emergency_stop(monkeypatch, tmp_path):
    from jstack_host import service_settings
    request = {"schema": 1, "action": "activate", "transaction": "a" * 32}
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    settings_path = tmp_path / "service-settings.json"
    settings = {"schema": 1, "app": str(tmp_path / "Hub.app"), "port": 9090,
                "environment": {"JREMOTE_STATE_DIR": str(tmp_path / "state")}}
    monkeypatch.setattr(admin.os, "geteuid", lambda: 501)
    monkeypatch.setattr(admin, "bootstrap_script", lambda *args: "approved native fixture")
    monkeypatch.setattr(admin.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stderr="", stdout=json.dumps({"transaction": "a" * 32, "state": "active"})))
    monkeypatch.setattr(service_settings, "read", lambda: dict(settings))
    monkeypatch.setattr(service_settings, "path", lambda: settings_path)
    assert admin.approve(tmp_path / "Network.app", request, private)["state"] == "active"
    saved = json.loads(settings_path.read_text())
    assert saved["network_transaction"] == "a" * 32


def test_bootstrap_refuses_writable_root_ancestry(monkeypatch, tmp_path):
    monkeypatch.setattr(admin, "ROOT", tmp_path)
    monkeypatch.setattr(admin.app_services, "verify", lambda *args: pytest.fail("unsafe staging reached signature check"))
    with pytest.raises(ValueError, match="unprotected"):
        admin.bootstrap_script(tmp_path, tmp_path, "a" * 32)


def test_root_private_ancestor_is_checked_before_unobservable_child(monkeypatch):
    calls = []
    def inspect(path):
        calls.append(str(path))
        if str(path) == "/protected/private/child":
            raise PermissionError("root-private ancestor")
        return SimpleNamespace(st_uid=0, st_mode=0o40700)
    monkeypatch.setattr(Path, "lstat", inspect)
    admin.protected_ancestry(Path("/protected/private/child"))
    assert calls == ["/", "/protected", "/protected/private", "/protected/private/child"]


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS ACLs")
def test_bootstrap_acl_check_runs_before_copied_code(monkeypatch, tmp_path):
    import os
    import pwd
    import subprocess
    app = tmp_path / "candidate.app"
    installer = app / "Contents/MacOS/JStackNetworkInstaller"
    installer.parent.mkdir(parents=True)
    installer.write_bytes(b"fixture")
    request = tmp_path / "request.json"
    request.write_text("{}")
    monkeypatch.setattr(admin, "protected_ancestry", lambda path: None)
    monkeypatch.setattr(admin.app_services, "verify", lambda *args: None)
    monkeypatch.setattr(admin, "command", lambda args: None)
    lines = admin.bootstrap_script(app, request, "a" * 32).splitlines()
    checker = next(line for line in lines if line.startswith("check_acl()"))
    shell = "set -e\n" + checker + '\ncheck_acl "$1"'
    name = pwd.getpwuid(os.getuid()).pw_name
    for grant, expected in (("deny delete", 0), ("allow read", 1), ("allow write", 1)):
        entry = f"user:{name} {grant}"
        subprocess.run(["/bin/chmod", "+a", entry, str(request)], check=True, capture_output=True)
        try:
            result = subprocess.run(["/bin/sh", "-c", shell, "acl-test", str(request)], capture_output=True)
            assert result.returncode == expected
        finally:
            subprocess.run(["/bin/chmod", "-a", entry, str(request)], check=True, capture_output=True)
    assert subprocess.run(["/bin/sh", "-c", shell, "acl-test", str(request)], capture_output=True).returncode == 0
    assert subprocess.run(["/bin/sh", "-c", shell, "acl-test", str(tmp_path / "missing")], capture_output=True).returncode != 0
