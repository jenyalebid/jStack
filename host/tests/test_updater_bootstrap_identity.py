import json

from jstack_host import cli, install_updater, sourcestamp


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
