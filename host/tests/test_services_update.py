import json
from pathlib import Path
import plistlib
import shutil

import pytest

from jstack_host import service_settings, services_update as update, services_handoff as handoff


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
    with pytest.raises(ValueError, match="catalog is incomplete"):
        update.prepare(candidate)
    assert not calls


def test_public_owner_without_optional_catalog_can_be_maintained(fixture):
    owner, candidate, states, calls, _ = fixture
    for app in (owner, candidate):
        resources = app / "Contents/Resources"
        (resources / "services.json").write_text(json.dumps({"updater": "live.jstack.hub.updater.plist"}))
        (resources / "automation-catalog.json").unlink()
    states.pop("worker")
    journal = update.prepare(candidate)
    update.apply(journal)
    update.rollback(journal)
    assert states == {"updater": "enabled"}


def test_missing_private_catalog_is_not_treated_as_empty(fixture):
    owner, candidate, states, calls, _ = fixture
    (candidate / "Contents/Resources/automation-catalog.json").unlink()
    with pytest.raises(FileNotFoundError):
        update.prepare(candidate)
    assert not calls


@pytest.fixture
def handoff_fixture(fixture, monkeypatch):
    monkeypatch.setattr(handoff.app_services, "verify", lambda _: None)
    registrations = []
    controller = {"state": "not_registered"}

    def control(app, action, role=None):
        if action == "status":
            return {handoff.ROLE: controller["state"]}
        registrations.append(role)
        controller["state"] = "enabled"
        return {"status": "enabled"}

    monkeypatch.setattr(handoff, "control", control)
    return fixture, registrations


def test_handoff_runs_independently_and_preserves_off(handoff_fixture):
    (owner, candidate, states, calls, _), registrations = handoff_fixture
    expected = update.seal(candidate)
    request = handoff.submit(candidate)
    assert registrations == [handoff.ROLE]
    assert "never copy me" not in handoff.request_path().read_text()
    assert handoff.reconcile()["state"] == "updated"
    assert update.seal(owner) == expected
    assert states["worker"] == "not_registered"
    calls.clear()
    assert handoff.reconcile()["id"] == request["id"]
    assert not calls


def test_completed_handoff_can_request_exact_rollback(handoff_fixture):
    (owner, candidate, states, _, _), _ = handoff_fixture
    original = update.seal(owner)
    request = handoff.submit(candidate, request_id="b" * 32)
    assert request["id"] == "b" * 32
    assert handoff.reconcile()["state"] == "updated"
    assert handoff.request_rollback(request["id"])["state"] == "rollback_pending"
    assert handoff.reconcile()["state"] == "rolled_back"
    assert update.seal(owner) == original
    assert states == {"updater": "enabled", "worker": "not_registered"}


def test_handoff_rollback_requires_matching_identity(handoff_fixture):
    (_, candidate, _, _, _), _ = handoff_fixture
    request = handoff.submit(candidate)
    handoff.reconcile()
    with pytest.raises(ValueError, match="does not match"):
        handoff.request_rollback("0" * 32)
    assert json.loads(handoff.request_path().read_text())["id"] == request["id"]


def test_handoff_recovers_killed_worker_after_unregister(handoff_fixture, monkeypatch):
    (owner, candidate, states, calls, _), _ = handoff_fixture
    expected = update.seal(owner)
    handoff.submit(candidate)
    replace = update.replace
    monkeypatch.setattr(update, "replace", lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit(9)))
    with pytest.raises(SystemExit):
        handoff.reconcile()
    assert states["updater"] == "not_registered"
    monkeypatch.setattr(update, "replace", replace)
    assert handoff.reconcile()["state"] == "rolled_back"
    assert states == {"updater": "enabled", "worker": "not_registered"}
    assert update.seal(owner) == expected


def test_handoff_recovers_prepare_before_request_write(handoff_fixture):
    (_, candidate, _, _, _), _ = handoff_fixture
    request = handoff.submit(candidate)
    update.prepare(candidate, transaction_name="services-update-" + request["id"])
    assert handoff.reconcile()["state"] == "updated"


def test_handoff_rejects_changed_candidate_without_stopping(handoff_fixture):
    (_, candidate, _, calls, _), _ = handoff_fixture
    handoff.submit(candidate)
    (candidate / "Contents/_CodeSignature/CodeResources").write_text("different signed candidate")
    with pytest.raises(ValueError, match="candidate changed"):
        handoff.reconcile()
    assert not calls
    assert json.loads(handoff.request_path().read_text())["state"] == "failed"


def test_handoff_never_overwrites_active_request(handoff_fixture):
    (_, candidate, _, _, _), registrations = handoff_fixture
    request = handoff.submit(candidate)
    with pytest.raises(ValueError, match="unfinished"):
        handoff.submit(candidate)
    assert json.loads(handoff.request_path().read_text())["id"] == request["id"]
    assert len(registrations) == 1


@pytest.mark.parametrize("state", ["requires_approval", "unknown", None])
def test_handoff_respects_denied_or_unobservable_controller(handoff_fixture, monkeypatch, state):
    (_, candidate, _, calls, _), _ = handoff_fixture
    monkeypatch.setattr(handoff, "control", lambda *args: {handoff.ROLE: state})
    with pytest.raises(ValueError, match="unavailable or denied"):
        handoff.submit(candidate)
    assert not calls and not handoff.request_path().exists()


def test_named_transaction_refuses_path_traversal(fixture):
    _, candidate, _, calls, _ = fixture
    with pytest.raises(ValueError, match="identifier"):
        update.prepare(candidate, transaction_name="../elsewhere")
    assert not calls


def test_handoff_registration_failure_cancels_request(handoff_fixture, monkeypatch):
    (_, candidate, _, calls, _), _ = handoff_fixture

    def control(app, action, role=None):
        if action == "status":
            return {handoff.ROLE: "not_registered"}
        raise OSError("registration response lost")

    monkeypatch.setattr(handoff, "control", control)
    with pytest.raises(OSError):
        handoff.submit(candidate)
    assert handoff.reconcile()["state"] == "failed"
    assert not calls


def test_handoff_checks_installed_owner_after_completed_journal(handoff_fixture):
    (owner, candidate, _, _, _), _ = handoff_fixture
    request = handoff.submit(candidate)
    journal = update.prepare(candidate, transaction_name="services-update-" + request["id"])
    update.apply(journal)
    (owner / "Contents/_CodeSignature/CodeResources").write_text("different signed app")
    with pytest.raises(ValueError, match="differs from the handoff outcome"):
        handoff.reconcile()
    assert json.loads(handoff.request_path().read_text())["state"] == "applying"
