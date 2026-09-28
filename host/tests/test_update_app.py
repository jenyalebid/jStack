# NOTE — destructive install/uninstall paths: never run these for real on the home
# machine (the production Hub). Every launchd / JStackHub / sudo boundary must be
# stubbed; conftest fails the test if one is reached. Real proofs run in lab guests.
import json
from pathlib import Path

import pytest
import shutil

from jstack_host import install_host, update_app, update_plugins


_REAL_SPAWNED = update_app.spawned


@pytest.fixture(autouse=True)
def _launchd_spawns(monkeypatch):
    """`spawned` asks the real launchd for a pid; a unit test has no job to
    show it. Tests of the spawn check override this."""
    monkeypatch.setattr(update_app, "spawned", lambda label, seconds=20.0: True)


def transaction_for(app, source, tmp_path, backup=None):
    return {"release": "same-release", "services": {}, "providers": [],
            "stack": str(tmp_path), "manifest": {"components": {"menubar": {}}},
            "apps": {"menubar": {"source": str(source), "target": str(app), "existed": True,
                                 "was_running": False,
                                 "backup": str(backup or tmp_path / "Hub.app.previous-stage-x")}}}


def prepared(monkeypatch, tmp_path, app):
    backend = update_app.AppBackend(tmp_path, {"menubar_path": str(app)})
    monkeypatch.setattr(backend, "_statuses", lambda _: {})
    monkeypatch.setattr(backend, "_stop_services", lambda *args: None)
    monkeypatch.setattr(backend, "_restore_services", lambda *args: None)
    monkeypatch.setattr(backend, "_check_app", lambda *args, **kwargs: None)
    monkeypatch.setattr(update_plugins, "install", lambda *args: None)
    return backend


def test_a_copy_a_kill_left_behind_does_not_poison_the_release(monkeypatch, tmp_path):
    """A kill mid-copy used to leave `<app>.incoming-*`, and every later job
    for that release died on it until someone deleted it by hand (#117)."""
    app, source = tmp_path / "Hub.app", tmp_path / "staged.app"
    app.mkdir()
    (app / "version").write_text("old")
    source.mkdir()
    (source / "version").write_text("new")
    backend = prepared(monkeypatch, tmp_path, app)
    transaction = transaction_for(app, source, tmp_path)

    def interrupted_copy(argv):
        destination = Path(argv[-1])
        destination.mkdir()
        (destination / "partial").write_text("interrupted")
        raise KeyboardInterrupt()

    monkeypatch.setattr(update_app, "command", interrupted_copy)
    with pytest.raises(KeyboardInterrupt):
        backend.apply({"id": "interrupted-job", "transaction": transaction})
    # Nothing was replaced, and the half-written copy is still lying there.
    assert (app / "version").read_text() == "old"
    assert (tmp_path / "Hub.app.incoming-interrupted-job").is_dir()
    monkeypatch.setattr(update_app, "command", lambda argv: shutil.copytree(argv[-2], argv[-1]))
    backend.apply({"id": "retry-job", "transaction": transaction})
    assert (app / "version").read_text() == "new"
    assert not (tmp_path / "Hub.app.incoming-interrupted-job").exists()
    assert not (tmp_path / "Hub.app.incoming-retry-job").exists()


def test_a_failed_update_leaves_the_bundle_that_is_installed_running(monkeypatch, tmp_path):
    """No rollback: settling starts the services again against whatever is at
    the target, and keeps no bundle of its own anywhere near it."""
    app, source = tmp_path / "Hub.app", tmp_path / "staged.app"
    app.mkdir()
    (app / "version").write_text("old")
    source.mkdir()
    backend = prepared(monkeypatch, tmp_path, app)
    restored = []
    monkeypatch.setattr(backend, "_restore_services", lambda *args: restored.append(args))
    monkeypatch.setattr(backend, "_statuses", lambda _: {"host": "enabled", "menu": "enabled"})
    for leftover in ("Hub.app.incoming-failed-job", "Hub.app.previous-stage-x",
                     "Hub.app.failed-older-job"):
        (tmp_path / leftover).mkdir()
    transaction = transaction_for(app, source, tmp_path)
    transaction["services"] = {"host": "enabled", "menu": "enabled"}
    job = {"id": "failed-job", "transaction": transaction}
    assert backend.applied(job) is True  # the backup exists: the swap happened
    assert backend.settle(job) == {"error": ""}
    assert (app / "version").read_text() == "old"
    assert restored == [(app, {"host": "enabled", "menu": "enabled"})]
    assert sorted(path.name for path in tmp_path.glob("Hub.app*")) == ["Hub.app"]


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


