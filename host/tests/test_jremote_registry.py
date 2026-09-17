"""The jStack agent registry beside the roster — how the default profile draws
its agents, and where a bare id opens.

The filesystem is the roster; `{agent_root}/agents.json` (jStack's registry,
the file `repo-seat` and `day-audit` already read) says how to draw it: the
name, the emoji, the role, and the seat an agent works in. Without it every
card is a bare directory and the app falls back to the robot; with it a fresh
install on a jStack machine shows the fleet the way its owner named it.
"""

import json
import os
from pathlib import Path

import pytest

from jstack_host import hostenv


@pytest.fixture
def root(tmp_path, monkeypatch):
    agents = tmp_path / "Agents"
    for name in ("Ops", "Atlas", "Retired"):
        (agents / name / "chat").mkdir(parents=True)
    monkeypatch.setenv("JREMOTE_HOST_PROFILE", "default")
    monkeypatch.setenv("JREMOTE_INSTANCE_ROOT", str(agents))
    monkeypatch.delenv("JSTACK_AGENT_REGISTRY", raising=False)
    monkeypatch.delenv("JSTACK_REPO_ROOT", raising=False)
    hostenv.reset_profile()
    yield agents
    hostenv.reset_profile()


def _write(agents: Path, reg: dict) -> None:
    (agents / "agents.json").write_text(json.dumps(reg))


def test_without_a_registry_the_directories_are_the_roster(root):
    a = hostenv.active_agents()
    assert set(a) == {"ops", "atlas", "retired"}
    assert a["ops"]["name"] == "Ops" and a["ops"]["emoji"] == ""
    assert hostenv.workspace("ops") == root / "Ops"


def test_the_registry_draws_the_card_and_names_the_seat(root):
    _write(root, {
        "_roles": {"ops": "Operations"},
        "ops": {"name": "Ops Manager", "emoji": "🛠️", "roles": ["ops"],
                "description": "keeps the lights on",
                "workspace": str(root / "Ops" / "chat"),
                "repos": ["Alpine.Storage.Api"]},
        "retired": {"active": False, "name": "Gone"},
    })
    a = hostenv.active_agents()
    assert "retired" not in a, "active: false hides the card"
    ops = a["ops"]
    assert ops["name"] == "Ops Manager" and ops["emoji"] == "🛠️"
    assert ops["roles"] == ["ops"] and ops["description"] == "keeps the lights on"
    assert ops["repos"] == ["Alpine.Storage.Api"]
    # No entry: still an agent, drawn bare.
    assert a["atlas"]["name"] == "Atlas" and a["atlas"]["emoji"] == ""
    # A bare id opens in the registered seat; a sub-mode id still resolves
    # against the directory tree; an agent with no entry opens at its root.
    assert hostenv.workspace("ops") == root / "Ops" / "chat"
    assert hostenv.workspace("ops-chat") == root / "Ops" / "chat"
    assert hostenv.workspace("atlas") == root / "Atlas"


def test_a_registered_seat_that_does_not_exist_falls_back_to_the_root(root):
    _write(root, {"ops": {"workspace": str(root / "Ops" / "nowhere")}})
    assert hostenv.workspace("ops") == root / "Ops"


def test_an_entry_with_no_directory_is_not_an_agent(root):
    _write(root, {"ghost": {"name": "Ghost", "emoji": "👻"}})
    assert "ghost" not in hostenv.active_agents()


