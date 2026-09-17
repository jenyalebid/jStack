import json

import pytest

from jstack_host import install_host, service_settings


def test_absent_settings_do_not_invent_an_installation(monkeypatch, tmp_path):
    monkeypatch.setattr(service_settings, "path", lambda: tmp_path / "settings.json")
    assert service_settings.read() == {}


@pytest.mark.parametrize("value", [{}, [], {"schema": 99}, {"schema": 1, "port": 0,
                          "environment": {}, "app": "/Applications/jStack Hub.app"}])
def test_corrupt_settings_fail_closed(monkeypatch, tmp_path, value):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(value))
    monkeypatch.setattr(service_settings, "path", lambda: path)
    with pytest.raises(ValueError):
        service_settings.read()


def test_cli_adopts_same_state_and_port_as_signed_service(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"schema": 1, "port": 9432, "environment": {
        "JREMOTE_STATE_DIR": str(tmp_path / "state"), "WG_PEER_DIR": str(tmp_path / "mesh"),
        "UNRELATED": "not adopted"}, "app": "/Applications/jStack Hub.app"}))
    monkeypatch.setattr(service_settings, "path", lambda: path)
    assert install_host.installed_port() == 9432
    assert install_host.installed_environment() == {
        "JREMOTE_STATE_DIR": str(tmp_path / "state"), "WG_PEER_DIR": str(tmp_path / "mesh")}


def test_explicit_legacy_plist_remains_readable_during_migration(monkeypatch, tmp_path):
    import plistlib
    path = tmp_path / "legacy.plist"
    path.write_bytes(plistlib.dumps({"ProgramArguments": ["python", "--port", "9182"],
                                   "EnvironmentVariables": {"JREMOTE_STATE_DIR": "old-state"}}))
    monkeypatch.setattr(service_settings, "read", lambda: {"port": 9432, "environment": {}})
    assert install_host.installed_port(path) == 9182
    assert install_host.installed_environment(path) == {"JREMOTE_STATE_DIR": "old-state"}


@pytest.mark.parametrize("change", [{"port": True}, {"app": None},
                                    {"environment": {"KEY": None}},
                                    {"environment": {"BAD=KEY": "value"}}])
def test_malformed_settings_fail_with_a_validation_error(monkeypatch, tmp_path, change):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"schema": 1, "port": 9090, "environment": {},
                               "app": "/Applications/jStack Hub.app", **change}))
    monkeypatch.setattr(service_settings, "path", lambda: path)
    with pytest.raises(ValueError):
        service_settings.read()