@pytest.mark.parametrize("layout,expected", [
    # (target, backup, incoming) present on disk after the cut
    ((True, True, False), "applied"),    # both renames done
    ((True, False, True), "unknown"),    # frozen mid-copy: prior untouched, copy half-written
    ((True, False, False), "unknown"),   # nothing began
    ((False, True, True), "unknown"),    # cut between the renames
    ((False, True, False), "unknown"),   # backup made, no bundle at all
    ((True, True, True), "unknown"),     # copy left behind next to a finished swap
])
def test_recovery_state_is_judged_by_the_transaction_files(monkeypatch, tmp_path, layout, expected):
    app = tmp_path / "Hub.app"
    backup = tmp_path / "Hub.app.previous-app-stage-x"
    incoming = tmp_path / "Hub.app.incoming-job-1"
    for path, present in zip((app, backup, incoming), layout):
        if present:
            path.mkdir()
    backend = update_app.AppBackend(tmp_path, {"menubar_path": str(app)})
    checked = []
    monkeypatch.setattr(backend, "_check_app", lambda path, component, kind, **_: checked.append((path, kind)))
    transaction = {"apps": {"menubar": {"target": str(app), "backup": str(backup), "existed": True}},
                   "manifest": {"components": {"menubar": {"version": "20260923"}}}}
    assert backend.recovery_status({"id": "job-1", "transaction": transaction}) == expected
    assert checked == ([(app, "menubar")] if expected == "applied" else [])


def test_recovery_ignores_a_same_version_prior_that_was_never_swapped(monkeypatch, tmp_path):
    """Two releases built the same day share CFBundleVersion; the untouched
    prior must not pass as the finished copy (jStack #127 reboot leg)."""
    app = tmp_path / "Hub.app"
    app.mkdir()
    (tmp_path / "Hub.app.incoming-job-2").mkdir()
    backend = update_app.AppBackend(tmp_path, {"menubar_path": str(app)})
    monkeypatch.setattr(backend, "_check_app", lambda path, component, kind, **_: None)  # version matches
    transaction = {"apps": {"menubar": {"target": str(app), "existed": True,
                                        "backup": str(tmp_path / "Hub.app.previous-x")}},
                   "manifest": {"components": {"menubar": {"version": "20260923"}}}}
    assert backend.recovery_status({"id": "job-2", "transaction": transaction}) == "unknown"


def test_recovery_judges_every_app_in_the_transaction(monkeypatch, tmp_path):
    hub, client = tmp_path / "Hub.app", tmp_path / "jRemote.app"
    hub.mkdir(); client.mkdir()
    (tmp_path / "Hub.app.previous-x").mkdir()
    backend = update_app.AppBackend(tmp_path, {"menubar_path": str(hub)})
    monkeypatch.setattr(backend, "_check_app", lambda path, component, kind, **_: None)
    transaction = {"apps": {
        "menubar": {"target": str(hub), "backup": str(tmp_path / "Hub.app.previous-x"), "existed": True},
        "client": {"target": str(client), "backup": str(tmp_path / "jRemote.app.previous-x"), "existed": True}},
        "manifest": {"components": {"menubar": {"version": "1"}, "client": {"version": "2"}}}}
    assert backend.recovery_status({"id": "job-3", "transaction": transaction}) == "unknown"
    (tmp_path / "jRemote.app.previous-x").mkdir()
    assert backend.recovery_status({"id": "job-3", "transaction": transaction}) == "applied"