def test_a_registry_edit_is_seen_without_a_restart(root):
    _write(root, {"ops": {"emoji": "1"}})
    assert hostenv.active_agents()["ops"]["emoji"] == "1"
    _write(root, {"ops": {"emoji": "22"}})
    os.utime(root / "agents.json", ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
    assert hostenv.active_agents()["ops"]["emoji"] == "22"


def test_a_broken_registry_costs_the_decoration_not_the_roster(root):
    (root / "agents.json").write_text("{not json")
    a = hostenv.active_agents()
    assert set(a) == {"ops", "atlas", "retired"}
    assert a["ops"]["emoji"] == ""


def test_the_registry_path_honours_jstacks_override(root, tmp_path, monkeypatch):
    other = tmp_path / "elsewhere.json"
    other.write_text(json.dumps({"ops": {"emoji": "🎯"}}))
    monkeypatch.setenv("JSTACK_AGENT_REGISTRY", str(other))
    hostenv.reset_profile()
    assert hostenv.active_agents()["ops"]["emoji"] == "🎯"


def test_repos_are_the_checkouts_beside_the_agents(root, tmp_path):
    """The repo root is the parent of the agents root — jStack's own default
    — and a checkout is a directory holding a `.git` directory. A linked
    worktree (`.git` file) is its main checkout's history and is skipped; a
    package checkout inside a repo's build tree is not ours."""
    for name in ("Widget-iOS", "Other"):
        (tmp_path / name / ".git").mkdir(parents=True)
    (tmp_path / "Widget-iOS" / "build" / "pkg" / ".git").mkdir(parents=True)
    (tmp_path / "Widget-iOS-issue-42").mkdir()
    (tmp_path / "Widget-iOS-issue-42" / ".git").write_text("gitdir: ../Widget-iOS/.git/worktrees/x")
    assert hostenv.profile().repo_root() == tmp_path
    assert {p.name for p in hostenv.repos()} == {"Widget-iOS", "Other"}


def test_jstacks_own_clone_is_not_the_users_work(root, tmp_path, monkeypatch):
    """A Mac that has just run the installer holds exactly one checkout —
    jStack's. Unpruned, the Timeline tab opens on a day made entirely of
    commits the user never wrote, which is what a fresh install showed."""
    for name in ("Widget-iOS", "jStack"):
        (tmp_path / name / ".git").mkdir(parents=True)
    plugin = tmp_path / "jStack" / "plugins" / "jstack"
    plugin.mkdir(parents=True)
    monkeypatch.setattr("jstack_host.plugin_paths.jstack_root", lambda: plugin)
    assert {p.name for p in hostenv.repos()} == {"Widget-iOS"}


def test_a_repo_that_merely_contains_jstack_is_still_the_users_work(
        root, tmp_path, monkeypatch):
    """Pruning by containment would take the whole tree with it: a home
    directory that is itself a repo contains the clone, and its history is
    the user's."""
    (tmp_path / ".git").mkdir(parents=True)
    plugin = tmp_path / "jStack" / "plugins" / "jstack"
    plugin.mkdir(parents=True)
    (tmp_path / "jStack" / ".git").mkdir()
    monkeypatch.setattr("jstack_host.plugin_paths.jstack_root", lambda: plugin)
    # The walk stops at the outer checkout, and the outer checkout survives.
    assert [p for p in hostenv.repos()] == [tmp_path]


def test_a_plugin_with_no_clone_prunes_nothing(root, tmp_path, monkeypatch):
    """Installed from the github marketplace there is no `~/jStack` at all —
    the working copy sits under `plugins/cache/` with no `.git` above it."""
    (tmp_path / "Widget-iOS" / ".git").mkdir(parents=True)
    cache = tmp_path / "cache" / "jstack"
    cache.mkdir(parents=True)
    monkeypatch.setattr("jstack_host.plugin_paths.jstack_root", lambda: cache)
    assert {p.name for p in hostenv.repos()} == {"Widget-iOS"}


def test_repo_owner_comes_from_the_registry_with_spelling_folded(root, tmp_path):
    (tmp_path / "Widget-iOS" / ".git").mkdir(parents=True)
    (root / "Widget" / "chat").mkdir(parents=True)
    _write(root, {"widget": {"repos": ["Widget_iOS"]}})
    assert hostenv.repo_agent(tmp_path / "Widget-iOS") == "widget"
    assert hostenv.repo_agent(tmp_path / "Other") == ""


def test_the_jstack_stores_resolve_from_their_own_overrides(root, tmp_path, monkeypatch):
    monkeypatch.setenv("JSTACK_TIMELINE_DIR", str(tmp_path / "tl"))
    monkeypatch.setenv("SCHEDULER_HOME", str(tmp_path / "sched"))
    assert hostenv.timeline_db() == tmp_path / "tl" / "timeline.db"
    assert hostenv.scheduler_dir() == tmp_path / "sched"
    assert hostenv.pings_db() is None


def test_scheduler_split_paths_match_root_and_explicit_overrides(root, tmp_path, monkeypatch):
    for name in ("SCHEDULER_HOME", "SCHEDULER_CONFIG_DIR", "SCHEDULER_STATE_DIR",
                 "JSTACK_CONFIG_DIR", "JSTACK_STATE_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JSTACK_ROOT", str(tmp_path / "stack-root"))
    assert hostenv.scheduler_config_dir() == tmp_path / "stack-root/Config"
    assert hostenv.scheduler_state_dir() == tmp_path / "stack-root/State/scheduler"
    monkeypatch.setenv("JSTACK_STATE_DIR", str(tmp_path / "state"))
    assert hostenv.scheduler_state_dir() == tmp_path / "state/scheduler"
    monkeypatch.setenv("SCHEDULER_HOME", str(tmp_path / "legacy"))
    assert hostenv.scheduler_config_dir() == tmp_path / "legacy/config"
    assert hostenv.scheduler_state_dir() == tmp_path / "legacy/state/scheduler"
    monkeypatch.setenv("SCHEDULER_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("SCHEDULER_STATE_DIR", str(tmp_path / "journal"))
    assert hostenv.scheduler_config_dir() == tmp_path / "config"
    assert hostenv.scheduler_state_dir() == tmp_path / "journal"
