"""Native review dispatch keeps the source identity and survives tmux teardown."""
import importlib.util
import json
import shlex
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

import pytest

from jstack_host import managed, messages, plugin_paths, router

PLUGIN = Path(__file__).resolve().parents[2] / "plugins/jstack"
NATIVE = "aaaaaaaa-1111-2222-3333-444444444444"
BOARD = "bbbbbbbb-1111-2222-3333-444444444444"


@pytest.fixture
def native(tmp_path):
    path = tmp_path / f"rollout-{NATIVE}.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": NATIVE, "cwd": str(tmp_path), "source": "cli"}}) + "\n")
    return path


def test_review_endpoint_resolves_closed_codex_board_handle(native, monkeypatch):
    calls = []
    monkeypatch.setattr(messages, "_find_session_file", lambda sid: native)
    monkeypatch.setattr(router, "_review_spawn_bin", lambda: Path("/review-engine"))
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: calls.append(cmd))
    assert router.review_session(BOARD)["review"] == "spawned"
    assert calls == [["/review-engine", NATIVE, str(native)]]


def test_review_engine_resolves_on_codex_only_install(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    native_home = tmp_path / "native"
    monkeypatch.setenv("CODEX_HOME", str(native_home))
    cache = native_home / "plugins/cache/jstack/jstack"
    older = cache / "0.66.1+codex.20260915201622"
    newer = cache / "0.66.1+codex.20260915204837"
    for version in (older, newer):
        (version / "bin").mkdir(parents=True)
        (version / "bin/session-review-spawn").touch()
    assert router._review_spawn_bin() == newer / "bin/session-review-spawn"


@pytest.mark.parametrize("review", [True, False])
def test_managed_close_dispatches_after_teardown_only_with_review(native, monkeypatch, review):
    calls = []
    spawn = native.parent / "review engine"
    spawn.touch()
    monkeypatch.setattr(messages, "_find_session_file", lambda sid: native)
    monkeypatch.setattr(plugin_paths, "jstack_bin", lambda name: spawn)
    monkeypatch.setattr(managed, "is_open", lambda sid: True)
    monkeypatch.setattr(managed, "client_ttys", lambda sid: [])
    monkeypatch.setattr(managed, "record_close", lambda sid: None)
    monkeypatch.setattr(managed, "close_windows", lambda ttys: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout="", returncode=0))
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: calls.append(cmd))
    assert managed.close_managed(BOARD, review)
    if review:
        script = calls[0][-1]
        dispatch = shlex.join([str(spawn), NATIVE, str(native)])
        assert script.index("kill-session") < script.index(dispatch)
        assert BOARD not in dispatch
    else:
        assert calls == []


def test_review_resolver_reads_native_metadata_and_quotes_paths(tmp_path, monkeypatch):
    agents = tmp_path / "agents with spaces"
    seat = agents / "Alpha/chat"
    seat.mkdir(parents=True)
    (seat / "CLAUDE.md").touch()
    codex = tmp_path / "codex"
    folder = codex / "sessions"
    folder.mkdir(parents=True)
    path = folder / f"rollout-{NATIVE}.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": NATIVE, "cwd": str(seat)}}) + "\n")
    monkeypatch.setenv("CODEX_HOME", str(codex))
    monkeypatch.setenv("JSTACK_AGENTS_DIR", str(agents))
    result = subprocess.run(["bash", str(PLUGIN / "skills/post-session-review/resolve-seat.sh"), NATIVE],
                            capture_output=True, text=True, check=True)
    values = dict(token.split("=", 1) for token in shlex.split(result.stdout))
    assert values == {"JSONL": str(path), "SEAT": "alpha/chat"}


