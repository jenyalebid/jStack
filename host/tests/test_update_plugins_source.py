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


# ── the release path, end to end on a machine that develops jStack ──────────

def test_staging_and_applying_a_release_leaves_a_checkout_registration_untouched(home, monkeypatch):
    """jarvis#168: prepare() is what staging runs and install()/rollback() are
    what applying and recovering run. On a checkout none of them may write a
    byte of either engine's registration, nor drive either CLI."""
    repo, stack = _checkout(home), _shipped(home)
    _claude(home, repo)
    _codex(home, repo)
    (home / ".codex/config.toml").write_text(
        (home / ".codex/config.toml").read_text()
        + f'[[hooks.SessionStart.hooks]]\ncommand = "{repo}/plugins/jstack/hooks/s.py"\n')
    files = [home / ".claude/plugins/known_marketplaces.json", home / ".codex/config.toml"]
    before = [f.read_bytes() for f in files]
    ran = []
    monkeypatch.setattr(update_plugins, "run", lambda argv: ran.append(argv) or "")
    monkeypatch.setattr(update_plugins.Path, "home", staticmethod(lambda: home))

    providers = update_plugins.prepare()
    update_plugins.install(providers, stack)
    update_plugins.rollback(providers, stack)

    assert providers == [] and ran == []
    assert [f.read_bytes() for f in files] == before


# ── codex_setup: the documented repair takes the registration back ───────────

def _setup_module():
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "tools/codex_setup.py"
    spec = importlib.util.spec_from_file_location("codex_setup_168", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_codex(module, monkeypatch, config: Path):
    """Codex's own semantics, observed on the real CLI: `add` refuses a name
    already added from another source; `remove` drops only the table."""
    calls = []

    def run(argv, check=False, **_):
        calls.append(argv[1:])
        if argv[1:4] == ["plugin", "marketplace", "remove"]:
            text = config.read_text()
            start = text.index("[marketplaces.jstack]")
            end = text.find("\n[", start + 1)
            config.write_text(text[:start] + (text[end + 1:] if end != -1 else ""))
        elif argv[1:4] == ["plugin", "marketplace", "add"]:
            if "[marketplaces.jstack]" in config.read_text():
                raise AssertionError("marketplace 'jstack' is already added from a different source")
            config.write_text(config.read_text()
                              + f'[marketplaces.jstack]\nsource_type = "local"\nsource = "{argv[4]}"\n')
    monkeypatch.setattr(module.subprocess, "run", run)
    return calls


def test_setup_takes_the_registration_back_from_a_release_stage(home, monkeypatch):
    module = _setup_module()
    repo, stage = _checkout(home), _shipped(home)
    config = home / ".codex/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(f'[mcp_servers.job_monitor]\nargs = ["{stage}/host/tools/job_monitor.py", "mcp"]\n'
                      f'[[hooks.SessionStart.hooks]]\ncommand = "{stage}/plugins/jstack/hooks/s.py"\n'
                      f'[marketplaces.jstack]\nsource_type = "local"\nsource = "{stage}"\n')
    calls = _fake_codex(module, monkeypatch, config)

    module.register_marketplace(repo, config)

    import tomllib
    parsed = tomllib.loads(config.read_text())
    assert parsed["marketplaces"]["jstack"]["source"] == str(repo)
    assert str(stage) not in config.read_text()
    assert parsed["hooks"]["SessionStart"]["hooks"][0]["command"] == f"{repo}/plugins/jstack/hooks/s.py"
    assert calls == [["plugin", "marketplace", "remove", "jstack"],
                     ["plugin", "marketplace", "add", str(repo)]]


def test_setup_is_a_no_op_on_its_own_registration(home, monkeypatch):
    module = _setup_module()
    repo = _checkout(home)
    _codex(home, repo)
    config = home / ".codex/config.toml"
    before = config.read_bytes()
    calls = _fake_codex(module, monkeypatch, config)
    module.register_marketplace(repo, config)
    assert calls == [] and config.read_bytes() == before


def test_setup_will_not_take_the_registration_from_another_checkout(home, monkeypatch):
    module = _setup_module()
    repo, other = _checkout(home), _checkout(home, "jStack-worktree")
    _codex(home, other)
    config = home / ".codex/config.toml"
    calls = _fake_codex(module, monkeypatch, config)
    with pytest.raises(SystemExit, match=str(other)):
        module.register_marketplace(repo, config)
    assert calls == []
