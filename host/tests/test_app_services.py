# NOTE — destructive install/uninstall paths: never run these for real on the home
# machine (the production Hub). Every launchd / JStackHub / sudo boundary must be
# stubbed; conftest fails the test if one is reached. Real proofs run in lab guests.
import io
import json

import pytest

from jstack_host import app_services, install_host, service_settings


@pytest.fixture
def installed(monkeypatch, tmp_path):
    configuration = {"schema": 1, "app": str(tmp_path / "Hub.app"), "port": 9345,
                     "bind": "127.0.0.1", "environment": {"JREMOTE_STATE_DIR": str(tmp_path / "state")}}
    monkeypatch.setattr(service_settings, "read", lambda: configuration)
    monkeypatch.setattr(app_services, "verify", lambda *args: None)
    monkeypatch.setattr(install_host, "_launchctl", lambda *args: pytest.fail("must not touch legacy launchctl definitions"))
    return configuration


@pytest.mark.parametrize("status", ["requires_approval", "not_registered", "not_found"])
def test_repair_preserves_disabled_and_stopped_states(monkeypatch, installed, status):
    calls = []

    def control(app, action, role=None):
        calls.append(action)
        return {"host": status, "menu": "enabled"}

    monkeypatch.setattr(app_services, "control", control)
    assert install_host.install(out=io.StringIO()) == 1
    assert calls == ["status"]


def test_repair_keeps_the_existing_endpoint(monkeypatch, installed):
    monkeypatch.setattr(app_services, "control", lambda *args: {"host": "enabled", "menu": "enabled"})
    ports = []
    monkeypatch.setattr(install_host, "wait_for_health", lambda port: ports.append(port) or {"service": "jremote-host"})
    assert install_host.install(out=io.StringIO()) == 0
    assert ports == [installed["port"]]


def test_repair_refuses_a_different_identity(installed, tmp_path):
    with pytest.raises(ValueError, match="identity or endpoint"):
        install_host.install(state_dir=tmp_path / "new-state", out=io.StringIO())


def test_signed_bundle_cannot_fall_back_to_legacy_install(monkeypatch):
    monkeypatch.setattr(service_settings, "read", lambda: {})
    monkeypatch.setattr(app_services, "bundled", lambda: True)
    monkeypatch.setattr(install_host, "port_answers", lambda *args: pytest.fail("must refuse before probing or writing"))
    with pytest.raises(ValueError, match="signed installer"):
        install_host.install(out=io.StringIO())


def test_embedding_host_resolves_through_the_hubs_sealed_catalog(monkeypatch, installed, tmp_path):
    from pathlib import Path
    app = Path(installed["app"])
    resources = app / "Contents/Resources"
    resources.mkdir(parents=True)
    (resources / "automation-catalog.json").write_text(json.dumps({"dashboard": {"job_sha256": "fixture"}}))
    installed["host_capability"] = "dashboard"
    expected = (app, "dashboard", "live.jstack.automation.dashboard")
    assert app_services.specification(app, installed, "host") == expected
    calls = []

    def control(owner, action, role=None):
        calls.append((owner, action, role))
        return {"host": "not_registered", "menu": "enabled", "dashboard": "enabled"}

    monkeypatch.setattr(app_services, "control", control)
    assert app_services.observe(app, installed)["host"] == "enabled"
    assert calls == [(app, "status", None)]
    installed["host_capability"] = "not-in-catalog"
    with pytest.raises(ValueError, match="sealed capability"):
        app_services.specification(app, installed, "host")


@pytest.fixture
def complete_install(installed, monkeypatch, tmp_path):
    import plistlib
    from pathlib import Path
    from jstack_host import migrate_services
    definitions = {"host": "live.jstack.hub.host", "menu": "live.jstack.hub.menu",
                   "updater": "live.jstack.hub.updater", "worker": "live.jstack.automation.worker"}
    app = Path(installed["app"])
    for directory in ("Resources", "_CodeSignature", "Library/LaunchAgents"):
        (app / "Contents" / directory).mkdir(parents=True)
    (app / "Contents/_CodeSignature/CodeResources").write_bytes(b"hub")
    (app / "Contents/Resources/services.json").write_text(
        json.dumps({role: label + ".plist" for role, label in definitions.items()}))
    for label in definitions.values():
        (app / "Contents/Library/LaunchAgents" / (label + ".plist")).write_bytes(plistlib.dumps({"Label": label}))
    states, calls = {str(app): dict.fromkeys(definitions, "enabled")}, []
    path = tmp_path / "service-settings.json"
    monkeypatch.setattr(service_settings, "path", lambda: path)
    monkeypatch.setattr(migrate_services, "migration_root", lambda: tmp_path / "migrations")

    def control(app, action, role=None):
        if action == "status":
            return dict(states[str(app)])
        assert action == "unregister"
        calls.append(role)
        states[str(app)][role] = "not_registered"
        return {"status": "not_registered"}

    monkeypatch.setattr(app_services, "control", control)
    monkeypatch.setattr(install_host, "wait_unloaded", lambda label: True)
    return states, calls, tmp_path / "migrations/uninstall-journal.json"


def test_all_service_removal_resumes_after_unregister(installed, complete_install, monkeypatch):
    states, calls, path = complete_install
    states[installed["app"]]["worker"] = "requires_approval"
    monkeypatch.setattr(install_host, "wait_unloaded", lambda label: False)
    with pytest.raises(ValueError, match="still loaded"):
        install_host.uninstall(all_services=True, out=io.StringIO())
    journal = json.loads(path.read_text())
    assert "configuration" not in journal and len(journal["configuration_sha256"]) == 64
    assert journal["attempted"] == ["live.jstack.hub.updater"] and journal["stopped"] == []
    assert path.stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(install_host, "wait_unloaded", lambda label: True)
    assert install_host.uninstall(all_services=True, out=io.StringIO()) == 0
    assert calls == ["updater", "menu", "worker", "host"]
    assert json.loads(path.read_text())["state"] == "unregistered"
    assert len(json.loads(path.read_text())["stopped"]) == 4
    assert install_host.uninstall(all_services=True, out=io.StringIO()) == 0
    assert len(calls) == 4


