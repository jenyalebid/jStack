"""Cross-provider contracts: patches, deterministic commands and delivery guards."""
import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from jstack_host import codex_commands, compact_delivery, spawn

PLUGIN = Path(__file__).resolve().parents[2] / "plugins/jstack"
sys.path.insert(0, str(PLUGIN))
import session_runtime as runtime


def test_multi_file_patch_rules_and_renames(tmp_path):
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "swift.md").write_text('---\npaths: ["**/*.swift"]\n---\nSwift convention')
    patch = "*** Begin Patch\n*** Update File: a.txt\n*** Move to: a.swift\n*** Add File: b.swift\n+hi\n*** Delete File: old.swift\n*** End Patch"
    paths = runtime.patch_paths(patch, str(tmp_path))
    assert paths == [str(tmp_path / name) for name in ("a.txt", "a.swift", "b.swift", "old.swift")]
    payload = {"tool_name": "apply_patch", "tool_input": {"command": patch},
               "cwd": str(tmp_path), "session_id": "native"}
    env = dict(os.environ, JSTACK_RULES_DIR=str(rules), JSTACK_CACHE_ROOT=str(tmp_path / "cache"))
    result = subprocess.run([sys.executable, str(PLUGIN / "hooks/inject-path-rules.py")],
                            input=json.dumps(payload), text=True, capture_output=True, env=env, check=True)
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert context.count("Swift convention") == 1
    assert "old.swift" in context


def test_codex_briefing_preserves_prose_without_shell_execution(tmp_path):
    brief = tmp_path / "brief.md"
    prose = 'one\n"two" $(touch SHOULD_NOT_EXIST) `id` \\ end'
    brief.write_text(prose)
    prelude, flags = spawn.build_shell_parts("takeover", str(brief), "", "go", "codex")
    parts = shlex.split(flags)
    assert parts[0] == "-c"
    assert json.loads(parts[1].split("=", 1)[1]) == prose
    assert parts[-1] == "go"
    assert "--name" not in parts and "--append-system-prompt" not in parts
    assert "rm -f" in prelude


def test_remote_commands_reach_hooks_without_model():
    for command in codex_commands.ZERO_TURN:
        assert codex_commands.translate(f"/jstack:{command} a b") == f"JSTACK_{command.upper()}_CMD a b"
        assert codex_commands.translate(f"$jstack:{command} a b") == f"JSTACK_{command.upper()}_CMD a b"
        assert codex_commands.translate(f"/{command}") == f"JSTACK_{command.upper()}_CMD"
    assert codex_commands.translate("/jstack:audit the changes") == "$jstack:audit the changes"
    assert codex_commands.translate("/elevator") == "$jstack:elevator"
    for native in ("/compact", "/model", "/permissions", "a /splitoff", "/splitoff-extra"):
        assert codex_commands.translate(native) == native
    assert codex_commands.translate_paste(b"\x1b[200~/splitoff copy\x1b[201~") == b"\x1b[200~JSTACK_SPLITOFF_CMD copy\x1b[201~"
    assert codex_commands.translate_paste(b"partial\x1b[200~/splitoff") == b"partial\x1b[200~/splitoff"
    assert b"JSTACK_TAKEOVER_CMD focus" in codex_commands.translate_composer("› /takeover focus\n\n footer")
    assert codex_commands.translate_composer("› /takeover\n  prose on another line\n") is None
    assert codex_commands.translate_composer("› /takeover\n\n› Ask Codex to do anything\n") is None


def test_delivery_never_treats_a_draft_as_empty():
    assert compact_delivery.empty_composer("old output\n› Ask Codex to do anything\n\n  model", "codex")
    assert not compact_delivery.empty_composer("› User draft\n", "codex")
    assert not compact_delivery.empty_composer("› \n  second line of draft\n", "codex")
    assert not compact_delivery.empty_composer("loading", "codex")