def test_recovery_of_a_first_install_needs_no_backup(monkeypatch, tmp_path):
    app = tmp_path / "Hub.app"
    backend = update_app.AppBackend(tmp_path, {"menubar_path": str(app)})
    monkeypatch.setattr(backend, "_check_app", lambda path, component, kind, **_: None)
    transaction = {"apps": {"menubar": {"target": str(app), "existed": False,
                                        "backup": str(tmp_path / "Hub.app.previous-x")}},
                   "manifest": {"components": {"menubar": {"version": "1"}}}}
    assert backend.recovery_status({"id": "job-4", "transaction": transaction}) == "unknown"
    app.mkdir()
    assert backend.recovery_status({"id": "job-4", "transaction": transaction}) == "applied"
    assert backend.recovery_status({"transaction": transaction}) == "unknown"


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


def test_a_failure_before_the_swap_says_so_and_keeps_the_copy_out_of_the_way(monkeypatch, tmp_path):
    app = tmp_path / "Hub.app"
    app.mkdir()
    (app / "version").write_text("old")
    (tmp_path / "Hub.app.incoming-test").mkdir()
    backend = update_app.AppBackend(tmp_path, {"menubar_path": str(app)})
    monkeypatch.setattr(backend, "_statuses", lambda _: {"host": "enabled", "menu": "not_registered"})
    restored = []
    monkeypatch.setattr(backend, "_restore_services", lambda *args: restored.append(args))
    transaction = {"services": {"host": "enabled", "menu": "not_registered"}, "providers": [],
                   "stack": str(tmp_path), "apps": {
                       "menubar": {"target": str(app), "existed": True,
                                   "backup": str(tmp_path / "Hub.app.previous-stage-x")},
                       "client": {"target": str(tmp_path / "client"), "existed": True,
                                  "backup": str(tmp_path / "client.previous-stage-x"),
                                  "was_running": False}}}
    job = {"id": "test", "transaction": transaction}
    assert backend.applied(job) is False  # no backup was ever made
    backend.settle(job)
    assert (app / "version").read_text() == "old"
    assert not (tmp_path / "Hub.app.incoming-test").exists()
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
def test_settling_reads_approvals_now_rather_than_trusting_the_snapshot(monkeypatch, tmp_path, status):
    app = tmp_path / "Hub.app"
    app.mkdir()
    backend = update_app.AppBackend(tmp_path, {"menubar_path": str(app), "host_capability": "dashboard"})
    monkeypatch.setattr(backend, "_statuses", lambda _: {
        "host": "not_registered", "menu": "not_registered", "dashboard": status})
    restored = []
    monkeypatch.setattr(backend, "_restore_services", lambda *args: restored.append(args))
    transaction = {"services": {"host": "not_registered", "menu": "not_registered",
                                "dashboard": "enabled"},
                   "providers": [], "stack": str(tmp_path),
                   "apps": {"menubar": {"target": str(app), "existed": True,
                                        "backup": str(tmp_path / "Hub.app.previous-x")}}}
    backend.settle({"id": "test", "transaction": transaction})
    # A denial recorded after the snapshot wins over the snapshot.
    assert restored[0][1]["dashboard"] == status


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


# --- private capabilities and sealed roles across an update -----------------

SEALED = {role: f"live.jstack.hub.{role}.plist" for role in ("host", "menu", "updater")}
CAPABILITY = {"plist": "live.jstack.automation.dashboard.plist", "job_sha256": "aa" * 32}


def bundle(root, services, catalog=None):
    resources = root / "Contents/Resources"
    resources.mkdir(parents=True)
    (resources / "services.json").write_text(json.dumps(services))
    if catalog is not None:
        (resources / "automation-catalog.json").write_text(json.dumps(catalog))
    return root


def capability(name):
    return {name: f"live.jstack.automation.{name}.plist"}


def ownership(tmp_path, installed, candidate):
    backend = update_app.AppBackend(tmp_path, {"menubar_path": str(installed)})
    return backend._check_ownership(candidate, installed)


def test_a_capability_the_candidate_seals_as_a_role_is_the_cutover_not_a_migration(tmp_path):
    """The Mac this grew up on ran the scheduler as a catalogued capability. The
    cutover (procedures: one transaction) removes it from the private catalog
    while the bundle seals a role of the same name — the update performs that
    move, so the catalog check must read it as the cutover and nothing else."""
    installed = bundle(tmp_path / "installed.app", {**SEALED, **capability("dashboard"), **capability("scheduler")},
                       {"dashboard": CAPABILITY, "scheduler": {**CAPABILITY, "plist": "live.jstack.automation.scheduler.plist"}})
    candidate = bundle(tmp_path / "candidate.app",
                       {**SEALED, "scheduler": "live.jstack.hub.scheduler.plist", **capability("dashboard")},
                       {"dashboard": CAPABILITY})
    ownership(tmp_path, installed, candidate)


