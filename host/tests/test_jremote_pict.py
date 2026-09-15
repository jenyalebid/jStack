"""jRemote pict — the thread menu's "what does this session open with".

Pins the contract the app leans on: the render is of the session's OWN
directory, it lands in that seat's pad (inside the read fence, so the phone can
ask the file back), the name is stable so asking twice refreshes one document
instead of stacking two, and a failed render leaves nothing behind for the
viewer to open as though it were the answer.

The renderer is faked — this test is about the route's placement and failure
behaviour, not about what pict prints. `pict`'s own output is pict's test.
"""

import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jstack_host.server import create_app

app = create_app()
from jstack_host import auth, open_path, pict, scratchpad, transcripts

SID = "aaaaaaaa-1111-2222-3333-444444444444"
#: Live in a tmux pane, no transcript yet — spawned a moment ago.
FRESH = "dddddddd-1111-2222-3333-444444444444"
#: Live, but running somewhere that is not a seat: no pad to write into.
OFFSEAT = "eeeeeeee-1111-2222-3333-444444444444"
GONE = "cccccccc-1111-2222-3333-444444444444"
SLUG = "-Users-test-Agents-Testa-chat"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "_expected_token", lambda: "test-token")
    c = TestClient(app)
    c.headers.update({"Authorization": "Bearer test-token"})
    return c


@pytest.fixture
def seat(monkeypatch, tmp_path):
    """A throwaway seat holding one transcript, plus two live-but-transcript-
    less sessions standing in tmux. Returns the seat's workspace."""
    ws = tmp_path / "Agents" / "Testa" / "chat"
    ws.mkdir(parents=True)
    elsewhere = tmp_path / "Projects" / "Something"
    elsewhere.mkdir(parents=True)
    projects = tmp_path / "projects"
    (projects / SLUG).mkdir(parents=True)
    (projects / SLUG / f"{SID}.jsonl").write_text("")

    panes = {FRESH: str(ws), OFFSEAT: str(elsewhere)}

    def _pane_cwd(sid):
        if sid not in panes:
            raise KeyError(sid)
        return panes[sid]

    monkeypatch.setattr(open_path, "pane_cwd", _pane_cwd)
    monkeypatch.setattr(transcripts, "_find_session_cwd",
                        lambda sid: str(ws) if sid == SID else None)
    monkeypatch.setattr(scratchpad, "_projects_dir", lambda: projects)
    monkeypatch.setattr(scratchpad, "_seat_dirs", lambda: [ws])
    monkeypatch.setattr(scratchpad, "_cwd_slug",
                        lambda cwd: SLUG if cwd == str(ws) else "-other")
    return ws


def _fake_pict(monkeypatch, tmp_path, body: str):
    """Stand a script in for the renderer. `body` is bash."""
    fake = tmp_path / "pict"
    fake.write_text("#!/bin/bash\n" + body)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(pict, "PICT", fake)
    return fake


@pytest.fixture
def renderer(monkeypatch, tmp_path):
    """A renderer that echoes the arguments it got — so a test can prove WHAT
    was rendered and in which view, not merely that something was written."""
    return _fake_pict(monkeypatch, tmp_path, 'echo "PICT $*"\n')


def test_renders_the_session_directory_into_that_seats_pad(client, seat, renderer):
    r = client.post(f"/api/jremote/v1/sessions/{SID}/pict")
    assert r.status_code == 200
    body = r.json()

    out = Path(body["path"])
    assert out == seat / "pad" / "pict-chat.md"
    assert body["title"] == "chat · pict"
    # The session's own directory, in the bare view — the reading copy.
    assert out.read_text().strip() == f"PICT {seat} --bare"


def test_native_transcript_selects_codex_even_with_a_stale_registry(client, seat, renderer,
                                                                  tmp_path, monkeypatch):
    from jstack_host import managed, messages
    source = tmp_path / "rollout.jsonl"
    source.write_text('{"type":"session_meta","payload":{"id":"native"}}\n')
    monkeypatch.setattr(messages, "_find_session_file", lambda sid: source)
    monkeypatch.setattr(managed, "open_registry", lambda: {SID: {"engine": "claude"}})
    result = client.post(f"/api/jremote/v1/sessions/{SID}/pict")
    assert result.status_code == 200
    assert Path(result.json()["path"]).read_text().strip() == f"PICT {seat} --engine codex --bare"


def test_the_name_is_stable_so_a_second_ask_refreshes_one_document(client, seat,
                                                                   renderer):
    first = client.post(f"/api/jremote/v1/sessions/{SID}/pict").json()["path"]
    second = client.post(f"/api/jremote/v1/sessions/{SID}/pict").json()["path"]
    assert first == second
    # Nothing left beside it — no second render stacked, no temp file orphaned.
    assert [p.name for p in (seat / "pad").iterdir()] == ["pict-chat.md"]


def test_full_swaps_the_reading_copy_for_the_annotated_view(client, seat, renderer):
    r = client.post(f"/api/jremote/v1/sessions/{SID}/pict?full=true")
    assert r.status_code == 200
    assert Path(r.json()["path"]).read_text().strip() == f"PICT {seat}"


