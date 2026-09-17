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


def test_embedded_host_stops_through_its_catalog_owner(monkeypatch, tmp_path):
    from jstack_host import app_services
    owner = tmp_path / "Services.app"
    monkeypatch.setattr(app_services, "specification", lambda app, config, role:
                        (owner, "dashboard", "live.jstack.automation.dashboard"))
    calls = []
    monkeypatch.setattr(update_app, "control", lambda *args: calls.append(args))
    waited = []
    monkeypatch.setattr(install_host, "wait_unloaded", lambda label: waited.append(label) or True)
    backend = update_app.AppBackend(tmp_path, {})
    backend._stop_services(tmp_path, {"host": "enabled", "menu": "not_registered"})
    assert calls == [(owner, "unregister", "dashboard")]
    assert waited == ["live.jstack.automation.dashboard"]


@pytest.mark.parametrize("status", ["requires_approval", "not_registered"])
def test_disabled_host_verifies_without_api_but_requires_no_loaded_job(monkeypatch, tmp_path, status):
    backend = update_app.AppBackend(tmp_path, {"menubar_path": str(tmp_path)})
    monkeypatch.setattr(update_plugins, "discover", lambda: {})
    monkeypatch.setattr(update_plugins, "observed", lambda _: {})
    monkeypatch.setattr(backend, "_verify_host", lambda _: pytest.fail("disabled host must stay off"))
    monkeypatch.setattr(backend, "_statuses", lambda _: {"host": status, "menu": "not_registered"})
    monkeypatch.setattr(install_host, "is_loaded", lambda _: False)
    job = {"envelope": {"manifest": {"components": {"stack": {"version": "1"}}}},
           "transaction": {"apps": {}, "services": {"host": status, "menu": "not_registered"}}}
    assert backend.verify(job)
    monkeypatch.setattr(install_host, "is_loaded", lambda _: True)
    assert not backend.verify(job)
    monkeypatch.setattr(install_host, "is_loaded", lambda _: False)
    monkeypatch.setattr(backend, "_statuses", lambda _: {"host": "unknown", "menu": "not_registered"})
    assert not backend.verify(job)


def test_enabled_host_still_requires_authenticated_api(monkeypatch, tmp_path):
    backend = update_app.AppBackend(tmp_path, {})
    monkeypatch.setattr(update_plugins, "discover", lambda: {})
    monkeypatch.setattr(update_plugins, "observed", lambda _: {})
    monkeypatch.setattr(backend, "_verify_host", lambda _: False)
    job = {"envelope": {"manifest": {"components": {"stack": {"version": "1"}}}},
           "transaction": {"apps": {}, "services": {"host": "enabled", "menu": "not_registered"}}}
    assert not backend.verify(job)


@pytest.mark.parametrize("status", ["enabled", "requires_approval"])
def test_missing_main_app_recovery_observes_independent_embedded_owner(monkeypatch, tmp_path, status):
    from jstack_host import app_services
    app, backup, owner = tmp_path / "Hub.app", tmp_path / "Hub.app.previous", tmp_path / "Services.app"
    backup.mkdir()
    (backup / "old-version").write_text("original")
    backend = update_app.AppBackend(tmp_path, {"menubar_path": str(app), "host_capability": "dashboard"})
    monkeypatch.setattr(app_services, "specification", lambda *args:
                        (owner, "dashboard", "live.jstack.automation.dashboard"))
    calls = []

    def control(*args):
        calls.append(args)
        assert not app.exists(), "observe and stop the embedded owner before restoring the app"
        return {"dashboard": status}

    monkeypatch.setattr(update_app, "control", control)
    monkeypatch.setattr(install_host, "wait_unloaded", lambda _: True)
    restored = []
    monkeypatch.setattr(backend, "_restore_services", lambda *args: restored.append(args))
    monkeypatch.setattr(update_plugins, "rollback", lambda *args: None)
    transaction = {"services": {"host": "enabled", "menu": "not_registered"}, "providers": [],
                   "stack": str(tmp_path), "apps": {"menubar": {"target": str(app), "backup": str(backup)}}}
    backend.rollback({"id": "test", "transaction": transaction})
    assert calls[0] == (owner, "status")
    assert ((owner, "unregister", "dashboard") in calls) == (status == "enabled")
    assert restored[0][1]["host"] == status
    assert (app / "old-version").read_text() == "original"


def test_disabled_host_observation_reports_installed_completion_not_running_source(monkeypatch, tmp_path):
    from jstack_host.update_macos import MacBackend
    monkeypatch.setattr(MacBackend, "observe", lambda *_:
                        {"verified": False, "release": None, "host_source": {}})
    backend = update_app.AppBackend(tmp_path, {"menubar_path": str(tmp_path)})
    statuses = {"host": "not_registered", "menu": "not_registered"}
    monkeypatch.setattr(backend, "_statuses", lambda _: statuses)
    monkeypatch.setattr(backend, "verify", lambda _: True)
    job = {"verified": True, "state": "verifying", "release": "77-source",
           "transaction": {"services": statuses}}
    observed = backend.observe(job)
    assert observed["verified"] and observed["release"] == "77-source"
    assert observed["host_source"] == {} and observed["host_running"] is False
    monkeypatch.setattr(backend, "verify", lambda _: False)
    assert not backend.observe(job)["verified"]
