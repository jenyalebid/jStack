from pathlib import Path

import pytest

from jstack_host import install_host, update_app, update_plugins


def test_disabled_services_are_never_registered_or_unregistered(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(update_app, "control", lambda *args: calls.append(args))
    backend = update_app.AppBackend(tmp_path, {})
    monkeypatch.setattr(backend, "_definitions", lambda app: {
        "host": "live.jstack.hub.host", "menu": "live.jstack.hub.menu"})
    statuses = {"host": "requires_approval", "menu": "not_registered"}
    backend._stop_services(tmp_path, statuses)
    backend._restore_services(tmp_path, statuses)
    assert calls == []


def test_superseded_native_schema_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="unsupported release schema"):
        update_app.AppBackend(tmp_path, {}).compatible({"schema": 2})


@pytest.mark.parametrize("installed,expected", [
    ("release", "applied"), ("different", "unknown"), (None, "unknown"),
])
def test_recovery_state_is_judged_by_the_installed_artifact(monkeypatch, tmp_path, installed, expected):
    app = tmp_path / "Hub.app"
    backend = update_app.AppBackend(tmp_path, {"menubar_path": str(app)})

    def check(path, component, kind):
        assert path == app and kind == "menubar"
        if installed != "release":
            raise ValueError("installed app differs from the release")

    monkeypatch.setattr(backend, "_check_app", check)
    transaction = {"apps": {"menubar": {"target": str(app)}},
                   "manifest": {"components": {"menubar": {"version": "42"}}}}
    if installed is None:
        transaction["apps"] = {}
    assert backend.recovery_status({"transaction": transaction}) == expected


def test_finalization_discards_the_retained_backup_bundle(tmp_path):
    app = tmp_path / "Hub.app"
    backup = tmp_path / "Hub.app.previous-app-stage-x"
    backup.mkdir()
    (backup / "old-version").write_text("original")
    backend = update_app.AppBackend(tmp_path, {"menubar_path": str(app)})
    job = {"transaction": {"apps": {"menubar": {"target": str(app), "backup": str(backup)}}}}
    backend.finalize(job)
    assert not backup.exists()
    stray = tmp_path / "Unrelated.app"
    stray.mkdir()
    job = {"transaction": {"apps": {"menubar": {"target": str(app), "backup": str(stray)}}}}
    with pytest.raises(ValueError, match="unexpected recovery bundle"):
        backend.finalize(job)
    assert stray.exists()


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


@pytest.mark.parametrize("status,message", [
    ("unknown", "cannot observe"), (None, "cannot observe"),
    ("not_found", "could not be restored"),
])
def test_recovery_never_registers_an_unobservable_service(monkeypatch, tmp_path, status, message):
    calls = []

    def control(app, action, role=None):
        calls.append((action, role))
        return {"host": status}

    monkeypatch.setattr(update_app, "control", control)
    backend = update_app.AppBackend(tmp_path, {})
    with pytest.raises(ValueError, match=message):
        backend._restore_services(tmp_path, {"host": "enabled", "menu": "not_registered"})
    assert calls == [("status", None)]


@pytest.mark.parametrize("method", ["_stop_services", "_restore_services"])
@pytest.mark.parametrize("statuses", [
    {"host": "unknown", "menu": "enabled"},
    {"host": "enabled"},
    {"menu": "enabled"},
])
def test_incomplete_approval_snapshot_refuses_all_mutations(monkeypatch, tmp_path, method, statuses):
    monkeypatch.setattr(update_app, "control", lambda *args: pytest.fail("must refuse before controls"))
    backend = update_app.AppBackend(tmp_path, {})
    with pytest.raises(ValueError, match="cannot observe"):
        getattr(backend, method)(tmp_path, statuses)


def test_stop_waits_for_the_real_job_to_disappear(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(update_app, "control", lambda *args: calls.append(args))
    monkeypatch.setattr(install_host, "wait_unloaded", lambda label: False)
    backend = update_app.AppBackend(tmp_path, {})
    monkeypatch.setattr(backend, "_definitions", lambda app: {
        "host": "live.jstack.hub.host", "menu": "live.jstack.hub.menu"})
    with pytest.raises(ValueError, match="has not stopped"):
        backend._stop_services(tmp_path, {"host": "enabled", "menu": "enabled"})
    assert calls == [(tmp_path, "unregister", "menu")]


def test_recovery_restores_an_app_missing_between_renames(monkeypatch, tmp_path):
    app = tmp_path / "Hub.app"
    backup = tmp_path / "Hub.app.previous"
    backup.mkdir()
    (backup / "old-version").write_text("original")
    backend = update_app.AppBackend(tmp_path, {"menubar_path": str(app)})
    monkeypatch.setattr(backend, "_statuses", lambda _: {"host": "enabled", "menu": "not_registered"})
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


def test_embedded_host_stops_through_the_hubs_own_catalog(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(update_app, "control", lambda *args: calls.append(args))
    waited = []
    monkeypatch.setattr(install_host, "wait_unloaded", lambda label: waited.append(label) or True)
    backend = update_app.AppBackend(tmp_path, {"host_capability": "dashboard"})
    monkeypatch.setattr(backend, "_definitions", lambda app: {
        "host": "live.jstack.hub.host", "menu": "live.jstack.hub.menu",
        "dashboard": "live.jstack.automation.dashboard"})
    backend._stop_services(tmp_path, {"host": "not_registered", "menu": "not_registered",
                                      "dashboard": "enabled"})
    assert calls == [(tmp_path, "unregister", "dashboard")]
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
def test_missing_main_app_recovery_reobserves_after_restoring_the_bundle(monkeypatch, tmp_path, status):
    app, backup = tmp_path / "Hub.app", tmp_path / "Hub.app.previous"
    backup.mkdir()
    (backup / "old-version").write_text("original")
    backend = update_app.AppBackend(tmp_path, {"menubar_path": str(app), "host_capability": "dashboard"})
    observed = []

    def statuses(target):
        observed.append(Path(target).exists())
        return {"host": "not_registered", "menu": "not_registered", "dashboard": status}

    monkeypatch.setattr(backend, "_statuses", statuses)
    restored = []
    monkeypatch.setattr(backend, "_restore_services", lambda *args: restored.append(args))
    monkeypatch.setattr(update_plugins, "rollback", lambda *args: None)
    transaction = {"services": {"host": "not_registered", "menu": "not_registered",
                                "dashboard": "enabled"},
                   "providers": [], "stack": str(tmp_path),
                   "apps": {"menubar": {"target": str(app), "backup": str(backup)}}}
    backend.rollback({"id": "test", "transaction": transaction})
    # Nothing is observable or stoppable until the bundle is back in place.
    assert observed == [True]
    # A denial recorded after the snapshot wins over the snapshot.
    assert restored[0][1]["dashboard"] == status
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
