"""One seat, one sub_mode spelling, whichever engine wrote the transcript.

The Claude branch of `SessionStore._fresh_state` reads Claude Code's project
dir, where every `/` is already `-`, so a nested seat `chat/pad` reaches
`_display_sub_mode` as `chat-pad`. The Codex branch used to join the cwd's
path parts with `/` and never flatten, so one seat was stored under two ids
depending on the engine that wrote the transcript, and a seat query found
only one of them.
"""
import json

from jstack_host import codex_transcript, hostenv, managed, store


def _rollout(sessions, name, cwd):
    path = sessions / name
    path.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": name, "cwd": str(cwd)}}) + "\n")
    return path


def test_codex_rollout_spells_a_nested_seat_like_the_claude_transcript(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    root = tmp_path / "Agents" / "Ada"
    (root / "chat" / "pad" / "probe").mkdir(parents=True)
    monkeypatch.setattr(codex_transcript, "root", lambda: sessions)
    monkeypatch.setattr(store, "_projects_root", lambda: tmp_path / "absent")
    monkeypatch.setattr(managed, "open_registry", lambda: {})
    monkeypatch.setattr(hostenv, "active_agents", lambda: ["ada"])
    monkeypatch.setattr(hostenv, "workspace", lambda base: root)

    shallow = _rollout(sessions, "rollout-shallow.jsonl", root / "chat")
    nested = _rollout(sessions, "rollout-nested.jsonl", root / "chat" / "pad" / "probe")
    db = store.SessionStore(tmp_path / "test.db")
    db.refresh()

    by_path = {r["path"]: r for r in db.query_sessions()}
    # What the Claude branch stores for the same directories.
    from jstack_host.board import _display_sub_mode
    assert by_path[str(shallow)]["sub_mode"] == _display_sub_mode("chat") == "chat"
    assert by_path[str(nested)]["sub_mode"] == _display_sub_mode("chat-pad-probe") == "chat/pad-probe"
    assert by_path[str(nested)]["agent_id"] == "ada"
    # The query by seat finds the Codex row under the same id a Claude row would carry.
    assert {r["path"] for r in db.query_sessions(agent="ada-chat-pad-probe")} == {str(nested)}
