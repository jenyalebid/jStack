import io

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
