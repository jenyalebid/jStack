# NOTE — destructive install/uninstall paths: never run these for real on the home
# machine (the production Hub). Every launchd / JStackHub / sudo boundary must be
# stubbed; conftest fails the test if one is reached. Real proofs run in lab guests.
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jstack_host import emergency_stop as stop, service_settings


@pytest.fixture
def installation(tmp_path, monkeypatch):
    app, services, state = tmp_path / "Hub.app", tmp_path / "Services.app", tmp_path / "state"
    for owner in (app, services):
        (owner / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/MacOS/tmux").touch()
    settings = {"schema": 1, "app": str(app), "services_app": str(services), "port": 9090,
                "environment": {"JREMOTE_STATE_DIR": str(state)}}
    monkeypatch.setattr(service_settings, "read", lambda: settings)
    monkeypatch.setattr(stop.app_services, "verify", lambda *args: None)
    monkeypatch.setattr(stop.app_services, "uninstall_all", lambda config, out: out.write("services removed\n"))
    monkeypatch.setattr(stop, "NETWORK_APP", tmp_path / "Absent Network.app")
    monkeypatch.setattr(stop.os, "geteuid", lambda: 501)
    calls = []

    def run(arguments, **kwargs):
        calls.append(arguments)
        code = 1 if arguments[-1] in {"kill-server", "list-sessions"} else 0
        return SimpleNamespace(returncode=code, stdout="", stderr="no server" if code else "")

    monkeypatch.setattr(stop.subprocess, "run", run)
    return settings, calls


def test_one_switch_stops_services_sessions_and_permissions(installation):
    settings, calls = installation
    updates = Path(settings["environment"]["JREMOTE_STATE_DIR"]) / "updates"
    updates.mkdir(parents=True)
    (updates / "config.json").write_text(json.dumps({"client_bundle_id": "example.client"}))
    output = io.StringIO()
    assert stop.stop(out=output) == 0
    journal = json.loads(stop.path(settings).read_text())
    assert journal["active"] is True and journal["state"] == "stopped"
    assert journal["network"] == "absent" and journal["managed_sessions"] == "stopped"
    assert journal["user_services"] == "unregistered"
    assert set(journal["permissions_reset"]) == stop.BUNDLE_IDS | {"example.client"}
    assert any(call[-1] == "kill-server" for call in calls)
    assert {call[-1] for call in calls if call[0] == "/usr/bin/tccutil"} == set(journal["permissions_reset"])
    assert stop.active(settings)
    assert "complete" in output.getvalue()


def test_missing_retired_bundle_is_already_permission_free(installation, monkeypatch):
    settings, _ = installation
    original = stop.subprocess.run

    def run(arguments, **kwargs):
        if arguments[0] == "/usr/bin/tccutil" and arguments[-1] == "com.jremote.menubar":
            return SimpleNamespace(returncode=64, stdout="", stderr=(
                'tccutil: No such bundle identifier "com.jremote.menubar": '
                "The operation couldn’t be completed. (OSStatus error -10814.)"))
        return original(arguments, **kwargs)

    monkeypatch.setattr(stop.subprocess, "run", run)
    assert stop.stop(out=io.StringIO()) == 0
    journal = json.loads(stop.path(settings).read_text())
    assert journal["permissions_absent"] == ["com.jremote.menubar"]
    assert "com.jremote.menubar" not in journal["permissions_reset"]


def test_installed_network_requires_reviewed_transaction_before_partial_stop(installation, monkeypatch, tmp_path):
    settings, calls = installation
    network = tmp_path / "Network.app"
    network.mkdir()
    monkeypatch.setattr(stop, "NETWORK_APP", network)
    with pytest.raises(ValueError, match="reviewed transaction"):
        stop.stop(out=io.StringIO())
    journal = json.loads(stop.path(settings).read_text())
    assert journal["active"] is True and journal["state"] == "stopping"
    assert not calls


def test_network_uninstall_is_approved_before_user_shutdown(installation, monkeypatch, tmp_path):
    settings, calls = installation
    network = tmp_path / "Network.app"
    network.mkdir()
    transaction = "a" * 32
    settings.update(network_transaction=transaction)
    approvals = []
    monkeypatch.setattr(stop, "NETWORK_APP", network)
    monkeypatch.setattr(stop.network_admin, "approve", lambda app, request, private: (
        approvals.append((app, request, private)) or {"state": "uninstalled", "transaction": transaction}))
    assert stop.stop(out=io.StringIO()) == 0
    assert approvals[0][0] == network
    assert approvals[0][1] == {"schema": 1, "action": "uninstall", "transaction": transaction}
    assert json.loads(stop.path(settings).read_text())["network"] == "uninstalled"


def test_active_refuses_malformed_or_absent_markers(installation):
    settings, _ = installation
    assert not stop.active(settings)
    marker = stop.path(settings)
    marker.parent.mkdir(parents=True)
    marker.write_text('{"schema":1,"active":false}')
    assert not stop.active(settings)
    marker.write_text("not json")
    assert not stop.active(settings)


def test_stop_refuses_root(installation, monkeypatch):
    monkeypatch.setattr(stop.os, "geteuid", lambda: 0)
    with pytest.raises(PermissionError, match="login user"):
        stop.stop(out=io.StringIO())


def test_retry_keeps_the_durable_shutdown_identity(installation):
    settings, _ = installation
    stop.stop(out=io.StringIO())
    first = json.loads(stop.path(settings).read_text())
    stop.stop(out=io.StringIO())
    second = json.loads(stop.path(settings).read_text())
    assert first["id"] == second["id"] and second["state"] == "stopped"
