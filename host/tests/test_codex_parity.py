"""Provider boundaries the first compatibility pass failed to exercise."""
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "plugins/jstack"
sys.path.insert(0, str(PLUGIN))


def test_native_startup_preserves_local_overrides_nested_rules_and_memory(tmp_path, monkeypatch):
    home = tmp_path / "home"
    seat = home / "repo/chat"
    seat.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(home / "repo")], check=True)
    (seat / "AGENTS.md").write_text("Native shared instructions")
    (seat / "CLAUDE.md").write_text("Replaced shared instructions")
    (seat / "CLAUDE.local.md").write_text("Local override <!-- hidden comment -->")
    rules = home / ".claude/rules/nested"
    rules.mkdir(parents=True)
    (rules / "all.md").write_text("---\ndescription: rule\n---\nAlways loaded")
    (rules / "scoped.md").write_text('---\npaths: ["**/*.swift"]\n---\nOn demand only')
    (seat / "file.swift").write_text("let value = 1\n")
    memory = home / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(home / "repo")) / "memory/MEMORY.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("\n".join(f"Fact {i}" for i in range(201)))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("JSTACK_RULES_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", raising=False)
    monkeypatch.setenv("JSTACK_REVIEW_CONFIG", str(tmp_path / "absent.json"))
    command = [sys.executable, str(PLUGIN / "hooks/session-start-environment.py")]
    payload = json.dumps({"cwd": str(seat), "transcript_path": "rollout-preview.jsonl"})
    result = subprocess.run(command, input=payload, capture_output=True, text=True, check=True)
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "Local override" in context and "Always loaded" in context
    assert "hidden comment" not in context and "Replaced shared" not in context
    assert "On demand only" not in context
    assert "Fact 199" in context and "Fact 200" not in context
    preview = subprocess.run([sys.executable, str(PLUGIN / "bin/pict"), str(seat),
                              "--engine", "codex", "--bare"], capture_output=True, text=True, check=True)
    assert "Local override" in preview.stdout and "Fact 199" in preview.stdout
    disabled = subprocess.run([sys.executable, str(PLUGIN / "bin/pict"), str(seat),
                               "--engine", "codex", "--bare", "--no-memory"],
                              capture_output=True, text=True, check=True)
    assert "Fact 199" not in disabled.stdout
    full = subprocess.run([sys.executable, str(PLUGIN / "bin/pict"), str(seat),
                           "--engine", "codex"], capture_output=True, text=True, check=True)
    assert "Source weights" in full.stdout and "~tokens" in full.stdout
    assert "On demand only" in full.stdout and "On-demand rules" in full.stdout
    payload = {"cwd": str(seat), "session_id": "native", "tool_name": "apply_patch",
               "transcript_path": "rollout-native.jsonl",
               "tool_input": {"command": "*** Begin Patch\n*** Update File: file.swift\n@@\n-let value = 1\n+let value = 2\n*** End Patch"}}
    result = subprocess.run([sys.executable, str(PLUGIN / "hooks/inject-path-rules.py")],
                            input=json.dumps(payload), capture_output=True, text=True, check=True,
                            env=dict(os.environ, JSTACK_CACHE_ROOT=str(tmp_path / "cache")))
    output = json.loads(result.stdout)["hookSpecificOutput"]
    assert "On demand only" in output["additionalContext"]
    assert "permissionDecision" not in output


