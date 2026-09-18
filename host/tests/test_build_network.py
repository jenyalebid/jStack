import pytest

from jstack_host import build_network


def test_privileged_definition_executes_only_the_bundled_native_helper():
    job = build_network.service_plist()
    assert job["BundleProgram"] == "Contents/MacOS/JStackNetwork"
    assert job["ProgramArguments"] == ["JStackNetwork"]
    assert job["AssociatedBundleIdentifiers"] == ["live.jstack.network"]
    assert job["StandardErrorPath"].startswith("/var/log/")
    assert not {"Program", "EnvironmentVariables", "WorkingDirectory"} & job.keys()


def test_network_builder_refuses_unreviewed_wireguard_source(monkeypatch, tmp_path):
    monkeypatch.setattr(build_network, "command", lambda *args, **kwargs: "unknown-revision")
    with pytest.raises(ValueError, match="unexpected WireGuard"):
        build_network.build(tmp_path, tmp_path, tmp_path / "output", "1.0", None)
    assert not (tmp_path / "output").exists()
