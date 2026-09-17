import copy
import hashlib
from pathlib import Path
import shlex
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
            "legacy": [{"label": "example.network", "sha256": "d" * 64, "loaded": True,
                        "disabled": False, "sources": [{"path": "/private/network.sh", "sha256": "e" * 64, "owner": 501}]}]}


@pytest.mark.parametrize("change", [
    lambda p: p["policy"].update(subnet="10.67.0.0/24"),
    lambda p: p["policy"].update(owner=0),
    lambda p: p["policy"].update(active="true"),
    lambda p: p["legacy"][0].update(disabled=True),
    lambda p: p["legacy"][0].update(loaded=False),
    lambda p: p["legacy"][0].update(sources=[]),
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
    assert shlex.split(lines[2]) == ["/usr/bin/install", "-d", "-o", "root", "-g", "wheel", "-m", "755", str(admin.ROOT.parent)]
    assert shlex.split(lines[5])[-2] == str(installer)
    assert shlex.split(lines[6])[-2] == str(request)
    assert hashlib.sha256(installer.read_bytes()).hexdigest() in lines[7]
    assert hashlib.sha256(request.read_bytes()).hexdigest() in lines[8]
    assert shlex.split(lines[9])[:3] == ["/usr/bin/codesign", "--verify", "--strict"]
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


def test_bootstrap_refuses_writable_root_ancestry(monkeypatch, tmp_path):
    monkeypatch.setattr(admin, "ROOT", tmp_path)
    monkeypatch.setattr(admin.app_services, "verify", lambda *args: pytest.fail("unsafe staging reached signature check"))
    with pytest.raises(ValueError, match="unprotected"):
        admin.bootstrap_script(tmp_path, tmp_path, "a" * 32)
