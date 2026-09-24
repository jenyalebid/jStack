"""A release must not take the marketplace away from a checkout — nor skip it.

`discover()` reads whatever directory the engines have jStack registered
against and `install()` rewrites every reference to point at the release stage
instead. On a shipped copy that is the whole mechanism: there is no checkout,
the copy is the only copy, and moving the reference is how the plugin updates.

Pointing a checkout at a stage is a trap with no exit. The stage is one frozen
commit; once the registration points at it, `claude plugin update` re-reads a
directory that cannot change and correctly reports nothing to do, the nightly
currency heal runs forever without landing, and `jstack-doctor` compares the
stage against a cache taken from the stage and calls it agreement. Every probe
that could catch it reads its ground truth from the thing that is wrong.
Observed on a hub 2026-09-17 → 2026-09-21: four nights of a green-ish self-heal
over a plugin pinned at 0.69.3 while the checkout reached 0.69.5.

So the checkout's PATH is left alone. Its cache is not: dropping the provider
entirely was the other half of the same bug, and once every Mac installs from a
commit there is no machine it did not silently disable.
"""

import json
from pathlib import Path

import pytest

from jstack_host import update_plugins


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    return tmp_path


def _checkout(home: Path, name: str = "jStack") -> Path:
    d = home / name
    (d / ".git").mkdir(parents=True)
    return d


def _shipped(home: Path) -> Path:
    """A release stage: a real directory of files, and no git anywhere in it."""
    d = home / "state/updates/releases/76-68c646f7/stage-ev0umz_y/stack"
    d.mkdir(parents=True)
    return d


def _claude(home: Path, root: Path):
    mk = home / ".claude/plugins/known_marketplaces.json"
    mk.parent.mkdir(parents=True, exist_ok=True)
    mk.write_text(json.dumps(
        {"jStack": {"source": {"source": "directory", "path": str(root)}}}))
    (home / ".claude/plugins/installed_plugins.json").write_text(
        json.dumps({"plugins": {"jstack@jStack": []}}))


def _codex(home: Path, root: Path, source_type: str = "local"):
    c = home / ".codex/config.toml"
    c.parent.mkdir(parents=True, exist_ok=True)
    c.write_text(f'[marketplaces.jstack]\nsource_type = "{source_type}"\n'
                 f'source = "{root}"\n')


# ── what counts as a checkout ────────────────────────────────────────────────

def test_a_git_directory_is_a_checkout(tmp_path):
    (tmp_path / ".git").mkdir()
    assert update_plugins.serves_a_checkout(str(tmp_path))


