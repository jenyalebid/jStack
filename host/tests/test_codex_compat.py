"""Cross-provider contracts: patches, deterministic commands and delivery guards."""
import importlib.util
import json
import pathlib
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
    """Codex draws no box, so the row under the prompt glyph is either a draft's second
    line or nothing. Claude's is its own border, which is why `boxed` exists."""
    empty = lambda screen: compact_delivery.composer_line(screen, "codex") == ""
    assert empty("old output\n› Ask Codex to do anything\n\n  model")
    assert not empty("› User draft\n")
    assert not empty("› \n  second line of draft\n")
    assert compact_delivery.composer_line("loading", "codex") is None
    # Measured on a live idle pane: the row below `❯` is the composer's bottom border.
    assert compact_delivery.composer_line("❯\u00a0\n" + "─" * 70 + "\n") == ""
    assert compact_delivery.composer_line("❯\u00a0half a message\n" + "─" * 70) == "half a message"


FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def test_delivery_reads_a_real_codex_pane():
    """Against two captures of a live codex pane (v0.156.1, `capture-pane -p -e`), with the
    account-advisory and desktop-app rows removed and nothing else touched.

    The draft capture is the case the whole rule exists for: the composer's FIRST line is
    empty and the words are on the row below it, so a reader that looks only at the glyph
    row calls that box empty and types `/compact` onto the end of somebody's message."""
    idle = (FIXTURES / "codex-pane-idle.txt").read_text()
    draft = (FIXTURES / "codex-pane-draft.txt").read_text()
    # The placeholder is drawn dim, so it is the CLI's text and the box is empty.
    assert compact_delivery.composer_line(idle, "codex") == ""
    assert compact_delivery.composer_line(draft, "codex") == "second line only"
    # Idleness is the rollout's answer, never this pane's: nothing on screen survives
    # typing well enough to be matched.
    assert compact_delivery.pane_is_ready(idle, "codex", turn="idle")
    assert not compact_delivery.pane_is_ready(idle, "codex", turn="working")
    assert not compact_delivery.pane_is_ready(idle, "codex", turn="")
    assert not compact_delivery.pane_is_ready(draft, "codex", turn="idle")
    assert compact_delivery.took_effect(idle, "codex", turn="working")
    # A pane with no composer row at all — launching, or a dialog in its place — is never
    # ready, whatever the rollout says.
    assert not compact_delivery.pane_is_ready("1. Review hooks\n2. Trust all", "codex", turn="idle")


def test_delivery_never_types_into_a_leftover_command_of_somebody_elses():
    """Whether codex re-arms its box on an aborted `/compact` is NOT measured here — claude
    does, and this only pins that the reader would recognise it. The composer row is built
    by putting solid text where the real capture draws its dim placeholder, because solid
    is the whole difference between the CLI's words and a person's (`_typed`).

    It could not have worked while readiness needed a footer, which is why it is a test:
    typing removes codex's only idle marker, so a box with anything in it had nothing left
    to match and every armed command read as "not idle, leave it"."""
    idle = (FIXTURES / "codex-pane-idle.txt").read_text()
    placeholder = "\x1b[2mAsk Codex to do anything\x1b[0m"
    assert placeholder in idle, "the fixture no longer draws its placeholder dim"
    armed = idle.replace(placeholder, "/compact")
    assert compact_delivery.composer_line(armed, "codex") == "/compact"
    assert compact_delivery.was_aborted(armed, "/compact", "codex", turn="idle")
    # Somebody has started arguing with it. Damaged, but theirs: never wiped.
    mine = idle.replace(placeholder, "/compact rewrite it my way")
    assert not compact_delivery.was_aborted(mine, "/compact", "codex", turn="idle")
    # And a dim placeholder is never mistaken for a leftover command.
    assert not compact_delivery.was_aborted(idle, "/compact", "codex", turn="idle")


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
    assert compact_delivery.turn_state(str(path), "codex") == "idle"
    # The boundary is the `compacted` row, and it is the newest thing on file.
    assert compact_delivery.resume_state(str(path), 0, "codex") == "landed"
    assert compact_delivery.freshly_compacted(str(path), "codex")
    # Codex keeps no per-session task dir; its plan is the same fact, in band.
    assert compact_delivery.docket("native", str(path), "codex") == "open"
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
    seat = agents / "Ada/code"
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
    checkout = agents / "Alice/chat/pad/copied-repo"
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


