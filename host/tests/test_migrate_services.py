import json
from pathlib import Path
import plistlib
from types import SimpleNamespace

import pytest

from jstack_host import install_host, migrate_services as migration, service_catalog


@pytest.fixture
def lab(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app = tmp_path / "Services.app"
    resources = app / "Contents/Resources"
    resources.mkdir(parents=True)
    job = {"Label": "test.legacy", "ProgramArguments": ["/bin/sleep", "300"], "RunAtLoad": True}
    catalog = {"probe": job}
    (resources / "automation-catalog.json").write_text(json.dumps(service_catalog.definitions(catalog)[1]))
    path = install_host.plist_path(job["Label"])
    path.parent.mkdir(parents=True)
    path.write_bytes(plistlib.dumps(job))
    loaded = {job["Label"]}
    statuses = {"probe": "not_registered"}
    calls = []
    disabled = set()

    def control(app, action, role=None):
        calls.append((action, role))
        if action == "status":
            return dict(statuses)
        if action == "legacy-status":
            return {"status": "enabled"}
        if action == "register":
            statuses[role] = "enabled"
            loaded.add("live.jstack.automation." + role)
        elif action == "unregister":
            statuses[role] = "not_registered"
            loaded.discard("live.jstack.automation." + role)
        return {"status": statuses[role]}

    def launchctl(*args):
        calls.append(args)
        if args[0] == "bootout":
            loaded.discard(args[1].split("/")[-1])
        return SimpleNamespace(returncode=0, stdout="")

    def bootstrap(label, path):
        loaded.add(label)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(migration, "approved_app", lambda app: None)
    monkeypatch.setattr(migration, "disabled_labels", lambda: disabled)
    monkeypatch.setattr(migration, "control", control)
    monkeypatch.setattr(install_host, "is_loaded", lambda label: label in loaded)
    monkeypatch.setattr(install_host, "wait_unloaded", lambda label: label not in loaded)
    monkeypatch.setattr(install_host, "_launchctl", launchctl)
    monkeypatch.setattr(install_host, "bootstrap", bootstrap)
    return SimpleNamespace(app=app, catalog=catalog, path=path, loaded=loaded, statuses=statuses,
                           calls=calls, disabled=disabled)


def test_exact_migration_and_reversible_rollback(lab):
    before = lab.path.read_bytes()
    journal = migration.prepare(lab.app, lab.catalog)
    migration.apply(journal)
    assert not lab.path.exists()
    assert lab.loaded == {"live.jstack.automation.probe"}
    assert migration.load(journal)["state"] == "migrated"
    assert (journal / "probe.original.plist").read_bytes() == before
    migration.rollback(journal)
    assert lab.path.read_bytes() == before
    assert lab.loaded == {"test.legacy"}


def test_disabled_original_is_not_enabled_under_new_name(lab):
    lab.loaded.clear()
    lab.disabled.add("test.legacy")
    journal = migration.prepare(lab.app, lab.catalog)
    migration.apply(journal)
    assert not lab.loaded
    assert not any(call[0] == "register" for call in lab.calls)
    migration.rollback(journal)
    assert not lab.loaded
    assert lab.path.exists()


def test_changed_same_named_definition_is_not_retired(lab):
    journal = migration.prepare(lab.app, lab.catalog)
    lab.path.write_bytes(plistlib.dumps({**lab.catalog["probe"], "RunAtLoad": False}))
    with pytest.raises(ValueError, match="changed since preparation"):
        migration.apply(journal)
    assert lab.loaded == {"test.legacy"}
    assert lab.path.exists()


def test_new_denial_does_not_restore_enabled_legacy_at_next_login(lab):
    journal = migration.prepare(lab.app, lab.catalog)
    migration.apply(journal)
    lab.statuses["probe"] = "requires_approval"
    lab.loaded.clear()
    migration.rollback(journal)
    assert not lab.path.exists()
    assert not lab.loaded
    assert migration.load(journal)["state"] == "approval_required"
    assert migration.load(journal)["held"] == ["probe"]


def test_changed_catalog_is_rejected_before_any_startup_change(lab):
    lab.catalog["probe"]["ProgramArguments"] = ["/bin/echo", "changed"]
    with pytest.raises(ValueError, match="differs from signed"):
        migration.prepare(lab.app, lab.catalog)
    assert lab.loaded == {"test.legacy"}
    assert lab.calls == []


def test_interrupted_migration_requires_recovery_not_blind_retry(lab):
    journal = migration.prepare(lab.app, lab.catalog)
    migration.apply(journal)
    with pytest.raises(ValueError, match="already attempted"):
        migration.apply(journal)
