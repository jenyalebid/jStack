from pathlib import Path

import pytest

from jstack_host import build_hub


@pytest.mark.parametrize("role", build_hub.ROLES)
def test_startup_targets_are_owned_by_the_app(role):
    definition = build_hub.service_plist(role)
    assert definition["Label"] == f"live.jstack.hub.{role}"
    assert definition["BundleProgram"].startswith("Contents/MacOS/JStack")
    assert "Program" not in definition
    assert "UserName" not in definition
    assert "EnvironmentVariables" not in definition
    assert definition["AssociatedBundleIdentifiers"] == ["live.jstack.hub"]


def test_unknown_service_cannot_be_rendered():
    with pytest.raises(KeyError):
        build_hub.service_plist("arbitrary")


def test_relocation_rejects_external_dependencies(monkeypatch, tmp_path):
    monkeypatch.setattr(build_hub, "command", lambda argv: "binary:\n\t/opt/homebrew/lib/foreign.dylib (version 1)\n")
    with pytest.raises(ValueError, match="unbundled dependency"):
        build_hub.relocate(tmp_path / "runtime", Path("/source"), tmp_path)


def test_relocation_requires_bundled_target(monkeypatch, tmp_path):
    monkeypatch.setattr(build_hub, "command", lambda argv: "binary:\n\t/source/lib/missing.dylib (version 1)\n")
    with pytest.raises(ValueError, match="missing bundled library"):
        build_hub.relocate(tmp_path / "runtime", Path("/source"), tmp_path)


def test_relocation_is_relative_to_each_binary(monkeypatch, tmp_path):
    (tmp_path / "Python").touch()
    calls = []

    def command(argv):
        calls.append(argv)
        return "binary:\n\t/source/Python (version 1)\n\t/usr/lib/libSystem.B.dylib (version 1)\n"

    monkeypatch.setattr(build_hub, "command", command)
    build_hub.relocate(tmp_path / "lib/module.so", Path("/source"), tmp_path)
    assert calls[1] == ["/usr/bin/install_name_tool", "-change", "/source/Python",
                        "@loader_path/../Python", str(tmp_path / "lib/module.so")]


def test_macho_does_not_follow_symlinks(tmp_path):
    binary = tmp_path / "binary"
    binary.write_bytes(b"\xcf\xfa\xed\xfe")
    link = tmp_path / "link"
    link.symlink_to(binary)
    assert build_hub.macho(binary)
    assert not build_hub.macho(link)
