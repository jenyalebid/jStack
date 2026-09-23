"""A release must not take the marketplace away from a checkout.

`discover()` reads whatever directory the engines have jStack registered
against and `install()` rewrites every reference to point at the release stage
instead. On a leaf that is the whole mechanism: there is no checkout, the
shipped copy is the only copy, and moving the reference is how the plugin
updates at all.

On a machine that DEVELOPS jStack it is a trap with no exit. The stage is one
frozen commit; once the registration points at it, `claude plugin update`
re-reads a directory that cannot change and correctly reports nothing to do,
the nightly currency heal runs forever without landing, and `jstack-doctor`
compares the stage against a cache taken from the stage and calls it agreement.
Every probe that could catch it reads its ground truth from the thing that is
wrong. Observed on a hub 2026-09-17 → 2026-09-21: four nights of a green-ish
self-heal over a plugin pinned at 0.69.3 while the checkout reached 0.69.5.

So the checkout wins, and the release says out loud what it left alone.
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


def test_a_registered_checkout_is_left_alone(home, capsys):
    repo = _checkout(home)
    _claude(home, repo)
    _codex(home, repo)
    assert update_plugins.discover() == []
    said = capsys.readouterr().out
    assert said.count(str(repo)) == 2
    assert "claude" in said and "codex" in said


def test_one_engine_on_a_checkout_does_not_pin_the_other(home):
    """The two registrations are independent — a machine can develop against
    Claude and run Codex from a shipped copy. Skipping must be per engine."""
    repo, shipped = _checkout(home), _shipped(home)
    _claude(home, repo)
    _codex(home, shipped)
    assert [(p["kind"], p["root"]) for p in update_plugins.discover()] == \
        [("codex", str(shipped))]


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