def test_native_session_facts_and_compaction(tmp_path):
    path = tmp_path / "rollout.jsonl"
    rows = [
        {"type": "session_meta", "payload": {"id": "native", "cwd": str(tmp_path), "source": "cli"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-1"}},
        {"type": "response_item", "payload": {"name": "update_plan", "arguments": json.dumps({"plan": [{"status": "pending"}]})}},
        {"type": "event_msg", "payload": {"type": "task_complete"}},
        {"type": "compacted", "payload": {}},
    ]
    path.write_text("\n".join(map(json.dumps, rows)) + "\n")
    facts = compact_delivery.facts(path, "codex")
    assert facts["turn"] == "idle" and facts["turn_id"] == "turn-1"
    assert facts["open_tasks"] and facts["boundary"] == 1
    assert runtime.engine({"transcript_path": str(path)}) == "codex"
    assert runtime.user_text({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "steering"}]}}) == "steering"


def test_direct_native_resume_uses_native_id_model_and_writer_guard(tmp_path, monkeypatch):
    import pytest
    from jstack_host import turns, messages, managed, transcripts
    path = tmp_path / "rollout.jsonl"
    path.write_text('\n'.join(map(json.dumps, [
        {"type": "session_meta", "payload": {"id": "native", "cwd": str(tmp_path)}},
        {"type": "turn_context", "payload": {"model": "gpt-5.6-luna"}},
    ])))
    monkeypatch.setattr(messages, "_find_session_file", lambda sid: path)
    monkeypatch.setattr(turns, "_live_session_ids", lambda: set())
    monkeypatch.setattr(managed, "open_registry", lambda: {})
    assert transcripts._find_session_cwd("board") == str(tmp_path)
    sid, cmd = turns._native_resume("board", "hello")
    assert sid == "native" and cmd[:3] == ["codex", "exec", "resume"]
    assert cmd[-4:] == ["--model", "gpt-5.6-luna", "native", "hello"]
    monkeypatch.setattr(managed, "open_registry", lambda: {"other-board": {"transcript": str(path)}})
    monkeypatch.setattr(managed, "is_open", lambda sid: True)
    with pytest.raises(turns.TurnError) as error:
        turns._native_resume("native", "hello")
    assert error.value.status == 409


def test_code_mode_write_ledger_keeps_all_patch_paths(tmp_path):
    ledger = tmp_path / "ledger"
    payload = {"tool_name": "apply_patch", "session_id": "native", "cwd": str(tmp_path),
               "tool_input": {"command": "*** Begin Patch\n*** Add File: one.txt\n+one\n*** Update File: two.txt\n*** Move to: three.txt\n@@\n-old\n+new\n*** End Patch"}}
    env = dict(os.environ, JSTACK_SESSION_FILES_DIR=str(ledger))
    subprocess.run([sys.executable, str(PLUGIN / "hooks/record-session-files.py")],
                   input=json.dumps(payload), text=True, env=env, check=True)
    rows = [json.loads(line) for line in (ledger / "native.jsonl").read_text().splitlines()]
    assert {p for row in rows for p in row} == {str(tmp_path / name) for name in ("one.txt", "two.txt", "three.txt")}