def test_fresh_native_review_uses_codex_and_clears_inherited_identity(native, tmp_path, monkeypatch):
    loader = SourceFileLoader("review_native_test", str(PLUGIN / "bin/session-review-spawn"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    eng = importlib.util.module_from_spec(spec)
    loader.exec_module(eng)
    calls = []
    monkeypatch.setitem(eng.CFG, "agent_root", tmp_path)
    monkeypatch.setitem(eng.CFG, "max_attempts", 1)
    monkeypatch.setattr(eng, "find_session", lambda sid: (native, "alpha"))
    monkeypatch.setattr(eng, "_timeline_max_id", lambda path: 0)
    monkeypatch.setattr(eng, "validate_review_output", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(eng, "_log", lambda line: None)
    monkeypatch.setenv("CODEX_THREAD_ID", "parent")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent")
    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="review", stderr="")
    monkeypatch.setattr(eng.subprocess, "run", run)
    eng.spawn_review(NATIVE, "alpha", ".")
    cmd, kwargs = calls[0]
    assert cmd[:2] == ["codex", "exec"] and "resume" not in cmd
    assert NATIVE in cmd[-1]
    assert "CODEX_THREAD_ID" not in kwargs["env"]
    assert "CLAUDE_CODE_SESSION_ID" not in kwargs["env"]
    assert kwargs["env"]["SKIP_SESSION_HOOK"] == "1"


@pytest.mark.parametrize("name", ["print", "splitoff", "takeover", "tag", "pict"])
def test_native_command_matching_preserves_arguments_and_rejects_suffixes(name):
    sys.path.insert(0, str(PLUGIN / "hooks"))
    path = PLUGIN / "hooks" / f"{name}-command.py"
    spec = importlib.util.spec_from_file_location(f"hook_{name}_native_test", path)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    assert hook.TRIGGER.match(f"$jstack:{name} a b").group(1) == "a b"
    for text in (f"$jstack:{name}-extra", f"/{name}-extra", f"Explain $jstack:{name}"):
        assert not hook.TRIGGER.match(text)


def test_native_hook_answer_is_visible_and_blocks_on_structured_success(native):
    result = subprocess.run([sys.executable, str(PLUGIN / "hooks/print-command.py")],
                            input=json.dumps({"prompt": "$jstack:print",
                                              "transcript_path": str(native)}),
                            capture_output=True, text=True)
    assert result.returncode == 0 and not result.stderr
    output = json.loads(result.stdout)
    assert output == {"continue": False, "stopReason": str(native),
                      "systemMessage": str(native)}


@pytest.mark.parametrize("completed", [False, True])
def test_review_offset_is_recorded_only_after_success(native, tmp_path, monkeypatch, completed):
    loader = SourceFileLoader("review_offset_test", str(PLUGIN / "bin/session-review-spawn"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    eng = importlib.util.module_from_spec(spec)
    loader.exec_module(eng)
    stamps = []
    monkeypatch.setattr(sys, "argv", ["review-engine", NATIVE, str(native)])
    monkeypatch.setattr(eng, "claim_session", lambda sid: True)
    monkeypatch.setattr(eng, "sweep_stale_dubs", lambda: None)
    monkeypatch.setattr(eng.time, "sleep", lambda secs: None)
    monkeypatch.setattr(eng, "find_session", lambda *a: (native, "alpha"))
    monkeypatch.setattr(eng, "reviewable_agents", lambda path: {"alpha": "Alpha"})
    monkeypatch.setattr(eng, "resolve_submode", lambda *a: "chat")
    monkeypatch.setattr(eng, "is_user_engaged", lambda path: True)
    monkeypatch.setattr(eng, "has_recent_activity", lambda path: True)
    monkeypatch.setattr(eng, "has_new_user_prose", lambda *a: True)
    monkeypatch.setattr(eng, "reviewed_offset", lambda sid: 0)
    monkeypatch.setattr(eng, "is_telegram_session", lambda *a: False)
    monkeypatch.setitem(eng.CFG, "min_session_bytes", 0)
    monkeypatch.setattr(eng, "acquire_review_slot", lambda *a: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(eng, "spawn_selfwrite", lambda *a: completed)
    monkeypatch.setattr(eng, "record_reviewed_offset", lambda *a: stamps.append(a))
    eng.main()
    assert stamps == ([(NATIVE, native.stat().st_size)] if completed else [])


def test_pict_native_preview_uses_agents_precedence_and_instruction_bridge(tmp_path, monkeypatch):
    home = tmp_path / "home"
    native_home = home / ".codex"
    native_home.mkdir(parents=True)
    (native_home / "AGENTS.md").write_text("NATIVE_GLOBAL")
    project = home / "repo"
    seat = project / "chat"
    seat.mkdir(parents=True)
    (project / ".git").mkdir()
    (project / "AGENTS.md").write_text("NATIVE_PROJECT")
    (seat / "AGENTS.md").write_text("IGNORED_NATIVE")
    (seat / "AGENTS.override.md").write_text("NATIVE_OVERRIDE")
    (project / "CLAUDE.md").write_text("IGNORED_BRIDGE")
    (home / "CLAUDE.md").write_text("BRIDGED_ORG")
    config = tmp_path / "review.json"
    config.write_text('{"timeline_inject": {}}')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(native_home))
    monkeypatch.setenv("JSTACK_REVIEW_CONFIG", str(config))
    result = subprocess.run([sys.executable, str(PLUGIN / "bin/pict"),
                             str(seat), "--engine", "codex", "--bare"],
                            capture_output=True, text=True, check=True)
    assert "NATIVE_GLOBAL" in result.stdout
    assert result.stdout.index("NATIVE_PROJECT") < result.stdout.index("NATIVE_OVERRIDE")
    assert "BRIDGED_ORG" in result.stdout
    assert "IGNORED_NATIVE" not in result.stdout
    assert "IGNORED_BRIDGE" not in result.stdout
    assert "auto-memory" not in result.stdout
