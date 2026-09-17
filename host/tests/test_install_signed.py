from contextlib import nullcontext
import json
import os
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import pytest

from jstack_host import install_signed, service_settings


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    app, recovery, state = tmp_path / "Hub.app", tmp_path / "Services.app", tmp_path / "state"
    settings = tmp_path / "settings/service-settings.json"
    monkeypatch.setattr(service_settings, "path", lambda: settings)
    monkeypatch.setattr(install_signed, "exclusive", nullcontext)
    monkeypatch.setattr(install_signed, "identity", lambda *args:
                        {"sha": "a" * 40, "release": "77-source", "github_repo": "owner/repo", "build": 77})
    monkeypatch.setattr(install_signed, "legacy_present", lambda: False)
    monkeypatch.setattr(install_signed.install_host, "port_answers", lambda _: False)
    statuses = {"host": "not_registered", "menu": "not_registered", "updater": "not_registered"}
    calls = []

    def control(owner, action, role=None):
        calls.append((owner, action, role))
        if action == "register":
            statuses[role] = "enabled"
            return {"status": "enabled"}
        return dict(statuses)

    monkeypatch.setattr(install_signed, "control", control)
    commands = []
    monkeypatch.setattr(install_signed, "command", lambda argv, **kwargs: commands.append(argv) or "")
    return app, recovery, state, statuses, calls, commands


def test_fresh_install_verifies_before_completion_and_repeat_does_not_restart(fresh):
    app, recovery, state, statuses, calls, commands = fresh
    assert install_signed.install(app, recovery, state)["state"] == "installed"
    assert [call[2] for call in calls if call[1] == "register"] == ["host", "updater", "menu"]
    assert commands == [[str(app / "Contents/MacOS/JStackRuntime"), "provision"],
                        [str(app / "Contents/MacOS/JStackRuntime"), "verify-install"]]
    statuses["host"] = "not_registered"
    calls.clear()
    commands.clear()
    result = install_signed.install(app, recovery, state)
    assert result["services"]["host"] == "not_registered"
    assert all(call[1] == "status" for call in calls)
    assert commands == []


def test_fresh_install_holds_denial_without_clearing_or_repeating_registration(fresh, monkeypatch):
    app, recovery, state, statuses, calls, _ = fresh
    original = install_signed.control

    def denied(owner, action, role=None):
        result = original(owner, action, role)
        if action == "register":
            statuses[role] = "requires_approval"
            return {"status": "requires_approval"}
        return result

    monkeypatch.setattr(install_signed, "control", denied)
    assert install_signed.install(app, recovery, state)["state"] == "approval_required"
    calls.clear()
    assert install_signed.install(app, recovery, state)["state"] == "approval_required"
    assert all(call[1] == "status" for call in calls)
    # A supported OS approval can let the transaction continue.
    statuses["host"] = "enabled"
    monkeypatch.setattr(install_signed, "control", original)
    assert install_signed.install(app, recovery, state)["state"] == "installed"


def test_interruption_does_not_repeat_an_ambiguous_start(fresh, monkeypatch):
    app, recovery, state, _, calls, _ = fresh
    original = install_signed.control

    def interrupted(owner, action, role=None):
        if action == "register":
            raise OSError("interrupted before registration")
        return original(owner, action, role)

    monkeypatch.setattr(install_signed, "control", interrupted)
    with pytest.raises(OSError):
        install_signed.install(app, recovery, state)
    calls.clear()
    monkeypatch.setattr(install_signed, "control", original)
    assert install_signed.install(app, recovery, state)["state"] == "stopped"
    assert all(call[1] == "status" for call in calls)


@pytest.mark.parametrize("conflict", ["legacy", "state", "port", "approval"])
def test_fresh_install_rejects_existing_ownership_before_writing(fresh, monkeypatch, conflict):
    app, recovery, state, statuses, calls, commands = fresh
    if conflict == "legacy":
        monkeypatch.setattr(install_signed, "legacy_present", lambda: True)
    elif conflict == "state":
        state.mkdir()
        (state / "host-id").write_text("existing")
    elif conflict == "port":
        monkeypatch.setattr(install_signed.install_host, "port_answers", lambda _: True)
    else:
        statuses["host"] = "requires_approval"
    with pytest.raises(ValueError):
        install_signed.install(app, recovery, state)
    assert not service_settings.path().exists()
    assert not install_signed.journal_path().exists()
    assert not commands
    assert all(call[1] == "status" for call in calls)


def test_resume_checks_settings_and_exact_source(fresh, monkeypatch):
    app, recovery, state, _, _, _ = fresh
    install_signed.install(app, recovery, state)
    monkeypatch.setattr(install_signed, "identity", lambda *args: {"sha": "b" * 40})
    with pytest.raises(ValueError, match="transaction"):
        install_signed.install(app, recovery, state)


def test_failed_api_verification_is_not_installed(fresh, monkeypatch):
    app, recovery, state, _, _, _ = fresh

    def command(argv, **kwargs):
        if argv[-1] == "verify-install":
            raise ValueError("API identity mismatch")
        return ""

    monkeypatch.setattr(install_signed, "command", command)
    with pytest.raises(ValueError, match="identity mismatch"):
        install_signed.install(app, recovery, state)
    assert json.loads(install_signed.journal_path().read_text())["state"] != "installed"


def test_sealed_provisioning_clears_inherited_identity_overrides(monkeypatch, tmp_path):
    expected = str(tmp_path / "installed-state")
    monkeypatch.setattr(service_settings, "read", lambda:
                        {"environment": {"JREMOTE_STATE_DIR": expected, "JREMOTE_HOST_PROFILE": "default"}})
    monkeypatch.setenv("JREMOTE_STATE_DIR", str(tmp_path / "wrong-state"))
    monkeypatch.setenv("JREMOTE_HOST_ID", "wrong-identity")
    monkeypatch.setenv("JREMOTE_TOKEN_PATH", str(tmp_path / "wrong-token"))
    monkeypatch.setattr(sys, "argv", ["JStackRuntime", "provision"])
    monkeypatch.setattr(sys, "path", list(sys.path))
    observed = []
    monkeypatch.setitem(sys.modules, "jstack_host.install_signed", SimpleNamespace(
        provision=lambda: observed.append(dict(os.environ))))
    runtime = Path(install_signed.__file__).parent.parent / "macos/runtime_entry.py"
    runpy.run_path(str(runtime))["main"]()
    assert observed[0]["JREMOTE_STATE_DIR"] == expected
    assert "JREMOTE_HOST_ID" not in observed[0]
    assert "JREMOTE_TOKEN_PATH" not in observed[0]