def test_a_worktree_is_a_checkout(tmp_path):
    """`.git` is a FILE in a linked worktree. Testing for a directory would
    hand exactly the machines that develop from worktrees back to the stage."""
    (tmp_path / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
    assert update_plugins.serves_a_checkout(str(tmp_path))


def test_a_shipped_copy_is_not_a_checkout(home):
    assert not update_plugins.serves_a_checkout(str(_shipped(home)))


def test_a_source_that_is_not_there_is_not_a_checkout(tmp_path):
    assert not update_plugins.serves_a_checkout(str(tmp_path / "gone"))


# ── discover() ───────────────────────────────────────────────────────────────

def test_a_leaf_still_moves(home):
    """No checkout, so the shipped copy is the only copy and relocating it is
    the entire update path. This must not change."""
    root = _shipped(home)
    _claude(home, root)
    _codex(home, root)
    kinds = {p["kind"]: p["root"] for p in update_plugins.discover()}
    assert kinds == {"claude": str(root), "codex": str(root)}


def test_a_registered_checkout_is_discovered_and_marked_as_one(home):
    """Discovered, because its cache still has to be refreshed; marked, because
    that mark is the only thing standing between it and the stage."""
    repo = _checkout(home)
    _claude(home, repo)
    _codex(home, repo)
    found = {p["kind"]: p for p in update_plugins.discover()}
    assert set(found) == {"claude", "codex"}
    assert all(p["root"] == str(repo) and p["checkout"] for p in found.values())


def test_the_mark_is_per_engine(home):
    """The two registrations are independent — a machine can develop against
    Claude and run Codex from a shipped copy."""
    repo, shipped = _checkout(home), _shipped(home)
    _claude(home, repo)
    _codex(home, shipped)
    found = {p["kind"]: p for p in update_plugins.discover()}
    assert (found["claude"]["root"], found["claude"]["checkout"]) == (str(repo), True)
    assert (found["codex"]["root"], found["codex"]["checkout"]) == (str(shipped), False)


def test_a_checkout_keeps_its_path_and_still_gets_its_cache_refreshed(home, monkeypatch):
    """The whole point. The reference is not rewritten to the stage, and the
    engine is still told to re-read the plugin — from the checkout.

    Skipping the provider did both at once, so on every Mac that installs from
    a commit the plugin never moved and no probe could see that it had not.
    """
    repo, stage = _checkout(home), _shipped(home)
    _claude(home, repo)
    _codex(home, repo)
    asked = []
    monkeypatch.setattr(update_plugins, "run", lambda argv: asked.append(argv) or "[]")
    moved = []
    monkeypatch.setattr(update_plugins, "replace_references",
                        lambda *a: moved.append(a))
    monkeypatch.setattr(update_plugins, "move_shell_references",
                        lambda *a: moved.append(a))
    update_plugins.install(update_plugins.discover(), stage)
    assert moved == []
    assert ["plugin", "update", "jstack@jStack", "--scope", "user"] == asked[0][1:]
    assert str(repo) in asked[1] and str(stage) not in asked[1]


def test_a_shipped_copy_is_still_relocated_onto_the_stage(home, monkeypatch):
    """The leaf path must not change: there the move IS the update."""
    shipped = _shipped(home)
    stage = shipped.parent / "next"
    stage.mkdir()
    _codex(home, shipped)
    asked, moved = [], []
    monkeypatch.setattr(update_plugins, "run", lambda argv: asked.append(argv) or "[]")
    monkeypatch.setattr(update_plugins, "replace_references", lambda *a: moved.append(a))
    monkeypatch.setattr(update_plugins, "move_shell_references", lambda *a: moved.append(a))
    update_plugins.install(update_plugins.discover(), stage)
    assert moved and all(a[-1] == str(stage) for a in moved)
    assert str(stage) in asked[0]


def test_a_non_directory_marketplace_still_refuses_before_the_checkout_test(home):
    """The existing contract: a github marketplace cannot be managed at all,
    and that refusal must not be reordered behind the new guard."""
    mk = home / ".claude/plugins/known_marketplaces.json"
    mk.parent.mkdir(parents=True, exist_ok=True)
    mk.write_text(json.dumps({"jStack": {"source": {"source": "github",
                                                    "repo": "o/jStack"}}}))
    with pytest.raises(Exception, match="local source"):
        update_plugins.discover()


def test_nothing_registered_discovers_nothing(home):
    (home / ".claude/plugins").mkdir(parents=True)
    (home / ".claude/plugins/known_marketplaces.json").write_text("{}")
    assert update_plugins.discover() == []


# ── where the provider CLI is looked for ─────────────────────────────────────
#
# The updater is a catalogued capability under the signed owner, whose PATH is
# the bare system one. A codex installed with `npm -g` sits in Homebrew's bin
# and is invisible from there, so a leaf that had registered the native
# marketplace refused every managed update with "codex CLI is missing" while
# `codex` answered in any of its terminals (work Mac, 2026-09-23). The hub had
# only ever survived through a hand-written shim in ~/.local/bin.

def _service_environment(monkeypatch, home: Path):
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    monkeypatch.setenv("HOME", str(home))  # no ~/.local/bin shim of this machine's


def _brew_codex(tmp_path: Path) -> Path:
    prefix = tmp_path / "homebrew/bin"
    prefix.mkdir(parents=True)
    tool = prefix / "codex"
    tool.write_text("#!/bin/sh\necho \"$PATH\"\n")
    tool.chmod(0o755)
    return prefix


def test_a_provider_cli_off_the_service_path_is_found_on_the_spawn_path(home, monkeypatch, tmp_path):
    _service_environment(monkeypatch, home)
    prefix = _brew_codex(tmp_path)
    from jstack_host import hostenv
    monkeypatch.setattr(hostenv, "spawn_path",
                        lambda *pre, inherit=None: f"{prefix}:{inherit or ''}")
    _codex(home, _shipped(home))
    [provider] = update_plugins.prepare()
    assert provider["binary"] == str(prefix / "codex")


def test_a_provider_cli_runs_with_its_own_directories_on_path(home, monkeypatch, tmp_path):
    """`#!/usr/bin/env node` must resolve too, not just the CLI itself."""
    _service_environment(monkeypatch, home)
    prefix = _brew_codex(tmp_path)
    from jstack_host import hostenv
    monkeypatch.setattr(hostenv, "spawn_path",
                        lambda *pre, inherit=None: f"{prefix}:{inherit or ''}")
    seen = update_plugins.run([str(prefix / "codex")]).strip().split(":")
    assert seen[0] == str(prefix) and "/usr/bin" in seen


def test_a_missing_provider_cli_names_where_it_looked(home, monkeypatch, tmp_path):
    _service_environment(monkeypatch, home)
    from jstack_host import hostenv
    monkeypatch.setattr(hostenv, "spawn_path",
                        lambda *pre, inherit=None: f"{tmp_path / 'empty'}:{inherit or ''}")
    _codex(home, _shipped(home))
    with pytest.raises(update_plugins.ReleaseError, match="codex CLI is missing.*empty"):
        update_plugins.prepare()


# ── the checkout moves to the commit the update installs ─────────────────────


def _git(*argv, cwd):
    import subprocess
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@x", "-c",
                           "commit.gpgsign=false", *argv], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def _origin_and_clone(tmp_path):
    """An origin two commits deep, and a clone left on the first — what
    `install.sh` leaves behind once the ref has moved on."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "--quiet", "-b", "main", cwd=origin)
    plugin = origin / "plugins/jstack/.claude-plugin/plugin.json"
    plugin.parent.mkdir(parents=True)
    plugin.write_text('{"version": "0.78.0"}')
    _git("add", ".", cwd=origin)
    _git("commit", "--quiet", "-m", "0.78.0", cwd=origin)
    first = _git("rev-parse", "HEAD", cwd=origin)
    plugin.write_text('{"version": "0.79.0"}')
    _git("commit", "--quiet", "-am", "0.79.0", cwd=origin)
    second = _git("rev-parse", "HEAD", cwd=origin)
    clone = tmp_path / "jStack"
    _git("clone", "--quiet", str(origin), str(clone), cwd=tmp_path)
    _git("reset", "--quiet", "--hard", first, cwd=clone)
    return clone, first, second


def _provider(clone):
    return {"kind": "claude", "binary": "/bin/true", "root": str(clone), "checkout": True}


def test_a_checkout_is_moved_to_the_commit_the_update_installs(tmp_path, monkeypatch):
    """The commit the manifest names is what `plugin update` then reads. The
    clone follows its branch, so a branch that can fast-forward keeps its name."""
    clone, first, second = _origin_and_clone(tmp_path)
    asked = []
    monkeypatch.setattr(update_plugins, "run", lambda argv: asked.append(argv) or "[]")
    update_plugins.install([_provider(clone)], tmp_path / "stack", second)
    assert _git("rev-parse", "HEAD", cwd=clone) == second
    assert _git("symbolic-ref", "--short", "HEAD", cwd=clone) == "main"
    assert '"0.79.0"' in (clone / "plugins/jstack/.claude-plugin/plugin.json").read_text()
    assert asked[0][1:] == ["plugin", "update", "jstack@jStack", "--scope", "user"]


def test_a_branch_that_cannot_fast_forward_is_left_alone_and_the_checkout_detached(tmp_path, monkeypatch):
    clone, first, second = _origin_and_clone(tmp_path)
    (clone / "local.txt").write_text("mine")
    _git("add", "local.txt", cwd=clone)
    _git("commit", "--quiet", "-m", "local work", cwd=clone)
    mine = _git("rev-parse", "main", cwd=clone)
    monkeypatch.setattr(update_plugins, "run", lambda argv: "[]")
    update_plugins.install([_provider(clone)], tmp_path / "stack", second)
    assert _git("rev-parse", "HEAD", cwd=clone) == second
    assert _git("rev-parse", "main", cwd=clone) == mine, "no branch is moved off its own commits"


def test_an_uncommitted_change_refuses_the_move_by_name(tmp_path, monkeypatch):
    clone, first, second = _origin_and_clone(tmp_path)
    (clone / "plugins/jstack/.claude-plugin/plugin.json").write_text('{"version": "hand-edited"}')
    asked = []
    monkeypatch.setattr(update_plugins, "run", lambda argv: asked.append(argv) or "[]")
    with pytest.raises(update_plugins.ReleaseError, match="uncommitted changes"):
        update_plugins.install([_provider(clone)], tmp_path / "stack", second)
    assert _git("rev-parse", "HEAD", cwd=clone) == first and asked == []


def test_a_checkout_already_on_the_commit_and_a_shipped_copy_are_not_touched(home, tmp_path, monkeypatch):
    clone, first, second = _origin_and_clone(tmp_path)
    _git("reset", "--quiet", "--hard", second, cwd=clone)
    _git("remote", "remove", "origin", cwd=clone)  # a fetch here would fail loudly
    monkeypatch.setattr(update_plugins, "run", lambda argv: "[]")
    monkeypatch.setattr(update_plugins, "replace_references", lambda *a: None)
    monkeypatch.setattr(update_plugins, "move_shell_references", lambda *a: None)
    update_plugins.install([_provider(clone)], tmp_path / "stack", second)
    assert _git("rev-parse", "HEAD", cwd=clone) == second
    shipped = _shipped(home)
    fetched = []
    monkeypatch.setattr(update_plugins, "advance", lambda *a: fetched.append(a))
    update_plugins.install([{**_provider(shipped), "checkout": False}], tmp_path / "stack", second)
    update_plugins.install([_provider(clone)], tmp_path / "stack", None)
    assert fetched == []
