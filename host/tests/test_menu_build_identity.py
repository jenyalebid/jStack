import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "menu_build_identity", Path(__file__).parents[1] / "menubar/build_identity.py")
identity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(identity)


def test_rebuild_reserves_unique_identity_without_git(tmp_path):
    repo = tmp_path / "repo"
    (repo / "host").mkdir(parents=True)
    (repo / "host/release-identity.json").write_text(json.dumps({"sha": "a" * 40}))
    manifest = repo / "plugins/jstack/.claude-plugin/plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"version":"1.2.3"}')
    with ThreadPoolExecutor(max_workers=4) as pool:
        builds = list(pool.map(lambda _: identity.reserve(repo, tmp_path / "state"), range(8)))
    assert sorted(b["build"] for b in builds) == list(range(1, 9))
    assert len({b["version"] for b in builds}) == 8
    assert all(b["sha"] == "a" * 40 for b in builds)
    (tmp_path / "state/build.json").write_text("broken")
    with pytest.raises(ValueError):
        identity.reserve(repo, tmp_path / "state")


def test_installer_embeds_identity_not_placeholder():
    script = (Path(__file__).parents[1] / "menubar/install.sh").read_text()
    assert '<key>CFBundleVersion</key><string>$BUILD_NUMBER</string>' in script
    assert '<key>JStackSourceCommit</key><string>$BUILD_SHA</string>' in script
