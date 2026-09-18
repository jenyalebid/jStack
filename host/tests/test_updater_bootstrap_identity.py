import json
import base64

import pytest

from jstack_host import cli, install_updater, sourcestamp


@pytest.fixture
def native_updater(tmp_path, monkeypatch):
    from jstack_host import app_services, service_settings, update_app
    public = base64.b64encode(bytes(range(32))).decode()
    state = tmp_path / "state"
    settings = {"schema": 1, "app": str(tmp_path / "Hub.app"), "port": 9345,
                "environment": {"JREMOTE_STATE_DIR": str(state)}}
    config = {"public_key": public, "service_model": "app", "machine": "existing-machine",
              "menubar_path": settings["app"],
              "local_url": "http://127.0.0.1:9345", "client_managed": False}
    path = state / "updates/config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(config))
    monkeypatch.setattr(service_settings, "read", lambda: settings)
    monkeypatch.setattr(app_services, "verify", lambda *args: None)
    monkeypatch.setattr(update_app, "control", lambda *args: {"updater": "enabled"})
    monkeypatch.setattr(install_updater.install_host, "adopt_installed_environment",
                        lambda: pytest.fail("native repair must precede legacy bootstrap"))
    return public, settings, path


@pytest.mark.parametrize("status", ["enabled", "requires_approval", "not_registered"])
def test_native_bootstrap_preserves_trust_distribution_and_disabled_choice(native_updater, monkeypatch, status):
    from jstack_host import update_app
    public, settings, path = native_updater
    original = path.read_bytes()
    calls = []

    def control(*args):
        calls.append(args)
        return {"updater": status}

    monkeypatch.setattr(update_app, "control", control)
    result = install_updater.bootstrap(public)
    assert result["status"] == status
    assert result["machine"] == "existing-machine"
    assert path.read_bytes() == original
    assert len(calls) == 1 and calls[0][1:] == ("status",)


@pytest.mark.parametrize("change", ["public_key", "service_model", "menubar_path", "local_url", "host_capability"])
def test_native_bootstrap_rejects_mismatched_installation(native_updater, change):
    public, _, path = native_updater
    config = json.loads(path.read_text())
    config[change] = "different"
    path.write_text(json.dumps(config))
    original = path.read_bytes()
    with pytest.raises(ValueError):
        install_updater.bootstrap(public)
    assert path.read_bytes() == original


def test_native_bootstrap_cannot_retarget_state_or_channel(native_updater, tmp_path):
    public, _, _ = native_updater
    with pytest.raises(ValueError, match="identity"):
        install_updater.bootstrap(public, state_dir=tmp_path / "other")
    with pytest.raises(ValueError, match="trust"):
        install_updater.bootstrap(public, candidate_test=True)


def test_unconfigured_signed_updater_never_bootstraps_legacy(monkeypatch):
    from jstack_host import app_services, service_settings
    monkeypatch.setattr(service_settings, "read", lambda: {})
    monkeypatch.setattr(app_services, "bundled", lambda: True)
    monkeypatch.setattr(install_updater.install_host, "adopt_installed_environment",
                        lambda: pytest.fail("legacy bootstrap is forbidden"))
    with pytest.raises(ValueError, match="signed installer"):
        install_updater.bootstrap(base64.b64encode(bytes(range(32))).decode())


def test_bootstrap_preserves_release_identity_and_loaded_fingerprint(tmp_path, monkeypatch):
    package = tmp_path / "source/jstack_host"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    identity = {"sha": "a" * 40, "release": "73-source", "build": 73,
                "version": "1.0", "package_sha256": sourcestamp.fingerprint(package)}
    (package.parent / "release-identity.json").write_text(json.dumps(identity))
    staged = install_updater.stage_runtime(package, tmp_path / "state")
    assert json.loads((staged / "release-identity.json").read_text()) == identity
    monkeypatch.setattr(sourcestamp, "_PKG", staged / "jstack_host")
    monkeypatch.setattr(sourcestamp, "_stamp", None)
    monkeypatch.setattr(sourcestamp, "_git", lambda *args: "")
    observed = sourcestamp.capture()
    assert observed["sha"] == identity["sha"]
    assert observed["build"] == 73
    assert observed["dirty"] is False
    assert install_updater.stage_runtime(package, tmp_path / "state") == staged
    identity["build"] = 74
    (package.parent / "release-identity.json").write_text(json.dumps(identity))
    assert install_updater.stage_runtime(package, tmp_path / "state") != staged


def test_updates_enable_is_a_supported_host_command(tmp_path, monkeypatch, capsys):
    called = {}
    monkeypatch.setattr(cli, "_adopt", lambda args: called.setdefault("adopted", True))
    monkeypatch.setattr(
        install_updater, "bootstrap",
        lambda public, state_dir=None: called.update(public=public, state_dir=state_dir)
        or {"supervisor": "installed"})

    args = cli.build_parser().parse_args(
        ["updates", "enable", "--state-dir", str(tmp_path)])
    assert args.fn(args) == 0
    assert called["adopted"] is True
    assert called["state_dir"] == tmp_path
    assert called["public"]
    assert json.loads(capsys.readouterr().out) == {"supervisor": "installed"}
