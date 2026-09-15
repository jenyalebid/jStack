import json
from datetime import datetime
from pathlib import Path

from jstack_host import codex_transcript, load, messages
from jstack_host.store import SessionStore


# Synthetic on purpose. This was a real session id lifted off a real machine,
# which is how the isolation gap above stayed invisible: the id resolved
# against that machine's live index and the test read the actual transcript.
SID = "deadbeef-0000-7000-8000-000000000001"


def _rollout(path: Path) -> Path:
    rows = [
        {"timestamp": "2026-08-26T21:00:00Z", "type": "session_meta",
         "payload": {"session_id": SID, "cwd": "/Users/x/Agents/Nova/chat",
                     "source": "cli"}},
        {"timestamp": "2026-08-26T21:00:01Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text",
                                  "text": "# AGENTS.md instructions for /tmp\nnoise"}]}},
        {"timestamp": "2026-08-26T21:00:02Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "hello"}]}},
        {"timestamp": "2026-08-26T21:00:03Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant", "phase": "commentary",
                     "content": [{"type": "output_text", "text": "working"}]}},
        {"timestamp": "2026-08-26T21:00:04Z", "type": "response_item",
         "payload": {"type": "custom_tool_call", "name": "exec",
                     "input": "echo ok"}},
        {"timestamp": "2026-08-26T21:00:05Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant", "phase": "final_answer",
                     "content": [{"type": "output_text", "text": "done"}]}},
        {"timestamp": "2026-08-26T21:00:05Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {"last_token_usage": {
             "input_tokens": 1234}}}},
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def test_rollout_parser_filters_boot_context_and_keeps_tools(tmp_path):
    path = _rollout(tmp_path / f"rollout-2026-08-26T21-00-00-{SID}.jsonl")
    parsed = codex_transcript.message_entries(path)
    assert [m["text"] for m in parsed if m["role"] == "user"][-1] == "hello"
    assert any(s["type"] == "tool" for m in parsed for s in m["segments"])


def test_store_fold_uses_real_prompt_and_counts_completed_turns(tmp_path):
    path = _rollout(tmp_path / f"rollout-2026-08-26T21-00-00-{SID}.jsonl")
    state = SessionStore(tmp_path / "db.sqlite")._fresh_state(path)
    SessionStore._fold(state, path.read_bytes().splitlines())
    assert state["first_msg"] == "hello"
    assert state["last_prompt"] == "hello"
    assert state["last_msg"] == "done"
    assert state["calls"] == 1
    assert state["last_context"] == 1234


def test_messages_and_load_resolve_native_codex_id(tmp_path, monkeypatch):
    path = _rollout(tmp_path / "sessions" / "2026" / "08" / "26" /
                    f"rollout-2026-08-26T21-00-00-{SID}.jsonl")
    monkeypatch.setattr(codex_transcript, "root", lambda: tmp_path / "sessions")
    monkeypatch.setattr(messages, "_CLAUDE_PROJECTS", tmp_path / "none")
    parsed = messages.parse_session(SID)
    assert [m["text"] for m in parsed["messages"] if m["role"] == "user"] == ["hello"]
    assert load.reading(SID) == {"context": 1234, "turns": 1, "floor": None}


def test_managed_codex_id_resolves_after_the_session_closed(tmp_path, monkeypatch):
    """A History card for a jRemote-started Codex session still opens.

    Its public id is jRemote's board handle — a name the rollout file does not
    carry — and the open registry drops it the moment the session ends. The
    index is the only thing that still knows, and the card is served from it,
    so a card that exists must be a thread that opens.
    """
    board_sid = "98c9dbaf-5733-4250-b635-b52f7c8784ee"
    path = _rollout(tmp_path / "sessions" / "2026" / "08" / "26" /
                    f"rollout-2026-08-26T21-00-00-{SID}.jsonl")
    store = SessionStore(tmp_path / "db.sqlite")
    monkeypatch.setattr("jstack_host.managed.open_registry",
                        lambda: {board_sid: {"transcript": str(path)}})
    store._index_file(path, path.stat())
    assert store.transcript_path(board_sid) == str(path)

    # The session ends: the registry forgets it, every other lookup misses.
    monkeypatch.setattr("jstack_host.managed.open_registry", dict)
    monkeypatch.setattr(messages, "_CLAUDE_PROJECTS", tmp_path / "none")
    monkeypatch.setattr(codex_transcript, "root", lambda: tmp_path / "sessions")
    monkeypatch.setattr("jstack_host.store.get_store", lambda: store)

    assert messages._find_session_file(board_sid) == path
    assert [m["text"] for m in messages.parse_session(board_sid)["messages"]
            if m["role"] == "user"] == ["hello"]
    assert load.reading(board_sid) == {"context": 1234, "turns": 1, "floor": None}


