"""jRemote splitoff — fork a session's transcript and open the copy managed.

Pins the contract the app leans on: the copy is a real dub (new id, sessionId
rewritten, title suffixed " - copy"), the source is untouched, the copy opens
managed with resume, and a failed window rolls the dub back so nothing is left
behind. The dub runs the real jStack `dub-session` adapter against a tmp HOME;
tmux is never touched.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jstack_host.server import create_app

app = create_app()
from jstack_host import auth, managed

# A project key that maps back to a real, existing cwd — _find_session_cwd
# requires the decoded path to exist on disk.
KEY = "-Users-nova-Agents-Nova-chat"
SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeffff0000"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "_expected_token", lambda: "test-token")
    c = TestClient(app)
    c.headers.update({"Authorization": "Bearer test-token"})
    return c


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A throwaway HOME holding one transcript under the real-cwd key.

    The agent root is built here too: a project-dir key is Claude Code's own
    encoding of a working directory, so it only decodes against the instance
    root it was made from — a literal one resolved on exactly one machine.
    """
    import sys
    from jstack_host import hostenv
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "Agents"
    (root / "Nova" / "chat").mkdir(parents=True)
    monkeypatch.setenv("JREMOTE_INSTANCE_ROOT", str(root))
    monkeypatch.setenv("JREMOTE_HOST_PROFILE", "default")
    hostenv.reset_profile()
    monkeypatch.setattr(sys.modules[__name__], "KEY",
                        str(root).replace("/", "-") + "-Nova-chat")
    proj = tmp_path / ".claude" / "projects" / KEY
    proj.mkdir(parents=True)
    lines = [
        {"type": "ai-title", "aiTitle": "menu work", "sessionId": SID},
        {"type": "user", "sessionId": SID, "message": {"content": "hi"}},
        {"type": "assistant", "sessionId": SID, "message": {"content": "yo"}},
    ]
    src = proj / f"{SID}.jsonl"
    src.write_text("".join(json.dumps(l) + "\n" for l in lines))
    return proj


@pytest.fixture
def opened(monkeypatch):
    """Record open_managed calls instead of raising tmux windows."""
    calls = []
    monkeypatch.setattr(managed, "open_managed",
                        lambda sid, cwd, **kw: calls.append((sid, cwd, kw)))
    return calls


def test_splitoff_dubs_and_opens_managed(client, home, opened):
    r = client.post(f"/api/jremote/v1/sessions/{SID}/splitoff")
    assert r.status_code == 200
    new_sid = r.json()["session_id"]
    assert new_sid and new_sid != SID

    # The copy is a self-consistent dub: new sessionId on every line, the
    # picker title carries the fork marker, the source is byte-identical.
    copy = home / f"{new_sid}.jsonl"
    rows = [json.loads(l) for l in copy.read_text().splitlines()]
    assert all(row["sessionId"] == new_sid for row in rows)
    assert rows[0]["aiTitle"] == "menu work - copy"
    src_rows = [json.loads(l) for l in (home / f"{SID}.jsonl").read_text().splitlines()]
    assert all(row["sessionId"] == SID for row in src_rows)

    # The copy opened managed, resuming its own transcript, in the source cwd.
    # The cwd is decoded back out of the project-dir key, so it is the
    # fixture's own root — spelling a literal here would assert the encoding
    # against a path the test never created.
    # The cwd is decoded back out of the project-dir key, so it is the
    # fixture's own root — a literal here would assert the encoding against a
    # path the test never created.
    cwd = str(home.parents[2] / "Agents" / "Nova" / "chat")
    assert opened == [(new_sid, cwd, {"resume": True})]
    # Registered under the workspace's agent — the board shows a CLI row.
    assert managed._reg_load()[new_sid] == {"agent": "nova"}


def test_splitoff_without_transcript_404s(client, home, opened):
    r = client.post("/api/jremote/v1/sessions/aaaaaaaa-0000-0000-0000-000000000000/splitoff")
    assert r.status_code == 404
    assert opened == []


def test_failed_open_rolls_the_dub_back(client, home, monkeypatch):
    def refuse(sid, cwd, **kw):
        raise RuntimeError("tmux is down")
    monkeypatch.setattr(managed, "open_managed", refuse)
    r = client.post(f"/api/jremote/v1/sessions/{SID}/splitoff")
    assert r.status_code == 500
    # Only the source transcript remains — the dub was rolled back, and the
    # registered-first row was cleared with it.
    assert sorted(p.name for p in home.iterdir()) == [f"{SID}.jsonl"]
    assert managed._reg_load() == {}
