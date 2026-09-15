"""Native titles must refresh independently of transcript activity."""
import json

from jstack_host import codex_transcript, managed, store, hostenv


def test_native_title_refreshes_live_and_history_without_rollout_write(tmp_path, monkeypatch):
    sessions = tmp_path / 'sessions'
    sessions.mkdir()
    monkeypatch.setattr(codex_transcript, 'root', lambda: sessions)
    monkeypatch.setattr(store, '_projects_root', lambda: tmp_path / 'absent')
    monkeypatch.setattr(managed, 'open_registry', lambda: {
        'board-id': {'transcript': str(sessions / 'rollout-native.jsonl')}})
    rollout = sessions / 'rollout-native.jsonl'
    rollout.write_text(json.dumps({'type': 'session_meta', 'payload': {
        'id': 'native', 'cwd': str(tmp_path)}}) + '\n' + json.dumps({
        'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
        'content': [{'type': 'input_text', 'text': 'initial input'}]}}) + '\n')
    index = tmp_path / 'session_index.jsonl'
    monkeypatch.setattr(hostenv, 'project_dir_to_agent', lambda cwd: ('ops', 'chat'))
    db = store.SessionStore(tmp_path / 'test.db')
    db.refresh()
    assert db.query_sessions()[0]['preview'] == 'initial input'
    stamp = rollout.stat().st_mtime_ns
    for name in ('Generated title', 'Renamed session title'):
        with index.open('a') as fh:
            fh.write(json.dumps({'id': 'other', 'thread_name': 'Wrong session'}) + '\n')
            fh.write('{partial\n')
            fh.write(json.dumps({'id': 'native', 'thread_name': name}) + '\n')
        assert codex_transcript.summary(rollout)['title'] == name
        assert db.refresh() == 1
        row = db.query_sessions()[0]
        assert row['session_id'] == 'board-id'
        assert row['preview'] == name
        assert db.query_sessions(agent='ops-chat')[0]['preview'] == name
        assert db.query_sessions(q=name)[0]['preview'] == name
        assert db.refresh() == 0
        assert rollout.stat().st_mtime_ns == stamp