def _codex_setup():
    path = PLUGIN.parents[1] / "host/tools/codex_setup.py"
    spec = importlib.util.spec_from_file_location("setup_docs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_setup_points_codex_at_the_claude_walkup():
    """Without this key Codex reads AGENTS.md and nothing else, so a machine
    jStack installed gave a session no org, no agent and no seat."""
    import tomllib
    module = _codex_setup()
    parsed = tomllib.loads(module.doc_config('[marketplaces.jstack]\nsource = "/x"\n'))
    assert parsed["project_doc_fallback_filenames"] == ["CLAUDE.md"]
    assert parsed["project_doc_max_bytes"] == 262144
    assert parsed["marketplaces"]["jstack"]["source"] == "/x"


def test_walkup_keys_land_above_the_first_table():
    """They are bare keys: written under a table header they silently become
    that table's members, and Codex never sees them."""
    module = _codex_setup()
    written = module.doc_config('[marketplaces.jstack]\nsource = "/x"\n')
    assert written.index("project_doc_fallback_filenames") < written.index("[marketplaces.jstack]")


def test_a_users_own_walkup_answer_is_left_alone():
    module = _codex_setup()
    mine = 'project_doc_fallback_filenames = ["AGENTS.md", "CLAUDE.md"]\n[tui]\n'
    assert module.doc_config(mine) == mine


def test_the_walkup_block_is_written_once():
    module = _codex_setup()
    once = module.doc_config("model = \"chosen\"\n")
    assert module.doc_config(once) == once
    assert once.count("project_doc_max_bytes") == 1


def test_managed_config_carries_every_hook_the_plugin_declares():
    """The manifest is the one definition; this is only its Codex spelling.

    A hook that fails to translate is a hook that silently never runs, so the
    property is not that nothing is dropped — it is that nothing is dropped
    QUIETLY. Every declared handler is either written to the file or named in
    `dropped`, and the only groups allowed in the second set are the ones whose
    matcher names no tool Codex has: those cannot fire whatever we write.
    """
    import tomllib
    from jstack_host import codex_hooks as module
    text, dropped = module.managed_config(PLUGIN)
    parsed = tomllib.loads(text)
    declared = json.loads((PLUGIN / "hooks/hooks.json").read_text())["hooks"]
    unfireable = []
    for event, groups in declared.items():
        # An event only one engine has is carried under the other's spelling, never
        # dropped — see `CODEX_ALIASES`.
        event = module.CODEX_ALIASES.get(event, event)
        written = [h["command"] for group in parsed["hooks"].get(event, ())
                   for h in group["hooks"]]
        for group in groups:
            matcher = group.get("matcher") or ""
            if matcher and not [t for t in matcher.split("|")
                                if t not in module.CODEX_ABSENT_TOOLS]:
                unfireable.append(f"{event}[{matcher}]")
                continue
            for handler in group["hooks"]:
                assert handler["command"].replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN)) in written
    # Nothing else may go missing: an event Codex cannot honour would land here
    # too, and this is the test that has to notice it.
    assert sorted(dropped) == sorted(unfireable)


def test_a_group_codex_could_never_fire_is_left_out_and_said_out_loud():
    """`ExitPlanMode` is Claude's tool. Codex ends plan mode by flipping
    `permission_mode`, so a group matching only that name is dead however it is
    spelled — and a dead line in an operator-owned file is worse than no line,
    because it answers "is the gate wired on Codex" with a yes."""
    from jstack_host import codex_hooks as module
    manifest = {"hooks": {"PreToolUse": [
        {"matcher": "ExitPlanMode", "hooks": [
            {"type": "command", "command": "/gate.py"}]},
        {"matcher": "Bash|ExitPlanMode", "hooks": [
            {"type": "command", "command": "/env.py"}]}]}}
    text, dropped = module.managed_hooks(manifest, Path("/plug"))
    assert "/gate.py" not in text
    assert dropped == ["PreToolUse[ExitPlanMode]"]
    # The mixed group keeps the tool Codex does have, and loses only the name.
    assert 'matcher = "Bash"' in text
    assert "/env.py" in text