def test_any_other_private_capability_change_is_a_separate_migration(tmp_path):
    installed = bundle(tmp_path / "installed.app", {**SEALED, **capability("dashboard"), **capability("relay")},
                       {"dashboard": CAPABILITY, "relay": CAPABILITY})
    dropped = bundle(tmp_path / "dropped.app", {**SEALED, **capability("dashboard")}, {"dashboard": CAPABILITY})
    with pytest.raises(update_app.releases.ReleaseError, match="separate migration"):
        ownership(tmp_path, installed, dropped)  # the role goes with it: ownership refuses first
    changed = bundle(tmp_path / "changed.app", {**SEALED, **capability("dashboard"), **capability("relay")},
                     {"dashboard": CAPABILITY, "relay": {**CAPABILITY, "job_sha256": "bb" * 32}})
    with pytest.raises(update_app.releases.ReleaseError, match="private capabilities"):
        ownership(tmp_path, installed, changed)


def test_a_new_sealed_role_is_accepted_and_any_other_ownership_change_refused(tmp_path):
    """A work Mac carries no private catalog; the one-hub release seals a
    scheduler role its predecessor did not. That is the update, not a migration.
    A role the candidate drops, or a name it adds without sealing, still is."""
    installed = bundle(tmp_path / "installed.app", SEALED)
    grown = bundle(tmp_path / "grown.app", {**SEALED, "scheduler": "live.jstack.hub.scheduler.plist"})
    ownership(tmp_path, installed, grown)
    shrunk = bundle(tmp_path / "shrunk.app", {role: SEALED[role] for role in ("host", "updater")})
    with pytest.raises(update_app.releases.ReleaseError, match="service ownership"):
        ownership(tmp_path, installed, shrunk)
    unsealed = bundle(tmp_path / "unsealed.app", {**SEALED, **capability("relay")}, {"relay": CAPABILITY})
    with pytest.raises(update_app.releases.ReleaseError, match="service ownership"):
        ownership(tmp_path, installed, unsealed)



def test_a_sealed_role_the_bundle_never_registered_is_registered_on_restore(monkeypatch, tmp_path):
    """Live on the home Mac, 22:10: the cutover stopped the catalogued scheduler,
    swapped the bundle, and restore asked SMAppService about the sealed role —
    which answers not_found for a plist never registered under this bundle. The
    old rule read that as 'the bundle dropped it' and left :9091 dead."""
    app = bundle(tmp_path / "Hub.app", {**SEALED, "scheduler": "live.jstack.hub.scheduler.plist"})
    calls = []

    def control(app, action, role=None):
        calls.append((action, role))
        return {"scheduler": "not_found"} if action == "status" else {"status": "enabled"}

    monkeypatch.setattr(update_app, "control", control)
    backend = update_app.AppBackend(tmp_path, {})
    backend._restore_services(app, {"host": "not_registered", "menu": "not_registered", "scheduler": "enabled"})
    assert calls == [("status", None), ("register", "scheduler")]


def test_a_capability_the_bundle_no_longer_carries_is_not_guessed_at(monkeypatch, tmp_path):
    app = bundle(tmp_path / "Hub.app", {**SEALED, **capability("dashboard")}, {"dashboard": CAPABILITY})
    calls = []

    def control(app, action, role=None):
        calls.append((action, role))
        return {"relay": "not_found"}

    monkeypatch.setattr(update_app, "control", control)
    backend = update_app.AppBackend(tmp_path, {})
    with pytest.raises(ValueError, match="could not be restored"):
        backend._restore_services(app, {"host": "not_registered", "menu": "not_registered", "relay": "enabled"})
    assert calls == [("status", None)]


