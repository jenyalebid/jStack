from pathlib import Path

import pytest

from jstack_host import bundle_tools


def test_native_closure_deduplicates_and_rewrites_dependencies(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "COPYING").write_text("test license")
    tool, library = source / "tool", source / "libtest.dylib"
    tool.write_bytes(b"tool")
    library.write_bytes(b"library")
    monkeypatch.setattr(bundle_tools, "dependencies", lambda path: [str(library), "/usr/lib/libSystem.B.dylib"])
    calls = []
    monkeypatch.setattr(bundle_tools, "command", lambda argv: calls.append(argv))
    target = tmp_path / "app/tool"
    result = bundle_tools.bundle(tool, target, tmp_path / "app/lib", tmp_path / "app/licenses")
    assert len(result) == 2
    assert result[1].read_bytes() == b"library"
    assert calls[0][1:4] == ["-change", str(library), "@loader_path/lib/" + result[1].name]
    assert calls[1][1:3] == ["-id", "@rpath/" + result[1].name]
    assert len(list((tmp_path / "app/licenses").iterdir())) == 2


def test_unresolved_loader_path_fails_the_build(tmp_path, monkeypatch):
    tool = tmp_path / "tool"
    tool.touch()
    (tmp_path / "LICENSE").touch()
    monkeypatch.setattr(bundle_tools, "dependencies", lambda path: ["@rpath/unknown.dylib"])
    with pytest.raises(ValueError, match="unresolved tool library"):
        bundle_tools.bundle(tool, tmp_path / "app/tool", tmp_path / "app/lib", tmp_path / "app/licenses")


def test_missing_license_fails_the_build(tmp_path, monkeypatch):
    tool = tmp_path / "tool"
    tool.touch()
    with pytest.raises(ValueError, match="no license notice"):
        bundle_tools.bundle(tool, tmp_path / "app/tool", tmp_path / "app/lib", tmp_path / "app/licenses")