def test_managed_config_keeps_codex_event_spelling_and_drops_what_it_cannot_run():
    """Codex ignores an event name it does not know without warning, so a name
    it would not honour must never reach the file."""
    import tomllib
    from jstack_host import codex_hooks as module
    manifest = {"hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/a.py",
                                     "timeout": 7, "additionalContextLimit": 900}]}],
        "Notification": [{"hooks": [{"type": "command", "command": "/dot-up.py"}]}],
        "SomethingOnlyClaudeWillEverFire": [
            {"hooks": [{"type": "command", "command": "/never.py"}]}]}}
    text, dropped = module.managed_hooks(manifest, Path("/plug"))
    parsed = tomllib.loads(text)
    # Notification has a Codex spelling and is carried under it; a name with no
    # counterpart at all is dropped, and said out loud.
    assert sorted(parsed["hooks"]) == ["PermissionRequest", "SessionStart"]
    assert parsed["hooks"]["PermissionRequest"][0]["hooks"][0]["command"] == "/dot-up.py"
    assert dropped == ["SomethingOnlyClaudeWillEverFire"]
    assert "/never.py" not in text
    handler = parsed["hooks"]["SessionStart"][0]["hooks"][0]
    assert handler["command"] == "/plug/a.py"
    assert (handler["timeout"], handler["additionalContextLimit"]) == (7, 900)


def test_a_matcher_rides_the_group_and_not_the_handler():
    import tomllib
    from jstack_host import codex_hooks as module
    manifest = {"hooks": {"PreToolUse": [{"matcher": "Edit|Write",
                                          "hooks": [{"type": "command", "command": "/x.py"}]}]}}
    group = tomllib.loads(module.managed_hooks(manifest, Path("/plug"))[0])["hooks"]["PreToolUse"][0]
    assert group["matcher"] == "Edit|Write"
    assert "matcher" not in group["hooks"][0]


def test_a_handler_codex_cannot_run_is_reported_not_written():
    from jstack_host import codex_hooks as module
    manifest = {"hooks": {"Stop": [{"hooks": [{"type": "prompt", "prompt": "hi"}]}]}}
    text, dropped = module.managed_hooks(manifest, Path("/plug"))
    assert "prompt" not in text
    assert dropped == ["Stop:prompt"]


def test_a_writable_managed_path_needs_no_sudo(tmp_path):
    from jstack_host import codex_hooks as module
    calls = []
    target = tmp_path / "managed_config.toml"
    message = module.install_managed_config(PLUGIN, target, lambda cmd, **kw: calls.append(cmd))
    assert calls == []
    assert "active" in message
    assert "hooks/session-start-inject.py" in target.read_text()


def test_an_unchanged_managed_file_is_not_rewritten(tmp_path):
    """A re-install must ask a person for nothing."""
    from jstack_host import codex_hooks as module
    target = tmp_path / "managed_config.toml"
    module.install_managed_config(PLUGIN, target, lambda cmd, **kw: None)
    before = target.stat().st_mtime_ns
    calls = []
    message = module.install_managed_config(PLUGIN, target, lambda cmd, **kw: calls.append(cmd))
    assert (calls, target.stat().st_mtime_ns) == ([], before)
    assert "already active" in message


def test_an_unwritable_managed_path_goes_through_sudo(tmp_path):
    from jstack_host import codex_hooks as module
    unwritable = tmp_path / "etc"
    unwritable.mkdir(mode=0o500)
    calls = []

    class Result:
        returncode = 0

    def runner(cmd, **kw):
        calls.append(cmd[:2])
        return Result()

    message = module.install_managed_config(PLUGIN, unwritable / "managed_config.toml", runner)
    assert calls == [["sudo", "mkdir"], ["sudo", "cp"], ["sudo", "chmod"]]
    assert "active" in message


def test_a_refused_sudo_is_reported_as_hooks_not_installed(tmp_path):
    """Silence is the failure mode this whole file exists to end."""
    from jstack_host import codex_hooks as module
    unwritable = tmp_path / "etc"
    unwritable.mkdir(mode=0o500)

    class Result:
        returncode = 1

    message = module.install_managed_config(PLUGIN, unwritable / "managed_config.toml",
                                            lambda cmd, **kw: Result())
    assert "NOT installed" in message
