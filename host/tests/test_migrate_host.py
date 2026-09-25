# NOTE — destructive install/uninstall paths: never run these for real on the home
# machine (the production Hub). Every launchd / JStackHub / sudo boundary must be
# stubbed; conftest fails the test if one is reached. Real proofs run in lab guests.
import json
from pathlib import Path
import plistlib
from types import SimpleNamespace

import pytest

from jstack_host import install_host, migrate_host as migration, service_settings

verify_services = migration.verify


def test_provenance_accepts_only_the_known_privacy_protected_legacy_menu(monkeypatch, tmp_path):
    executable = tmp_path / "Library/Application Support/jStack/JStack Host.app/Contents/MacOS/JStackHostBar"
    row = {"label": "com.jremote.menubar", "executable": str(executable),
           "definition": {"sha256": "definition"},
           "executable_file": {"unobserved": "app data path; requires a separate permission review"},
           "signature": {"status": "unobservable", "reason": "app data path"}, "scripts": [],
           "findings": ["executable_signature_not_verified", "code_file_not_observed"]}
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(migration.service_inventory, "inspect_job", lambda *args: row)
    assert migration.provenance(tmp_path / "menu.plist")["executable_file"] == row["executable_file"]
    row["label"] = "another.product"
    with pytest.raises(ValueError, match="legacy source is unobserved"):
        migration.provenance(tmp_path / "menu.plist")


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    state = tmp_path / "existing-state"
    state.mkdir()
    (state / "host-id").write_text("existing-machine")
    (state / "updates").mkdir()
    app = tmp_path / "Hub.app"
    trust = app / "Contents/Resources/packages/jstack_host/release-trust.json"
    trust.parent.mkdir(parents=True)
    trust.write_text(json.dumps({"public_key": "existing-public-key"}))
    root = tmp_path / "private/migrations"
    settings = {"schema": 1, "app": str(app), "migration_dir": str(root),
                "port": 9432, "bind": "127.0.0.1", "environment": {
                    "JREMOTE_STATE_DIR": str(state), "JREMOTE_HOST_PROFILE": "default"}}
    old = {"machine": "existing-machine", "team_id": "MZ95H77RQQ", "local_url": "http://127.0.0.1:9432",
           "public_key": "existing-public-key", "host_label": "old.host", "menubar_label": "old.menu",
           "client_bundle_id": "existing.client", "client_managed": False, "dispatcher": "old-dispatcher",
           "token_path": str(state / "internal-token")}
    config = state / "updates/config.json"
    config.write_text(json.dumps(old))
    jobs = {role: {"Label": label, "ProgramArguments": ["/bin/sleep", "300"], "RunAtLoad": True}
            for role, label in {"host": "old.host", "menu": "old.menu", "updater": "com.jremote.updater"}.items()}
    jobs["host"]["ProgramArguments"] += ["--port", "9432", "--host", "127.0.0.1"]
    jobs["host"]["EnvironmentVariables"] = settings["environment"].copy()
    paths = {}
    for role, job in jobs.items():
        path = install_host.plist_path(job["Label"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps(job))
        paths[role] = path
    loaded = {job["Label"] for job in jobs.values()}
    statuses = {role: "not_registered" for role in migration.ROLES}
    disabled, calls = set(), []
    monkeypatch.setattr(migration, "verified_hub", lambda _: {"sha": "a" * 40, "release": "new-release"})
    monkeypatch.setattr(migration, "provenance", lambda path: {"sha256": migration.file_hash(path)})
    monkeypatch.setattr(migration.embed, "read", lambda: {})
    monkeypatch.setattr(migration.migration, "disabled_labels", lambda: disabled)
    monkeypatch.setattr(migration.migration, "legacy_status", lambda *args: "enabled")
    monkeypatch.setattr(migration, "verify", lambda _: None)

    def control(owner, action, role=None):
        calls.append((action, role))
        if action == "status":
            return dict(statuses)
        if action == "register":
            assert not any(label in loaded for label in (job["Label"] for job in jobs.values()))
            statuses[role] = "enabled"
            loaded.add("live.jstack.hub." + role)
        else:
            statuses[role] = "not_registered"
            loaded.discard("live.jstack.hub." + role)
        return {"status": statuses[role]}

    def launchctl(*args):
        calls.append(args)
        if args[0] == "bootout":
            loaded.discard(args[1].split("/")[-1])
        return SimpleNamespace(returncode=0)

    def bootstrap(label, path):
        calls.append(("bootstrap", label))
        assert not any(label.startswith("live.jstack.") for label in loaded)
        loaded.add(label)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(migration, "control", control)
    monkeypatch.setattr(install_host, "is_loaded", lambda label: label in loaded)
    monkeypatch.setattr(install_host, "wait_unloaded", lambda label: label not in loaded)
    monkeypatch.setattr(migration.migration, "loaded", lambda label: label in loaded)
    monkeypatch.setattr(migration.migration, "wait_unloaded", lambda label: label not in loaded)
    monkeypatch.setattr(install_host, "_launchctl", launchctl)
    monkeypatch.setattr(install_host, "bootstrap", bootstrap)
    request = {"settings": settings, "jobs": jobs,
               "provenance": {role: migration.provenance(path) for role, path in paths.items()}}
    return SimpleNamespace(request=request, root=root, paths=paths, config=config, old=old, loaded=loaded,
                           statuses=statuses, calls=calls, disabled=disabled, settings=settings, state=state)


def test_cutover_and_rollback_preserve_identity_trust_and_distribution(lab):
    original_config = lab.config.read_bytes()
    originals = {role: path.read_bytes() for role, path in lab.paths.items()}
    journal = migration.prepare(lab.request, lab.root)
    migration.apply(journal)
    assert migration.load(journal)["state"] == "migrated"
    assert lab.loaded == {"live.jstack.hub." + role for role in migration.ROLES}
    assert not any(path.exists() for path in lab.paths.values())
    installed = json.loads(lab.config.read_text())
    for key in ("machine", "public_key", "client_bundle_id", "client_managed", "token_path", "local_url"):
        assert installed[key] == lab.old[key]
    assert "dispatcher" not in installed and installed["service_model"] == "app"
    migration.rollback(journal)
    assert lab.config.read_bytes() == original_config
    assert not service_settings.path().exists()
    assert {role: path.read_bytes() for role, path in lab.paths.items()} == originals
    assert lab.loaded == {job["Label"] for job in lab.request["jobs"].values()}


def test_disabled_host_is_not_started_under_a_new_owner_or_during_rollback(lab):
    lab.loaded.discard("old.host")
    lab.disabled.add("old.host")
    journal = migration.prepare(lab.request, lab.root)
    migration.apply(journal)
    assert "live.jstack.hub.host" not in lab.loaded
    migration.rollback(journal)
    assert "old.host" not in lab.loaded and lab.paths["host"].exists()


def test_stopped_unregistered_original_stays_out_of_login_discovery(lab):
    lab.loaded.discard("old.host")
    journal = migration.prepare(lab.request, lab.root)
    migration.apply(journal)
    migration.rollback(journal)
    assert "old.host" not in lab.loaded and not lab.paths["host"].exists()
    assert migration.load(journal)["stopped_originals"] == ["host"]


def test_denial_during_cutover_holds_the_legacy_paths(lab, monkeypatch):
    original = migration.control

    def denied(owner, action, role=None):
        if action == "register":
            lab.statuses[role] = "requires_approval"
            return {"status": "requires_approval"}
        return original(owner, action, role)

    monkeypatch.setattr(migration, "control", denied)
    journal = migration.prepare(lab.request, lab.root)
    with pytest.raises(ValueError, match="OS approval"):
        migration.apply(journal)
    assert migration.load(journal)["state"] == "approval_required"
    assert not lab.loaded and not any(path.exists() for path in lab.paths.values())
    assert json.loads(lab.config.read_text())["service_model"] == "app"


def test_failed_health_restores_the_exact_legacy_installation(lab, monkeypatch):
    journal = migration.prepare(lab.request, lab.root)

    def failed(_):
        raise ValueError("API failed")

    monkeypatch.setattr(migration, "verify", failed)
    with pytest.raises(ValueError, match="API failed"):
        migration.apply(journal)
    assert migration.load(journal)["state"] == "rolled_back"
    assert json.loads(lab.config.read_text()) == lab.old
    assert lab.loaded == {job["Label"] for job in lab.request["jobs"].values()}


@pytest.mark.parametrize("status", ["not_registered", "not_found"])
def test_rollback_reloads_enabled_originals_after_os_forgets_registration(lab, monkeypatch, status):
    journal = migration.prepare(lab.request, lab.root)
    migration.apply(journal)
    monkeypatch.setattr(migration.migration, "legacy_status", lambda *args: status)
    migration.rollback(journal)
    assert migration.load(journal)["state"] == "rolled_back"
    assert lab.loaded == {job["Label"] for job in lab.request["jobs"].values()}


def test_rollback_does_not_claim_completion_with_unknown_legacy_approval(lab, monkeypatch):
    journal = migration.prepare(lab.request, lab.root)
    migration.apply(journal)
    monkeypatch.setattr(migration.migration, "legacy_status", lambda *args: "unknown")
    with pytest.raises(ValueError, match="unobservable during rollback"):
        migration.rollback(journal)
    assert migration.load(journal)["state"] != "rolled_back"
    assert not any(call[0] == "bootstrap" for call in lab.calls)


@pytest.mark.parametrize("host_off", [False, True])
def test_interrupted_rollback_does_not_treat_its_own_stop_as_user_off(lab, monkeypatch, host_off):
    journal = migration.prepare(lab.request, lab.root)
    migration.apply(journal)
    if host_off:
        lab.statuses["host"] = "not_registered"
        lab.loaded.discard("live.jstack.hub.host")
    original = migration.control
    crashed = False

    def interrupted(owner, action, role=None):
        nonlocal crashed
        result = original(owner, action, role)
        if action == "unregister" and not crashed:
            crashed = True
            raise OSError("interrupted after stopping replacement")
        return result

    monkeypatch.setattr(migration, "control", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        migration.rollback(journal)
    migration.rollback(journal)
    assert lab.loaded == {job["Label"] for role, job in lab.request["jobs"].items() if role != "host" or not host_off}


def test_completed_rollback_is_idempotent_after_user_stops_legacy_host(lab):
    journal = migration.prepare(lab.request, lab.root)
    migration.apply(journal)
    migration.rollback(journal)
    lab.loaded.discard("old.host")
    calls = list(lab.calls)
    migration.rollback(journal)
    assert "old.host" not in lab.loaded
    assert not any(call[0] == "bootstrap" for call in lab.calls[len(calls):])


@pytest.mark.parametrize("change", ["source", "trust", "endpoint", "state", "label"])
def test_unreviewed_source_or_identity_is_rejected_before_stopping(lab, change):
    if change == "source":
        lab.request["provenance"]["host"] = {"sha256": "different"}
    elif change == "trust":
        config = json.loads(lab.config.read_text())
        config["public_key"] = "different"
        lab.config.write_text(json.dumps(config))
    elif change == "endpoint":
        lab.settings["port"] = 9000
    elif change == "state":
        (lab.state / "host-id").write_text("different-machine")
    else:
        lab.request["jobs"]["host"]["Label"] = "some.other.job"
    with pytest.raises(ValueError):
        migration.prepare(lab.request, lab.root)
    assert not any(call[0] in {"bootout", "register", "bootstrap"} for call in lab.calls)


def test_stop_after_preparation_is_not_undone(lab):
    journal = migration.prepare(lab.request, lab.root)
    lab.loaded.discard("old.host")
    with pytest.raises(ValueError, match="loaded state changed"):
        migration.apply(journal)
    assert "old.host" not in lab.loaded
    assert not any(call[0] == "bootstrap" for call in lab.calls)


def test_rollback_preserves_replacement_switched_off_after_migration(lab):
    journal = migration.prepare(lab.request, lab.root)
    migration.apply(journal)
    lab.statuses["host"] = "not_registered"
    lab.loaded.discard("live.jstack.hub.host")
    migration.rollback(journal)
    assert "old.host" not in lab.loaded
    assert not lab.paths["host"].exists()
    assert migration.load(journal)["stopped_originals"] == ["host"]


def test_configuration_race_during_preparation_rejects_stale_merge(lab, monkeypatch):
    original = migration.statuses

    def changed(settings):
        result = original(settings)
        lab.config.write_text("new external configuration")
        return result

    monkeypatch.setattr(migration, "statuses", changed)
    with pytest.raises(ValueError, match="configuration changed while preparing"):
        migration.prepare(lab.request, lab.root)
    assert lab.config.read_text() == "new external configuration"
    assert not any(call[0] == "bootout" for call in lab.calls)


def test_corrupt_staging_is_rejected_before_retiring_jobs(lab):
    journal = migration.prepare(lab.request, lab.root)
    (journal / "updater.after").write_text("changed")
    with pytest.raises(ValueError, match="staged configuration changed"):
        migration.apply(journal)
    assert all(path.exists() for path in lab.paths.values())


def test_external_configuration_change_is_not_overwritten_by_rollback(lab):
    journal = migration.prepare(lab.request, lab.root)
    migration.apply(journal)
    lab.config.write_text("external change")
    with pytest.raises(ValueError, match="refusing overwrite"):
        migration.rollback(journal)
    assert lab.config.read_text() == "external change"


def test_rollback_does_not_restart_an_unattempted_job_stopped_by_user(lab, monkeypatch):
    journal = migration.prepare(lab.request, lab.root)
    original = migration.provenance
    observations = 0

    def observe(path):
        nonlocal observations
        observations += 1
        result = original(path)
        if observations == 3:
            lab.loaded.discard("old.host")
        return result

    monkeypatch.setattr(migration, "provenance", observe)
    with pytest.raises(ValueError, match="approval or definition changed"):
        migration.apply(journal)
    assert "old.host" not in lab.loaded
    assert not any(call[0] == "bootstrap" for call in lab.calls)


@pytest.mark.parametrize("failure", [None, "authentication", "identity", "sessions"])
def test_verification_observes_authenticated_identity_and_sessions(lab, monkeypatch, failure):
    import httpx
    journal = migration.prepare(lab.request, lab.root)
    migration.apply(journal)
    value = migration.load(journal)
    (lab.state / "internal-token").write_text("existing-test-token")
    (lab.state / "updates/observed.json").write_text(json.dumps({"updater_source": value["identity"]}))
    monkeypatch.setattr(migration.service_inventory, "launch_state", lambda *args: {"state": "running"})
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if not request.headers.get("Authorization"):
            return httpx.Response(200 if failure == "authentication" else 401)
        assert request.headers["Authorization"] == "Bearer existing-test-token"
        if request.url.path.endswith("/host"):
            return httpx.Response(200, json={"host_id": "wrong" if failure == "identity" else "existing-machine",
                                            "source": value["identity"]})
        return httpx.Response(200, json={"sessions": None if failure == "sessions" else []})

    client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    if failure:
        with pytest.raises(ValueError, match={"authentication": "authentication gate", "identity": "identity or source",
                                               "sessions": "sessions API"}[failure]):
            verify_services(value)
    else:
        verify_services(value)
        assert calls == ["/api/jremote/v1/host", "/api/jremote/v1/host", "/api/jremote/v1/sessions/active"]


def test_verification_of_disabled_host_does_not_require_an_api(lab, monkeypatch):
    lab.loaded.clear()
    journal = migration.prepare(lab.request, lab.root)
    migration.apply(journal)
    monkeypatch.setattr(migration.service_inventory, "launch_state", lambda *args: pytest.fail("OFF service probed"))
    verify_services(migration.load(journal))
