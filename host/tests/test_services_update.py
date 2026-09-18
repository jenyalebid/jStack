import json
from pathlib import Path
import plistlib
import shutil

import pytest

from jstack_host import service_settings, services_update as update


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    owner, candidate = tmp_path / "Services.app", tmp_path / "Candidate.app"
    roles = {"updater": "live.jstack.hub.updater", "worker": "live.jstack.automation.worker"}
    for app, version in ((owner, "old"), (candidate, "new")):
        (app / "Contents/_CodeSignature").mkdir(parents=True)
        (app / "Contents/_CodeSignature/CodeResources").write_text(version)
        resources = app / "Contents/Resources"
        resources.mkdir()
        (resources / "services.json").write_text(json.dumps({k: v + ".plist" for k, v in roles.items()}))
        (resources / "automation-catalog.json").write_text('{"worker":"same reviewed digest"}')
        agents = app / "Contents/Library/LaunchAgents"
        agents.mkdir(parents=True)
        for label in roles.values():
            (agents / (label + ".plist")).write_bytes(plistlib.dumps({"Label": label}))
    settings = {"services_app": str(owner), "app": str(tmp_path / "Hub.app"),
                "migration_dir": str(tmp_path / "private"), "environment": {"SECRET_FIXTURE": "never copy me"}}
    monkeypatch.setattr(service_settings, "read", lambda: settings)
    monkeypatch.setattr(update, "approved_app", lambda _: None)
    monkeypatch.setattr(update.os, "geteuid", lambda: 501)
    monkeypatch.setattr(update, "command", lambda argv: shutil.copytree(argv[1], argv[2]))
    monkeypatch.setattr(update, "wait_unloaded", lambda _: True)
    states, calls = {"updater": "enabled", "worker": "not_registered"}, []
    monkeypatch.setattr(update, "loaded", lambda label: states[label.rsplit(".", 1)[1]] == "enabled")

    def control(app, action, role=None):
        if action == "status":
            return dict(states)
        calls.append((action, role))
        states[role] = "enabled" if action == "register" else "not_registered"
        return {"status": states[role]}

    monkeypatch.setattr(update, "control", control)
    return owner, candidate, states, calls, settings


def test_update_and_rollback_preserve_off_and_private_settings(fixture):
    owner, candidate, states, calls, _ = fixture
    original, replacement = update.seal(owner), update.seal(candidate)
    journal = update.prepare(candidate)
    assert "never copy me" not in journal.read_text()
    assert journal.stat().st_mode & 0o777 == 0o600
    update.apply(journal)
    assert update.seal(owner) == replacement
    assert states == {"updater": "enabled", "worker": "not_registered"}
    update.rollback(journal)
    assert update.seal(owner) == original
    assert all(role != "worker" for _, role in calls)
    states["updater"] = "not_registered"
    calls.clear()
    update.rollback(journal)
    assert calls == [] and states["updater"] == "not_registered"


def test_denial_and_explicit_off_after_update_survive_rollback(fixture):
    owner, candidate, states, calls, _ = fixture
    journal = update.prepare(candidate)
    update.apply(journal)
    states["updater"] = "requires_approval"
    calls.clear()
    update.rollback(journal)
    assert not calls
    assert states["updater"] == "requires_approval"


@pytest.mark.parametrize("change", ["unknown", "settings", "candidate", "approval"])
def test_changed_preconditions_refuse_before_stopping(fixture, change):
    owner, candidate, states, calls, settings = fixture
    journal = update.prepare(candidate)
    if change == "unknown":
        states["worker"] = "unknown"
    elif change == "settings":
        settings["environment"]["SECRET_FIXTURE"] = "changed"
    elif change == "candidate":
        (journal.parent / "candidate.app/Contents/_CodeSignature/CodeResources").write_text("tamper")
    else:
        states["updater"] = "requires_approval"
    with pytest.raises(ValueError):
        update.apply(journal)
    assert calls == []


@pytest.mark.parametrize("boundary", ["before_old_rename", "after_old_rename", "after_new_rename"])
def test_interrupted_replacement_rolls_back_from_durable_original(fixture, monkeypatch, boundary):
    owner, candidate, states, calls, _ = fixture
    original = update.seal(owner)
    journal = update.prepare(candidate)
    replace = update.os.replace

    def interrupt(source, target):
        source, target = Path(source), Path(target)
        if source == owner and boundary == "before_old_rename":
            raise RuntimeError("power loss")
        result = replace(source, target)
        if (source == owner and boundary == "after_old_rename" or
                target == owner and boundary == "after_new_rename"):
            raise RuntimeError("power loss")
        return result

    monkeypatch.setattr(update.os, "replace", interrupt)
    with pytest.raises(RuntimeError, match="power loss"):
        update.apply(journal)
    assert json.loads(journal.read_text())["state"] == "applying"
    monkeypatch.setattr(update.os, "replace", replace)
    update.rollback(journal)
    assert update.seal(owner) == original
    assert states == {"updater": "enabled", "worker": "not_registered"}


def test_changed_capability_catalog_requires_separate_migration(fixture):
    _, candidate, _, calls, _ = fixture
    (candidate / "Contents/Resources/automation-catalog.json").write_text('{}')
    with pytest.raises(ValueError, match="private capabilities"):
        update.prepare(candidate)
    assert not calls
