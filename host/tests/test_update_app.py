from pathlib import Path

import pytest

from jstack_host import install_host, update_app, update_plugins


def test_disabled_services_are_never_registered_or_unregistered(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(update_app, "control", lambda *args: calls.append(args))
    backend = update_app.AppBackend(tmp_path, {})
    statuses = {"host": "requires_approval", "menu": "not_registered"}
    backend._stop_services(tmp_path, statuses)
    backend._restore_services(tmp_path, statuses)
    assert calls == []


def test_revocation_after_snapshot_is_not_repaired_away(monkeypatch, tmp_path):
    calls = []

    def control(app, action, role=None):
        calls.append((action, role))
        return {"host": "requires_approval", "menu": "requires_approval"}

    monkeypatch.setattr(update_app, "control", control)
    backend = update_app.AppBackend(tmp_path, {})
    backend._restore_services(tmp_path, {"host": "enabled", "menu": "enabled"})
    assert calls == [("status", None), ("status", None)]


def test_apply_refuses_approval_race_before_stopping_anything(monkeypatch, tmp_path):
    monkeypatch.setattr(update_app, "control", lambda *args: {"host": "requires_approval", "menu": "enabled"})
    backend = update_app.AppBackend(tmp_path, {"menubar_path": str(tmp_path)})
    with pytest.raises(ValueError, match="approvals changed"):
        backend.apply({"transaction": {"services": {"host": "enabled", "menu": "enabled"}}})


def test_stop_waits_for_the_real_job_to_disappear(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(update_app, "control", lambda *args: calls.append(args))
    monkeypatch.setattr(install_host, "wait_unloaded", lambda label: False)
    backend = update_app.AppBackend(tmp_path, {})
    with pytest.raises(ValueError, match="has not stopped"):
        backend._stop_services(tmp_path, {"host": "enabled", "menu": "enabled"})
    assert calls == [(tmp_path, "unregister", "menu")]


def test_recovery_restores_an_app_missing_between_renames(monkeypatch, tmp_path):
    app = tmp_path / "Hub.app"
    backup = tmp_path / "Hub.app.previous"
    backup.mkdir()
    (backup / "old-version").write_text("original")
    backend = update_app.AppBackend(tmp_path, {"menubar_path": str(app)})
    restored = []
    monkeypatch.setattr(backend, "_restore_services", lambda *args: restored.append(args))
    monkeypatch.setattr(update_plugins, "rollback", lambda *args: None)
    transaction = {"services": {"host": "enabled", "menu": "not_registered"}, "providers": [],
                   "stack": str(tmp_path), "apps": {
                       "menubar": {"target": str(app), "backup": str(backup)},
                       "client": {"target": str(tmp_path / "client"), "backup": str(tmp_path / "absent"),
                                  "was_running": False}}}
    backend.rollback({"id": "test", "transaction": transaction})
    assert (app / "old-version").read_text() == "original"
    assert restored == [(app, transaction["services"])]


def test_disabled_menu_does_not_need_a_running_process(tmp_path):
    backend = update_app.AppBackend(tmp_path, {})
    assert not backend._running_required("menubar", {}, {"services": {"menu": "requires_approval"}})
    assert backend._running_required("menubar", {}, {"services": {"menu": "enabled"}})
    assert backend.activate_runtime({"state": "current", "verified": True}) is False