def test_a_session_with_only_a_live_pane_still_renders(client, seat, renderer):
    """No transcript yet is the shape of a session spawned a moment ago — the
    pane's cwd is the same answer, earlier."""
    r = client.post(f"/api/jremote/v1/sessions/{FRESH}/pict")
    assert r.status_code == 200
    assert Path(r.json()["path"]) == seat / "pad" / "pict-chat.md"


def test_a_failed_render_leaves_nothing_behind_and_says_why(client, seat,
                                                            monkeypatch, tmp_path):
    _fake_pict(monkeypatch, tmp_path,
               'echo "half a document"\necho "no walk-up here" >&2\nexit 1\n')
    r = client.post(f"/api/jremote/v1/sessions/{SID}/pict")
    assert r.status_code == 500
    # The renderer's own complaint, not a bare code — it is what tells the user
    # whether to retry or to go look at something.
    assert r.json()["detail"] == "no walk-up here"
    # A half-written render must never be readable as the answer.
    assert not (seat / "pad").exists() or list((seat / "pad").iterdir()) == []


def test_a_failed_render_does_not_replace_the_last_good_one(client, seat,
                                                            monkeypatch, tmp_path):
    _fake_pict(monkeypatch, tmp_path, 'echo "PICT good"\n')
    out = Path(client.post(f"/api/jremote/v1/sessions/{SID}/pict").json()["path"])
    _fake_pict(monkeypatch, tmp_path, 'echo "junk"\nexit 1\n')
    assert client.post(f"/api/jremote/v1/sessions/{SID}/pict").status_code == 500
    assert out.read_text().strip() == "PICT good"


def test_a_session_nothing_can_place_404s(client, seat, renderer):
    r = client.post(f"/api/jremote/v1/sessions/{GONE}/pict")
    assert r.status_code == 404
    assert r.json()["detail"] == "session not found"


def test_a_session_outside_a_seat_404s_rather_than_writing_past_the_fence(
        client, seat, renderer):
    """The pad is what makes the render readable by the app. A session with no
    pad has nowhere the phone could fetch the file back from, so it is refused
    here rather than written somewhere the fence would later reject."""
    r = client.post(f"/api/jremote/v1/sessions/{OFFSEAT}/pict")
    assert r.status_code == 404
    assert "workspace" in r.json()["detail"]


def test_a_host_without_the_renderer_says_so(client, seat, monkeypatch, tmp_path):
    monkeypatch.setattr(pict, "PICT", tmp_path / "nope")
    r = client.post(f"/api/jremote/v1/sessions/{SID}/pict")
    assert r.status_code == 501
    assert "pict" in r.json()["detail"]


def test_pict_requires_token(monkeypatch, seat, renderer):
    monkeypatch.setattr(auth, "_expected_token", lambda: "test-token")
    c = TestClient(app)
    assert c.post(f"/api/jremote/v1/sessions/{SID}/pict").status_code == 401
    assert not (seat / "pad" / "pict-chat.md").exists()


def test_the_render_is_not_readable_while_it_is_being_written(client, seat,
                                                              monkeypatch, tmp_path):
    """The document appears whole or not at all: the render goes to a temp file
    in the pad and is moved into place, so a viewer opening mid-render either
    reads the previous answer or finds nothing — never a truncated one."""
    fake = _fake_pict(monkeypatch, tmp_path,
                      f'ls "{seat}/pad" > "{tmp_path}/seen"\necho done\n')
    assert fake.exists()
    client.post(f"/api/jremote/v1/sessions/{SID}/pict")
    mid = (tmp_path / "seen").read_text().split()
    assert "pict-chat.md" not in mid
    assert all(n.startswith(".pict-") for n in mid), mid
    assert (seat / "pad" / "pict-chat.md").exists()


def test_the_pad_is_created_if_the_seat_never_had_one(client, seat, renderer):
    assert not (seat / "pad").exists()
    assert client.post(f"/api/jremote/v1/sessions/{SID}/pict").status_code == 200
    assert (seat / "pad").is_dir()


def test_a_directory_name_with_a_space_survives_the_round_trip(client, monkeypatch,
                                                               tmp_path, renderer):
    """The path goes to the app as a query parameter on a `jremote://doc` link,
    and a space is where that encoding has broken before."""
    ws = tmp_path / "Agents" / "Testa" / "my seat"
    ws.mkdir(parents=True)
    monkeypatch.setattr(transcripts, "_find_session_cwd", lambda sid: str(ws))
    monkeypatch.setattr(scratchpad, "_seat_dirs", lambda: [ws])
    monkeypatch.setattr(scratchpad, "_projects_dir", lambda: tmp_path / "none")
    monkeypatch.setattr(open_path, "pane_cwd", lambda sid: str(ws))
    r = client.post(f"/api/jremote/v1/sessions/{SID}/pict")
    assert r.status_code == 200
    assert Path(r.json()["path"]) == ws / "pad" / "pict-my seat.md"
    assert r.json()["title"] == "my seat · pict"
