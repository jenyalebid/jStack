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


def test_embedding_host_uses_its_signed_capability_owner(monkeypatch, installed, tmp_path):
    from pathlib import Path
    owner = tmp_path / "Services.app"
    resources = owner / "Contents/Resources"
    resources.mkdir(parents=True)
    (resources / "automation-catalog.json").write_text(json.dumps({"dashboard": {"job_sha256": "fixture"}}))
    installed.update(host_capability="dashboard", services_app=str(owner))
    expected = (owner, "dashboard", "live.jstack.automation.dashboard")
    assert app_services.specification(Path(installed["app"]), installed, "host") == expected
    calls = []

    def control(app, action, role=None):
        calls.append((app, action, role))
        return {"dashboard": "enabled"} if app == owner else {"host": "not_registered", "menu": "enabled"}

    monkeypatch.setattr(app_services, "control", control)
    assert app_services.observe(Path(installed["app"]), installed)["host"] == "enabled"
    assert calls[-1] == (owner, "status", None)
    installed["host_capability"] = "not-in-catalog"
    with pytest.raises(ValueError, match="sealed capability"):
        app_services.specification(Path(installed["app"]), installed, "host")