def test_prompt_opening_with_an_attachment_survives(tmp_path):
    """Codex inlines an attachment as markup in the prompt text. The user's words
    are what the card is titled with and what the thread opens on — the
    leading '<' must not read as machine injection and take them with it."""
    typed = "I thought you fixed this shit yesterday"
    # Codex's own shape: the tag is split across blocks, with the image block
    # carrying no text at all and the prose arriving last.
    blocks = [
        {"type": "input_text",
         "text": '<image name=[Image #1] path="/Users/x/pad/shot.png">'},
        {"type": "input_image", "image_url": "data:image/png;base64,AA"},
        {"type": "input_text", "text": "</image>"},
        {"type": "input_text", "text": typed},
    ]
    path = tmp_path / f"rollout-2026-08-26T21-00-00-{SID}.jsonl"
    rows = _rollout(path).read_text().splitlines()
    rows.insert(2, json.dumps(
        {"timestamp": "2026-08-26T21:00:01Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": blocks}}))
    path.write_text("\n".join(rows) + "\n")

    parsed = codex_transcript.message_entries(path)
    assert [m["text"] for m in parsed if m["role"] == "user"] == [typed, "hello"]

    state = SessionStore(tmp_path / "db.sqlite")._fresh_state(path)
    SessionStore._fold(state, path.read_bytes().splitlines())
    assert state["first_msg"] == typed
    assert state["first_real_user_msg"] == typed


def test_rollout_binding_uses_launch_time_and_cwd(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(codex_transcript, "root", lambda: sessions)
    old = _rollout(sessions / "old" / f"rollout-old-{SID}.jsonl")
    current = _rollout(sessions / "new" / f"rollout-new-{SID}.jsonl")
    rows = current.read_text().splitlines()
    meta = json.loads(rows[0])
    meta["payload"]["timestamp"] = "2026-08-26T21:00:10Z"
    current.write_text(json.dumps(meta) + "\n" + "\n".join(rows[1:]) + "\n")
    old_meta = json.loads(old.read_text().splitlines()[0])
    old_meta["payload"]["timestamp"] = "2026-08-26T20:59:00Z"
    old.write_text(json.dumps(old_meta) + "\n")

    launched = datetime.fromisoformat("2026-08-26T21:00:09+00:00").timestamp()
    assert codex_transcript.rollout_started_after(
        launched, "/Users/x/Agents/Nova/chat") == current


def test_reattach_keeps_rollout_link(tmp_path, monkeypatch):
    from jstack_host import managed
    monkeypatch.setattr(managed, '_REG', tmp_path / 'open.json')
    managed.record_open(SID, 'nova', engine='codex', model='gpt-5.6-sol')
    managed.record_transcript(SID, '/tmp/rollout-existing.jsonl')
    managed.record_open(SID, 'nova')
    assert managed._reg_load()[SID]['transcript'] == '/tmp/rollout-existing.jsonl'


def test_delayed_rollout_recovers_without_stealing_a_sibling(tmp_path, monkeypatch):
    from jstack_host import managed
    from types import SimpleNamespace
    monkeypatch.setattr(managed, '_REG', tmp_path / 'open.json')
    sessions = tmp_path / 'sessions'
    monkeypatch.setattr(codex_transcript, 'root', lambda: sessions)
    older = 'aaaaaaaa-0000-0000-0000-000000000000'
    current = 'bbbbbbbb-0000-0000-0000-000000000000'
    cwd = '/Users/x/Agents/Nova/chat'
    start = datetime.fromisoformat('2026-08-26T21:00:00+00:00').timestamp()
    for sid in (older, current):
        managed.record_open(sid, 'nova', engine='codex')
    path = _rollout(sessions / f'rollout-new-{SID}.jsonl')
    lines = path.read_text().splitlines()
    first = json.loads(lines[0]); first['payload']['timestamp'] = '2026-08-26T21:02:00Z'
    path.write_text(json.dumps(first) + '\n' + '\n'.join(lines[1:]) + '\n')
    panes = f'jr-aaaaaaaa\t{start}\t{cwd}\njr-bbbbbbbb\t{start + 60}\t{cwd}\n'
    monkeypatch.setattr(codex_transcript.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=panes))
    result = codex_transcript.recover_open_sessions(managed._reg_load())
    assert 'transcript' not in result[older]
    assert result[current]['transcript'] == str(path)
    assert managed._reg_load()[current]['transcript'] == str(path)


def test_card_metadata_tracks_model_usage_and_turn_events(tmp_path):
    path = _rollout(tmp_path / f'rollout-new-{SID}.jsonl')
    with path.open('a') as fh:
        for event in [
            {'type': 'turn_context', 'payload': {'model': 'gpt-6-astra'}},
            {'type': 'event_msg', 'payload': {'type': 'task_started'}},
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                'last_token_usage': {'input_tokens': 123},
                'total_token_usage': {'total_tokens': 567}}}},
        ]:
            fh.write(json.dumps(event) + '\n')
    facts = codex_transcript.summary(path)
    assert (facts['model'], facts['turn'], facts['context'], facts['tokens']) == ('gpt-6-astra', 'working', 123, 567)
    with path.open('a') as fh:
        fh.write(json.dumps({'type': 'event_msg', 'payload': {'type': 'turn_aborted'}}) + '\n')
        fh.write('{"partial":')
    assert codex_transcript.summary(path)['turn'] == 'idle'