def test_a_failed_restore_still_registers_every_service_after_it_and_names_them_all(monkeypatch, tmp_path):
    """Live on the home Mac, 22:10: the scheduler's restore raised and the loop
    ended there. session-stall sorts right after it, stayed unregistered through
    the hand recovery, and the next update snapshotted it as not_registered."""
    app = bundle(tmp_path / "Hub.app", {**SEALED, **capability("session-stall"), **capability("wda")},
                 {"session-stall": CAPABILITY, "wda": CAPABILITY})
    calls = []

    def control(app, action, role=None):
        calls.append((action, role))
        if action == "status":
            return {"scheduler": "not_found", "session-stall": "not_registered", "wda": "not_registered"}
        return {"status": "enabled"}

    monkeypatch.setattr(update_app, "control", control)
    backend = update_app.AppBackend(tmp_path, {})
    with pytest.raises(ValueError, match="scheduler service could not be restored") as raised:
        backend._restore_services(app, {"host": "not_registered", "menu": "not_registered",
                                        "scheduler": "enabled", "session-stall": "enabled",
                                        "wda": "enabled"})
    assert "session-stall" not in str(raised.value) and "wda" not in str(raised.value)
    assert [c for c in calls if c[0] == "register"] == [("register", "session-stall"), ("register", "wda")]


def test_every_failed_restore_is_named_once_at_the_end(monkeypatch, tmp_path):
    app = bundle(tmp_path / "Hub.app", {**SEALED, **capability("session-stall")}, {"session-stall": CAPABILITY})

    def control(app, action, role=None):
        if action == "status":
            return {"relay": "not_found", "session-stall": "not_registered"}
        return {"status": "not_found"}

    monkeypatch.setattr(update_app, "control", control)
    backend = update_app.AppBackend(tmp_path, {})
    with pytest.raises(ValueError) as raised:
        backend._restore_services(app, {"host": "not_registered", "menu": "not_registered",
                                        "relay": "enabled", "session-stall": "enabled"})
    assert str(raised.value) == ("relay service could not be restored; "
                                 "session-stall service could not be restored")


def test_a_role_launchd_refuses_to_spawn_is_reregistered_once(monkeypatch, tmp_path):
    """Work Main, macOS 27.0, 2026-09-25 (#182): after the ad-hoc → Developer
    ID swap every role answered `enabled` while launchd logged `spawn failed`,
    exit 78, for half an hour. `unregister` + `register` from the new bundle
    is what brought each one back by hand; restore does that once, then asks
    launchd for the pid again."""
    app = bundle(tmp_path / "Hub.app", SEALED)
    calls, answers = [], iter([False, True])

    def control(app, action, role=None):
        calls.append((action, role))
        return {"host": "enabled", "menu": "enabled"} if action == "status" else {"status": "enabled"}

    monkeypatch.setattr(update_app, "control", control)
    monkeypatch.setattr(update_app, "spawned", lambda label, seconds=20.0: next(answers, True))
    backend = update_app.AppBackend(tmp_path, {})
    backend._restore_services(app, {"host": "enabled", "menu": "enabled"})
    assert calls == [("status", None), ("register", "host"), ("unregister", "host"), ("register", "host"),
                     ("status", None), ("register", "menu")]


def test_a_role_launchd_still_refuses_after_reregistration_fails_the_restore(monkeypatch, tmp_path):
    app = bundle(tmp_path / "Hub.app", SEALED)
    monkeypatch.setattr(update_app, "control",
                        lambda app, action, role=None: {"menu": "enabled"} if action == "status"
                        else {"status": "enabled"})
    monkeypatch.setattr(update_app, "spawned", lambda label, seconds=20.0: False)
    backend = update_app.AppBackend(tmp_path, {})
    with pytest.raises(update_app.releases.ReleaseError, match="menu service is registered but launchd could not spawn"):
        backend._restore_services(app, {"host": "not_registered", "menu": "enabled"})


def test_a_role_awaiting_approval_is_not_asked_to_spawn(monkeypatch, tmp_path):
    app = bundle(tmp_path / "Hub.app", SEALED)
    monkeypatch.setattr(update_app, "control",
                        lambda app, action, role=None: {"menu": "requires_approval"} if action == "status"
                        else {"status": "requires_approval"})
    monkeypatch.setattr(update_app, "spawned", lambda label, seconds=20.0: pytest.fail("no pid to wait for"))
    update_app.AppBackend(tmp_path, {})._restore_services(app, {"host": "not_registered", "menu": "enabled"})