def test_codex_session_review_resolves_cwd_and_engagement(tmp_path):
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader("review_compat", str(PLUGIN / "bin/session-review-spawn"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    path = tmp_path / "rollout.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "native", "cwd": str(tmp_path), "source": "cli"}}) + "\n")
    assert module._session_cwd(path) == str(tmp_path)
    assert module.is_user_engaged(path)


def test_native_spend_counts_each_response_once_without_double_counting_cache(tmp_path):
    from jstack_host import spend
    usage = {"type": "token_usage_record", "timestamp": "2026-09-15T19:00:00Z",
             "payload": {"thread_id": "native", "response_id": "one",
                         "usage": {"input_tokens": 100, "cached_input_tokens": 70,
                                   "cache_write_input_tokens": 10, "output_tokens": 5}}}
    inherited = {**usage, "payload": {**usage["payload"], "thread_id": "source", "response_id": "two"}}
    path = tmp_path / "rollout.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in (usage, usage, inherited)))
    rows = spend._scan_codex_file(path, "native")
    assert list(rows.values()) == [[20, 10, 70, 5, 1]]


def test_setup_preserves_mcp_tables_when_codex_moves_comments():
    path = PLUGIN.parents[1] / "host/tools/codex_setup.py"
    spec = importlib.util.spec_from_file_location("setup_compat", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = ('model = "chosen"\n# BEGIN jstack shell\n[shell_environment_policy.set]\nPATH = "old"\n'
                '[mcp_servers.local]\ncommand = "existing"\n# END jstack shell\n')
    changed = module.shell_config(original, PLUGIN, "/bin")
    import tomllib
    parsed = tomllib.loads(changed)
    assert parsed["model"] == "chosen"
    assert parsed["mcp_servers"]["local"]["command"] == "existing"
    assert parsed["shell_environment_policy"]["set"]["PATH"] == "/bin"
    assert module.shell_config(changed, PLUGIN, "/bin") == changed


def test_setup_bridges_commands_scoped_to_nested_seats(tmp_path):
    path = PLUGIN.parents[1] / "host/tools/codex_setup.py"
    spec = importlib.util.spec_from_file_location("setup_seat_commands", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    agents = tmp_path / "Agents"
    profile = tmp_path / "profiles/code/commands"
    profile.mkdir(parents=True)
    command = profile / "distribute.md"
    command.write_text("Build and distribute the selected app.\n")
    seat = agents / "Lynda/code"
    (seat / ".claude").mkdir(parents=True)
    (seat / "CLAUDE.md").write_text("# Code seat\n")
    (seat / ".claude/commands").symlink_to(profile, target_is_directory=True)

    module.share_workspace(agents)

    skill = seat / ".agents/skills/distribute/SKILL.md"
    assert skill.is_file()
    assert str(command.resolve()) in skill.read_text()
    assert codex_commands.translate("/distribute habits", str(seat)) == "$distribute habits"


def test_setup_does_not_bridge_checkout_commands_from_a_seat_pad(tmp_path):
    path = PLUGIN.parents[1] / "host/tools/codex_setup.py"
    spec = importlib.util.spec_from_file_location("setup_pad_commands", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    agents = tmp_path / "Agents"
    checkout = agents / "Jarvis/chat/pad/copied-repo"
    (checkout / ".claude/commands").mkdir(parents=True)
    (checkout / "CLAUDE.md").write_text("# Not a seat\n")
    (checkout / ".claude/commands/foreign.md").write_text("Do foreign work.\n")

    module.share_workspace(agents)

    assert not (checkout / ".agents/skills/foreign/SKILL.md").exists()


def test_startup_watchers_do_not_hold_a_spawning_hook_pipe(monkeypatch):
    from jstack_host import managed
    calls = []
    monkeypatch.setattr(managed.subprocess, "Popen", lambda *a, **kw: calls.append(kw))
    managed._auto_skip_codex_update("test")
    assert calls[0]["stdout"] == subprocess.DEVNULL
    assert calls[0]["stderr"] == subprocess.DEVNULL


def test_explicit_native_resume_binding_is_never_overwritten(tmp_path, monkeypatch):
    from jstack_host import codex_transcript, managed
    source = tmp_path / "source.jsonl"
    source.touch()
    monkeypatch.setattr(managed, "open_registry", lambda: {"board": {"transcript": str(source)}})
    def wrong(*_):
        raise AssertionError("explicit source binding must not be guessed again")
    monkeypatch.setattr(codex_transcript, "rollout_started_after", wrong)
    class InlineThread:
        def __init__(self, target, **kw):
            self.target = target
        def start(self):
            self.target()
    monkeypatch.setattr(codex_transcript.threading, "Thread", InlineThread)
    codex_transcript.bind_open_session("board", 0, attempts=1)
