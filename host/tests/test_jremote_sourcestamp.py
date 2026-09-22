"""The serving-source stamp — captured once, when the bytes load.

An editable install serves whatever the tree held at process startup; the
stamp exists so that fact stops being a guess. Two properties carry it: it is
computed ONCE per process (a stamp that tracked the live tree would describe
bytes the process is not running), and `dirty` covers only the served package
(an edit in the repo's docs changes nothing about what this process answers).
"""

import subprocess
import json
from pathlib import Path

import pytest

from jstack_host import sourcestamp


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *args],
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A throwaway repo shaped like the real one: the served package is a
    subdirectory, with siblings whose edits must not count as dirty."""
    repo = tmp_path / "repo"
    pkg = repo / "host" / "jstack_host"
    pkg.mkdir(parents=True)
    (repo / "docs").mkdir()
    (pkg / "server.py").write_text("bytes v1\n")
    (repo / "docs" / "notes.md").write_text("prose\n")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "one")
    monkeypatch.setattr(sourcestamp, "_PKG", pkg)
    monkeypatch.setattr(sourcestamp, "_stamp", None)
    yield repo, pkg
    sourcestamp._stamp = None


def test_the_stamp_names_the_loaded_commit(checkout):
    repo, _pkg = checkout
    stamp = sourcestamp.capture()
    assert stamp["sha"] == _git(repo, "rev-parse", "HEAD")
    assert stamp["dirty"] is False
    assert Path(stamp["root"]).resolve() == repo.resolve()
    assert sourcestamp.describe(stamp) == stamp["sha"][:12]


def test_packaged_identity_reports_date_and_exact_source(checkout):
    _, pkg = checkout
    identity = {"sha": "a" * 40, "release": "72-aaaaaaaa", "version": "0.69.3",
                "date": "2026-09-21", "package_sha256": sourcestamp.fingerprint(pkg)}
    (pkg.parent / "release-identity.json").write_text(json.dumps(identity))
    stamp = sourcestamp.capture()
    assert stamp["date"] == "2026-09-21"
    assert stamp["version"] == "0.69.3"
    assert stamp["sha"] == "a" * 40
    assert stamp["dirty"] is False


def test_captured_once_the_tree_moving_on_does_not_move_the_stamp(checkout):
    """The whole point: the stamp is what this process LOADED. A later commit
    changes the tree, not the running bytes, and a stamp that followed it
    would report drift as already deployed."""
    repo, pkg = checkout
    loaded = sourcestamp.capture()["sha"]
    (pkg / "server.py").write_text("bytes v2\n")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-aqm", "two")
    assert _git(repo, "rev-parse", "HEAD") != loaded
    assert sourcestamp.capture()["sha"] == loaded


def test_dirty_means_the_served_package_not_the_whole_repo(checkout):
    """An uncommitted edit outside the package does not change what the
    process runs, and flagging it would teach readers the dirty flag cries
    wolf — the habit that hides the real uncommitted-serving case."""
    repo, pkg = checkout
    (repo / "docs" / "notes.md").write_text("edited prose\n")
    sourcestamp._stamp = None
    assert sourcestamp.capture()["dirty"] is False
    (pkg / "server.py").write_text("uncommitted bytes\n")
    sourcestamp._stamp = None
    stamp = sourcestamp.capture()
    assert stamp["dirty"] is True
    assert sourcestamp.describe(stamp).endswith("+dirty")


def test_outside_any_checkout_the_stamp_is_empty_not_wrong(tmp_path, monkeypatch):
    """A real pip install has no tree to drift from; the stamp says so
    instead of inventing an identity."""
    bare = tmp_path / "site-packages" / "jstack_host"
    bare.mkdir(parents=True)
    monkeypatch.setattr(sourcestamp, "_PKG", bare)
    monkeypatch.setattr(sourcestamp, "_stamp", None)
    stamp = sourcestamp.capture()
    assert stamp == {"sha": "", "dirty": False, "root": ""}
    assert sourcestamp.describe(stamp) == "not a checkout"


def bundle_tree(tmp_path, repo="jenyalebid/jStack"):
    """A signed bundle as it is installed: an identity file, and no `.git`."""
    import json
    from jstack_host.sourcestamp import fingerprint
    pkg = tmp_path / "packages" / "jstack_host"
    pkg.mkdir(parents=True)
    (pkg / "server.py").write_text("shipped bytes\n")
    identity = {"sha": "b" * 40, "release": "2026-09-22-bbbbbbbb", "version": "0.69.9",
                "date": "2026-09-22", "package_sha256": fingerprint(pkg)}
    if repo:
        identity["github_repo"] = repo
    (pkg.parent / "release-identity.json").write_text(json.dumps(identity))
    return pkg


def test_a_shipped_bundle_still_knows_where_it_was_published_from(tmp_path, monkeypatch):
    """The installer URL in a joiner file is derived from this.

    `_installer_url` used to ask git for the origin inside `package_root()`.
    In an installed Hub that is a signed app bundle with no `.git` anywhere,
    so the question could only fail — and it did, on the hub itself, every
    time someone asked for a joiner file. The origin rides in the bundle.
    """
    monkeypatch.setattr(sourcestamp, "_PKG", bundle_tree(tmp_path))
    monkeypatch.setattr(sourcestamp, "_stamp", None)
    assert sourcestamp.capture()["github_repo"] == "jenyalebid/jStack"
    assert sourcestamp.github_repo() == "jenyalebid/jStack"


def test_a_bundle_without_an_origin_says_so_rather_than_guessing(tmp_path, monkeypatch):
    monkeypatch.setattr(sourcestamp, "_PKG", bundle_tree(tmp_path, repo=""))
    monkeypatch.setattr(sourcestamp, "_stamp", None)
    assert sourcestamp.github_repo() == ""