def test_spawned_reads_a_pid_from_launchd_not_a_registration(monkeypatch):
    import subprocess
    spawned = _REAL_SPAWNED
    shown = {"stdout": ""}
    monkeypatch.setattr(install_host, "_launchctl",
                        lambda *args: subprocess.CompletedProcess(args, 0, shown["stdout"], ""))
    assert spawned("live.jstack.hub.menu", seconds=0) is False
    shown["stdout"] = "live.jstack.hub.menu = {\n\tactive count = 1\n\tpid = 4242\n\tstate = running\n}"
    assert spawned("live.jstack.hub.menu", seconds=0) is True
    shown["stdout"] = "live.jstack.hub.menu = {\n\tstate = spawn failed\n\tlast exit code = 78\n}"
    assert spawned("live.jstack.hub.menu", seconds=0) is False


def test_spawned_accepts_a_scheduled_role_launchd_holds_between_runs(monkeypatch):
    """Home hub, 2026-09-25 19:11 (#203): the self-update failed naming seven
    automation roles "registered but launchd could not spawn it" while every
    one was loaded with exit 0. They run on a calendar or an interval, so
    between runs launchd shows no pid; that is the job at rest, not a refusal."""
    import subprocess
    spawned = _REAL_SPAWNED
    shown = {"stdout": ""}
    monkeypatch.setattr(install_host, "_launchctl",
                        lambda *args: subprocess.CompletedProcess(args, 0, shown["stdout"], ""))
    calendar = ("live.jstack.automation.host-audit = {\n\tstate = not running\n"
                "\tjob state = uninitialized\n\tlast exit code = (never exited)\n\truns = 0\n"
                "\tevent triggers = {\n\t\tcalendar => {\n\t\t\tkeepalive = 0\n"
                "\t\t\tstream = com.apple.launchd.calendarinterval\n\t\t}\n\t}\n}")
    interval = ("live.jstack.automation.ddns = {\n\tstate = not running\n\tjob state = exited\n"
                "\tlast exit code = 0\n\trun interval = 300 seconds\n\truns = 41\n}")
    for shown["stdout"] in (calendar, interval):
        assert spawned("live.jstack.automation.x", seconds=0) is True
    # A scheduled job launchd refused is still a refusal.
    shown["stdout"] = ("live.jstack.automation.ddns = {\n\tstate = spawn failed\n"
                       "\tlast exit code = 78\n\trun interval = 300 seconds\n}")
    assert spawned("live.jstack.automation.x", seconds=0) is False
    # A service that stays up and shows no pid is not spawned, schedule or not.
    shown["stdout"] = "live.jstack.hub.host = {\n\tstate = not running\n\tlast exit code = 0\n}"
    assert spawned("live.jstack.hub.host", seconds=0) is False
    # An unloaded label is never spawned, whatever the text says.
    monkeypatch.setattr(install_host, "_launchctl",
                        lambda *args: subprocess.CompletedProcess(args, 113, interval, "not found"))
    assert spawned("live.jstack.automation.x", seconds=0) is False


def _settled_job(release, sha):
    return {"state": "current", "finalized": True, "release": release,
            "transaction": {"apps": {"menubar": {}}},
            "envelope": {"manifest": {"sources": {"stack": sha}}}}


def test_restart_is_required_when_the_release_changed_on_the_same_sha(monkeypatch, tmp_path):
    """A joiner installs a source build of the sha the hub already publishes;
    the hub's update swaps a different bundle with the same sha. Work Main
    kept its swapped-out updater running 35 minutes after such an update
    (#183): the sha said nothing changed. The release id is what changed."""
    from jstack_host import sourcestamp
    monkeypatch.setattr(sourcestamp, "capture", lambda: {"sha": "abc"})
    monkeypatch.setattr(update_app, "RUNNING_RELEASE", "2026-09-25-abc-source")
    backend = update_app.AppBackend(tmp_path, {})
    assert backend.restart_required(_settled_job("2026-09-25-abc-hub", "abc")) is True
    assert backend.restart_required(_settled_job("2026-09-25-abc-source", "abc")) is False
    monkeypatch.setattr(update_app, "RUNNING_RELEASE", "")
    assert backend.restart_required(_settled_job("2026-09-25-abc-hub", "abc")) is False
    assert backend.restart_required(_settled_job("2026-09-25-def-hub", "def")) is True