def test_unknown_recovery_ownership_refuses_before_stopping_menu(installed, complete_install):
    states, calls, path = complete_install
    states[installed["app"]]["worker"] = "unknown"
    with pytest.raises(ValueError, match="unobservable"):
        install_host.uninstall(all_services=True, out=io.StringIO())
    assert not calls and not path.exists()


def test_changed_sealed_owner_refuses_resumed_removal(installed, complete_install, monkeypatch):
    from pathlib import Path
    _, calls, _ = complete_install
    monkeypatch.setattr(install_host, "wait_unloaded", lambda label: False)
    with pytest.raises(ValueError, match="still loaded"):
        install_host.uninstall(all_services=True, out=io.StringIO())
    (Path(installed["app"]) / "Contents/_CodeSignature/CodeResources").write_bytes(b"new signed owner")
    with pytest.raises(ValueError, match="ownership changed"):
        install_host.uninstall(all_services=True, out=io.StringIO())
    assert calls == ["updater"]


def test_unknown_host_refuses_basic_uninstall_before_menu(installed, monkeypatch):
    monkeypatch.setattr(app_services, "control", lambda *args: {"menu": "enabled", "host": "unknown"})
    monkeypatch.setattr(install_host, "wait_unloaded", lambda *args: pytest.fail("partial removal"))
    with pytest.raises(ValueError, match="unobservable"):
        install_host.uninstall(out=io.StringIO())


def test_private_settings_are_not_copied_to_removal_journal(installed, complete_install):
    _, _, path = complete_install
    installed["environment"]["FIXTURE_PRIVATE_VALUE"] = "private-fixture-value"
    assert install_host.uninstall(all_services=True, out=io.StringIO()) == 0
    assert "private-fixture-value" not in path.read_text()
    installed["environment"]["FIXTURE_PRIVATE_VALUE"] = "changed-fixture-value"
    with pytest.raises(ValueError, match="ownership changed"):
        install_host.uninstall(all_services=True, out=io.StringIO())


def test_old_removal_journal_is_not_silently_abandoned(installed, complete_install):
    _, calls, path = complete_install
    service_settings.path().with_name("uninstall-journal.json").write_text("{}")
    with pytest.raises(ValueError, match="earlier removal journal"):
        install_host.uninstall(all_services=True, out=io.StringIO())
    assert not path.exists() and calls == []


def _status_out(monkeypatch, installed, *, loaded, records=None):
    """`jstack-host status` with launchd and the record store both stubbed.

    `loaded` maps a role to what launchd would say about it, which is the whole point:
    the record and launchd are two different sources and the command exists to notice
    when they disagree.
    """
    records = records or {"host": "enabled", "menu": "enabled"}
    monkeypatch.setattr(app_services, "control", lambda *args: dict(records))
    monkeypatch.setattr(install_host, "is_loaded",
                        lambda label: loaded[label.rsplit(".", 1)[-1]])
    monkeypatch.setattr(install_host, "health", lambda port: {"service": "jremote-host"})
    monkeypatch.setattr(install_host, "api_answers", lambda port: True)
    out = io.StringIO()
    code = app_services.status(installed, port=None, out=out)
    return code, out.getvalue()


def test_a_role_registered_but_absent_from_launchd_is_reported(monkeypatch, installed):
    """The 2026-09-24 fault, twice in one day: the menu's record said `enabled` while
    launchd held no such job and there was no menu bar item on the screen. This command
    printed a clean `menu enabled` over it both times, so the Hub vouched for a service
    the user could see was gone — a probe reporting state it never observed."""
    code, text = _status_out(monkeypatch, installed,
                             loaded={"host": True, "menu": False})
    assert "record only" in text
    assert code == 1


def test_the_disagreement_names_the_repair(monkeypatch, installed):
    """A stale `enabled` record is also what makes a plain `register` do nothing, so the
    line has to say the record must be dropped first or the reader retries the no-op."""
    _, text = _status_out(monkeypatch, installed, loaded={"host": True, "menu": False})
    assert "unregister then register" in text


def test_the_host_is_cross_examined_too_not_just_the_menu(monkeypatch, installed):
    """The old code asked launchd about the host alone, for the `loaded` line, and never
    compared it with the host's own record."""
    code, text = _status_out(monkeypatch, installed,
                             loaded={"host": False, "menu": True})
    assert "record only" in text.split("menu")[0]
    assert code == 1


def test_agreement_stays_quiet_and_green(monkeypatch, installed):
    """Every role loaded is the normal case and must print exactly what it printed
    before — a status that shouts on a healthy machine is one nobody reads."""
    code, text = _status_out(monkeypatch, installed,
                             loaded={"host": True, "menu": True})
    assert "record only" not in text
    assert "host       enabled" in text and "menu       enabled" in text
    assert code == 0


def test_a_role_that_is_not_enabled_is_not_called_stale(monkeypatch, installed):
    """`not_registered` already agrees with launchd holding nothing. Flagging it would
    turn every deliberately-off capability into a permanent red."""
    code, text = _status_out(monkeypatch, installed,
                             loaded={"host": True, "menu": False},
                             records={"host": "enabled", "menu": "not_registered"})
    assert "record only" not in text
    assert code == 0