@pytest.mark.parametrize("resume", [False, True])
def test_scheduler_uses_native_provider_ids_model_and_durable_history(tmp_path, monkeypatch, resume):
    from scheduler import runner
    calls, launches = [], []
    path = tmp_path / "native.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "source"}}))

    class RPC:
        def __init__(self, **kwargs):
            assert kwargs["env"]["PATH"]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def call(self, method, params):
            calls.append((method, params))
            if method == "thread/read":
                return {"thread": {"model": "gpt-5.6-sol"}}
            return {"thread": {"id": "fork", "path": str(path)}, "model": "gpt-5.6-luna"}

    class Thread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(runner, "CodexRPC", RPC)
    monkeypatch.setattr(runner, "transcripts", lambda sid: [path] if sid == "source" else [])
    monkeypatch.setattr(runner.subprocess, "Popen", lambda argv, **kwargs: (
        launches.append((argv, kwargs)) or SimpleNamespace(pid=123)))
    monkeypatch.setattr(runner.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(runner.threading, "Thread", Thread)
    monkeypatch.setattr(runner.journal, "append", lambda *args: None)
    monkeypatch.setattr(runner.config, "LOGS_DIR", tmp_path / "logs")
    payload = {"message": "Do the scheduled work"}
    if resume:
        payload["resume_session_id"] = "source"
    run = runner.Run({"id": "job", "workspace": str(tmp_path), "engine": "codex",
                      "codex_model": "gpt-5.6-luna", "payload": payload},
                     {"model": "opus", "permission_mode": "bypassPermissions"},
                     datetime.now(timezone.utc), lambda *a, **k: None).spawn()
    run._log_fh.close()
    argv, kwargs = launches[0]
    assert Path(argv[0]).name == "codex" and argv[1:4] == ["exec", "resume", "fork"]
    assert run.session_id == "fork" and run.model == "gpt-5.6-luna" and run._jsonl == path
    assert "opus" not in argv and "CODEX_THREAD_ID" not in kwargs["env"]
    methods = [method for method, params in calls]
    assert methods == (["thread/read", "thread/fork"] if resume else ["thread/start", "thread/inject_items"])
    assert calls[1 if resume else 0][1]["model"] == "gpt-5.6-luna"


def test_schedule_self_books_fresh_native_wake(tmp_path, monkeypatch):
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader("schedule_self_parity", str(PLUGIN / "bin/schedule-self"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    command = []
    monkeypatch.setenv("CODEX_THREAD_ID", "native")
    monkeypatch.setattr(sys, "argv", ["schedule-self", "2099-01-01 09:00", "Continue work"])
    monkeypatch.setattr(module, "_identity", lambda cfg: ("alpha-chat", tmp_path))
    monkeypatch.setattr(module, "_sched_config", lambda cfg: SimpleNamespace(
        SCHEDULE_FILE=tmp_path / "absent.json", DEFAULT_TZ="UTC"))
    monkeypatch.setattr(module, "_check_time", lambda *a: None)
    monkeypatch.setattr(module, "_check_timeout", lambda *a: None)
    monkeypatch.setattr(module.subprocess, "run", lambda argv, **kwargs: (
        command.extend(argv) or SimpleNamespace(returncode=0, stdout='{"id":"job"}')))
    assert module.main() == 0
    assert command[command.index("--engine") + 1] == "codex"
    assert "--resume-session" not in command


def test_native_inbox_wake_is_found_after_environment_injection(tmp_path):
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader("inbox_guard_parity", str(PLUGIN / "hooks/stop-inbox-guard.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    path = tmp_path / "rollout.jsonl"
    path.write_text("\n".join(json.dumps({"type": "response_item", "payload": {
        "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}})
        for text in ("<environment_context>Injected</environment_context>", "[inbox:42] Finish this task")))
    assert module.first_prompt(path) == "[inbox:42] Finish this task"


def test_native_scheduler_summary_keeps_deliverable_before_stop_epilogue(tmp_path):
    from scheduler import runner
    path = tmp_path / "rollout.jsonl"
    path.write_text("\n".join(json.dumps({"type": "response_item", "payload": {
        "type": "message", "role": role, "content": [{"type": kind, "text": text}]}})
        for role, kind, text in (("assistant", "output_text", "Actual deliverable"),
                                 ("user", "input_text", "Stop hook feedback: log your work"),
                                 ("assistant", "output_text", "Logged."))))
    assert runner.last_text_block(path) == "Actual deliverable"