def test_a_signing_identity_change_reregisters_the_updater_at_restart(monkeypatch, tmp_path):
    """The updater is never in the snapshot, so nothing re-registers it; on
    Work Main its relaunch after the ad-hoc → Developer ID swap hit the same
    spawn refusal as every other role (#182). Judged at apply, while the old
    bundle is still at `backup`; acted on when the supervisor exits."""
    import subprocess
    app, source = tmp_path / "Hub.app", tmp_path / "staged.app"
    app.mkdir(); source.mkdir()
    backend = prepared(monkeypatch, tmp_path, app)
    monkeypatch.setattr(update_app, "command", lambda argv: shutil.copytree(argv[-2], argv[-1]))
    monkeypatch.setattr(update_app, "team_identifier",
                        lambda path: "MZ95H77RQQ" if path == app else "")
    job = {"id": "swap", "transaction": transaction_for(app, source, tmp_path)}
    backend.apply(job)
    assert job["transaction"]["updater_reregister"] is True
    launched = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: launched.append((argv, kw)))
    backend.prepare_restart(job)
    (argv, kw), = launched
    assert argv[-1] == str(app / "Contents/MacOS/JStackHub")
    assert "unregister updater" in argv[2] and argv[2].index("unregister updater") < argv[2].index("register updater", 5)
    assert kw["start_new_session"] is True


def test_an_update_that_keeps_its_signing_identity_leaves_the_updater_item_alone(monkeypatch, tmp_path):
    import subprocess
    app, source = tmp_path / "Hub.app", tmp_path / "staged.app"
    app.mkdir(); source.mkdir()
    backend = prepared(monkeypatch, tmp_path, app)
    monkeypatch.setattr(update_app, "command", lambda argv: shutil.copytree(argv[-2], argv[-1]))
    monkeypatch.setattr(update_app, "team_identifier", lambda path: "MZ95H77RQQ")
    job = {"id": "same", "transaction": transaction_for(app, source, tmp_path)}
    backend.apply(job)
    assert job["transaction"]["updater_reregister"] is False
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("no relaunch helper"))
    backend.prepare_restart(job)


def test_an_automation_role_gone_after_the_swap_fails_by_name_and_settling_restores_it(monkeypatch, tmp_path):
    """The home hub's update of 2026-09-25 19:11: nine automation roles read
    not_registered mid-job and were registered by hand (#197). A role the
    snapshot held enabled and verification finds gone must never read as a
    passed update, the failure must say which role, and settling registers it."""
    app = bundle(tmp_path / "Hub.app", {**SEALED, **capability("tunnel")}, {"tunnel": CAPABILITY})
    registry = {"host": "not_registered", "menu": "not_registered", "tunnel": "not_registered"}

    def control(app, action, role=None):
        if action == "status":
            return dict(registry)
        registry[role] = "enabled" if action == "register" else "not_registered"
        return {"service": role, "status": registry[role]}

    monkeypatch.setattr(update_app, "control", control)
    monkeypatch.setattr(update_plugins, "discover", lambda: {})
    monkeypatch.setattr(update_plugins, "observed", lambda _: {})
    monkeypatch.setattr(install_host, "is_loaded", lambda _: False)
    backend = update_app.AppBackend(tmp_path, {"menubar_path": str(app)})
    job = {"id": "job-197", "envelope": {"manifest": {"components": {"stack": {"version": "1"}}}},
           "transaction": {"apps": {}, "services": {
               "host": "not_registered", "menu": "not_registered", "tunnel": "enabled"}}}
    assert not backend.verify(job)
    assert backend.unverified == "tunnel service is not_registered, was enabled before the update"
    assert backend.settle(job) == {"error": ""}
    assert registry["tunnel"] == "enabled"
    assert backend.verify(job) and backend.unverified == ""
