import importlib.util
import json
import subprocess
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "menu_build_identity", Path(__file__).parents[1] / "menubar/build_identity.py")
identity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(identity)


def tree(root: Path) -> Path:
    (root / "host").mkdir(parents=True)
    manifest = root / "plugins/jstack/.claude-plugin/plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"version":"1.2.3"}')
    return root


def test_a_release_is_named_by_its_own_sealed_identity(tmp_path):
    repo = tree(tmp_path / "repo")
    (repo / "host/release-identity.json").write_text(json.dumps({"sha": "a" * 40}))
    first = identity.reserve(repo)
    assert first["sha"] == "a" * 40
    assert first["version"] == f"1.2.3+{first['date']}.aaaaaaaa"
    # Rebuilding the same source twice is the same build. The counter this
    # replaced made them differ, which named the machine's history rather
    # than the software.
    assert identity.reserve(repo) == first


def test_a_checkout_is_named_by_its_commit_and_its_dirt(tmp_path):
    repo = tree(tmp_path / "repo")
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True,
                                    capture_output=True)
    run("init", "-q")
    run("config", "user.email", "rig@example.invalid")
    run("config", "user.name", "rig")
    run("add", "-A")
    run("commit", "-qm", "first")
    clean = identity.reserve(repo)
    sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                  text=True).strip()
    assert clean["sha"] == sha
    assert clean["version"] == f"1.2.3+{clean['date']}.{sha[:8]}"
    assert not clean["version"].endswith(".dirty")

    (repo / "plugins/jstack/.claude-plugin/plugin.json").write_text('{"version":"1.2.4"}')
    assert identity.reserve(repo)["version"].endswith(".dirty")


def test_a_copied_tree_is_named_unprovenanced_not_fatal(tmp_path):
    # live-vm-test.sh rsyncs with --exclude '.git', so every test rig runs
    # this case. It used to raise out of `git rev-parse` and take the whole
    # menubar stage down with it.
    built = identity.reserve(tree(tmp_path / "repo"))
    assert built["sha"] == ""
    assert built["version"] == f"1.2.3+{built['date']}.nosource"


def test_the_bundle_version_is_the_day_and_sorts(tmp_path):
    built = identity.reserve(tree(tmp_path / "repo"))
    assert built["bundle"] == built["date"].replace("-", "")
    assert built["bundle"].isdigit() and len(built["bundle"]) == 8


def test_installer_embeds_identity_not_placeholder():
    script = (Path(__file__).parents[1] / "menubar/install.sh").read_text()
    assert '<key>CFBundleVersion</key><string>$BUILD_BUNDLE</string>' in script
    assert '<key>JStackSourceCommit</key><string>$BUILD_SHA</string>' in script
    # No counter survives anywhere in the install path.
    assert "BUILD_NUMBER" not in script
