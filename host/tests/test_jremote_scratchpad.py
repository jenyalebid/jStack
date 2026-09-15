"""jRemote files — the seat's pad over HTTP.

The Files pane reads one folder: `~/Agents/{Name}/{seat}/pad`, the seat's
single shared space. It is reached from an agent id, which is what lets the
pane open before a thread has spawned anything, and from a session id, which
has to land in the same folder or the two views disagree about what the seat
holds.

Pins the route contract: a listing reads exactly one directory and never
descends, so a checkout in the pad costs one row and not its contents; one rel
shape (a path relative to the pad, empty meaning the pad) names a file to fetch
or delete and a folder to open, and can't escape; every session of a seat sees
the same folder; Clear takes the folder it was pressed on and all of it;
uploads land where the pane is pointed and come back marked as the user's; every
route sits behind the bearer token.

Ownership and the sweep are pinned in `test_seat_pads.py`, where the hook that
makes a session's scratchpad this folder is also exercised.
"""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jstack_host.server import create_app

app = create_app()
from jstack_host import auth, open_path, scratchpad

# Must satisfy the router's _SID_RE.
SID = "aaaaaaaa-1111-2222-3333-444444444444"
SIBLING = "bbbbbbbb-1111-2222-3333-444444444444"
#: Spawned, attached and typing — but no turn has landed, so Claude Code has
#: written no transcript for it yet. The pad has to place it anyway.
FRESH = "dddddddd-1111-2222-3333-444444444444"
#: Live too, but running somewhere that is not a seat — no pad exists for it.
OFFSEAT = "eeeeeeee-1111-2222-3333-444444444444"
#: Booted in the seat's own pad, so the harness named its project dir for the
#: pad and not for the seat. `/takeover` did exactly this on 2026-09-15.
WANDERED = "ffffffff-1111-2222-3333-444444444444"
#: Live, and standing in a checkout parked inside the pad — deeper still.
INPAD = "99999999-1111-2222-3333-444444444444"
SLUG = "-Users-test-Agents-Testa-chat"
AGENT = "testa-chat"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "_expected_token", lambda: "test-token")
    c = TestClient(app)
    c.headers.update({"Authorization": "Bearer test-token"})
    return c


@pytest.fixture
def pad(monkeypatch, tmp_path):
    """A throwaway seat with two sessions' transcripts under its slug, plus two
    live-but-transcript-less sessions standing in tmux. Returns the seat's pad —
    the one folder every reading that has a pad resolves to."""
    seat = tmp_path / "Agents" / "Testa" / "chat"
    seat.mkdir(parents=True)
    elsewhere = tmp_path / "Projects" / "Something"
    elsewhere.mkdir(parents=True)
    projects = tmp_path / "projects"
    (projects / SLUG).mkdir(parents=True)
    for sid in (SID, SIBLING):
        (projects / SLUG / f"{sid}.jsonl").write_text("")
    (projects / f"{SLUG}-pad").mkdir()
    (projects / f"{SLUG}-pad" / f"{WANDERED}.jsonl").write_text("")

    panes = {FRESH: str(seat), OFFSEAT: str(elsewhere),
             INPAD: str(seat / scratchpad.PAD / "a-checkout")}

    def _pane_cwd(sid):
        if sid not in panes:
            raise KeyError(sid)     # no live pane to ask
        return panes[sid]

    monkeypatch.setattr(open_path, "pane_cwd", _pane_cwd)
    monkeypatch.setattr(scratchpad, "_projects_dir", lambda: projects)
    monkeypatch.setattr(scratchpad, "_seat_dirs", lambda: [seat])
    monkeypatch.setattr(scratchpad, "_cwd_slug",
                        lambda cwd: SLUG if cwd == str(seat) else "-other")
    monkeypatch.setattr(scratchpad, "workspace",
                        lambda a: seat if a == AGENT else _unknown(a))
    return seat / scratchpad.PAD


def _unknown(agent_id):
    raise KeyError(agent_id)


def _plant(pad, rel, data=b"x", mtime=None):
    p = pad / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


# ── reaching the pad ──

def test_a_session_and_its_agent_reach_the_same_folder(pad):
    assert scratchpad.session_pad(SID) == pad
    assert scratchpad.session_pad(SIBLING) == pad
    assert scratchpad.agent_pad(AGENT) == pad


def test_an_unknown_session_or_agent_raises(pad):
    with pytest.raises(KeyError):
        scratchpad.session_pad("cccccccc-1111-2222-3333-444444444444")
    with pytest.raises(KeyError):
        scratchpad.agent_pad("nobody-chat")


def test_a_session_with_no_transcript_yet_still_reaches_its_pad(pad):
    """The window between spawn and the first turn.

    Claude Code writes no transcript until a turn lands, so a session opened
    from the phone is attachable, typing and visible with nothing on disk
    naming where it runs. Resolving only through the transcript made the whole
    pad 404 in exactly that window — attaching a photo to a brand-new session
    answered "unknown session" while its terminal sat there live.
    """
    assert scratchpad.session_pad(FRESH) == pad


def test_a_session_working_below_its_seat_still_reaches_the_pad(pad):
    """A session's cwd is not a fixed point, and both ways in used to demand
    it match a seat exactly.

    `cd` into the seat's pad — or into a checkout parked there, or a worktree
    — and the harness carries that directory for the rest of the session: it
    names the project dir, and it is what the live pane reports. None of them
    is a seat, deliberately (a checkout in a pad carries its own CLAUDE.md and
    must never be addressable as one), so an equality test placed the session
    nowhere at all and every pad route on it answered "unknown session" while
    its terminal sat there live. 95e01508 spent a morning like that: its
    `/takeover` opened it in the seat's own pad and the host lost it.

    Containment is the question, not identity — one reading for both routes.
    """
    assert scratchpad.session_pad(WANDERED) == pad   # placed by its project dir
    assert scratchpad.session_pad(INPAD) == pad      # placed by its live pane


def test_the_deepest_containing_seat_wins(monkeypatch, tmp_path):
    """Seats nest, so containment alone is not an answer — `Ada/social` holds
    every session under `Ada/social/threads` too. The session belongs to the
    seat whose CLAUDE.md it is actually running under, which is the deeper."""
    outer = tmp_path / "Agents" / "Ada" / "social"
    inner = outer / "threads"
    (inner / scratchpad.PAD).mkdir(parents=True)
    monkeypatch.setattr(scratchpad, "_projects_dir", lambda: tmp_path / "none")
    monkeypatch.setattr(scratchpad, "_seat_dirs", lambda: [outer, inner])
    monkeypatch.setattr(open_path, "pane_cwd",
                        lambda sid: str(inner / scratchpad.PAD / "checkout"))
    assert scratchpad.session_pad(SID) == inner / scratchpad.PAD


def test_the_transcript_still_wins_when_there_is_one(pad, monkeypatch):
    """The live pane is the fallback, not the answer. A session's transcript
    places it whether or not it is still running — which is the only reading
    that survives the session ending."""
    def _boom(sid):
        raise AssertionError(f"asked tmux about {sid} with a transcript on disk")
    monkeypatch.setattr(open_path, "pane_cwd", _boom)
    assert scratchpad.session_pad(SID) == pad


def test_a_live_session_outside_any_seat_has_no_pad(pad):
    """A pad belongs to a seat. A session running somewhere that isn't one
    still resolves to nothing — the fallback widens WHEN a seat can be found,
    never WHAT counts as one."""
    with pytest.raises(KeyError):
        scratchpad.session_pad(OFFSEAT)


# ── one directory at a time ──

def test_the_pad_lists_its_own_level_newest_first(pad):
    _plant(pad, "old.txt", mtime=1000)
    _plant(pad, "new.txt", mtime=2000)
    listing = scratchpad.list_dir(pad)
    assert listing["path"] == ""
    assert [f["name"] for f in listing["files"]] == ["new.txt", "old.txt"]


def test_a_folder_is_one_row_not_its_contents(pad):
    for i in range(30):
        _plant(pad, f"repo/src/{i}.swift")
    listing = scratchpad.list_dir(pad)
    assert [d["name"] for d in listing["dirs"]] == ["repo"]
    assert listing["files"] == []


def test_a_folder_opens_by_its_rel(pad):
    _plant(pad, "repo/README.md")
    listing = scratchpad.list_dir(pad, "repo")
    assert listing["path"] == "repo"
    assert [f["name"] for f in listing["files"]] == ["README.md"]
    assert listing["files"][0]["rel"] == "repo/README.md"


def test_listing_skips_hidden_entries(pad):
    _plant(pad, ".DS_Store")
    _plant(pad, "shown.txt")
    assert [f["name"] for f in scratchpad.list_dir(pad)["files"]] == ["shown.txt"]


def test_a_huge_directory_is_capped_and_says_so(pad, monkeypatch):
    monkeypatch.setattr(scratchpad, "MAX_ROWS", 5)
    for i in range(20):
        _plant(pad, f"{i}.txt")
    listing = scratchpad.list_dir(pad)
    assert listing["truncated"]
    assert len(listing["files"]) == 5


def test_listing_a_missing_or_file_rel_raises(pad):
    _plant(pad, "a.txt")
    for rel in ("nope", "a.txt", "../.."):
        with pytest.raises(FileNotFoundError):
            scratchpad.list_dir(pad, rel)


def test_an_untouched_seat_lists_empty(pad):
    assert scratchpad.list_dir(pad)["files"] == []
    assert scratchpad.list_dir(pad)["dirs"] == []


# ── one rel, both verbs ──

def test_file_path_resolves_by_rel(pad):
    p = _plant(pad, "repo/notes.md", b"hello")
    assert scratchpad.file_path(pad, "repo/notes.md") == p


def test_file_path_refuses_traversal_dirs_and_missing(pad):
    _plant(pad, "repo/x.txt")
    (pad.parent / "secret.txt").write_text("not yours")
    for rel in ("../secret.txt", "repo", "repo/nope.txt"):
        with pytest.raises(FileNotFoundError):
            scratchpad.file_path(pad, rel)


def test_delete_removes_exactly_its_file(pad):
    _plant(pad, "a.txt")
    keep = _plant(pad, "b.txt")
    scratchpad.delete(pad, "a.txt")
    assert not (pad / "a.txt").exists()
    assert keep.exists()


def test_clear_takes_the_folder_it_was_pressed_on_and_stops(pad):
    _plant(pad, "repo/a.txt")
    _plant(pad, "repo/deep/b.txt")
    outside = _plant(pad, "keep.txt")
    assert scratchpad.clear_dir(pad, "repo") == 2
    assert (pad / "repo").is_dir()
    assert list((pad / "repo").iterdir()) == []
    assert outside.exists()


def test_clearing_the_pad_takes_the_whole_folder(pad):
    """The pad's own Clear is the user emptying their own folder — including what they
    put there, which is the one place the ownership asymmetry does not apply."""
    _plant(pad, "mine.log")
    theirs = _plant(pad, "theirs.png")
    scratchpad._mark_boss(theirs)
    _plant(pad, "repo/deep/x")
    assert scratchpad.clear_dir(pad) == 3
    assert list(pad.iterdir()) == []


# ── writing into it ──

def test_save_lands_in_the_folder_it_was_pointed_at(pad):
    (pad / "repo").mkdir(parents=True)
    p = scratchpad.save(pad, "repo", "shot.png", b"bytes")
    assert p == pad / "repo" / "shot.png"
    assert p.read_bytes() == b"bytes"


def test_save_at_the_pads_own_level(pad):
    p = scratchpad.save(pad, "", "shot.png", b"bytes")
    assert p.parent == pad


def test_save_collision_suffixes_never_overwrites(pad):
    scratchpad.save(pad, "", "a.png", b"first")
    second = scratchpad.save(pad, "", "a.png", b"second")
    assert second.name == "a-2.png"
    assert (pad / "a.png").read_bytes() == b"first"


def test_save_sanitizes_traversal_inside_the_pad(pad):
    p = scratchpad.save(pad, "", "../../etc/passwd", b"x")
    assert p.parent == pad
    assert ".." not in p.name


def test_an_upload_is_marked_his(pad):
    p = scratchpad.save(pad, "", "from-phone.png", b"x")
    assert scratchpad.is_boss(p)
    assert scratchpad.list_dir(pad)["files"][0]["boss"] is True


# ── an edit going back where it was opened from ──

def test_a_save_back_lands_on_the_original(pad):
    _plant(pad, "repo/shot.png", b"before")
    p = scratchpad.write_back(pad, "repo/shot.png", b"marked up")
    assert p == pad / "repo" / "shot.png"
    assert p.read_bytes() == b"marked up"
    assert [f["name"] for f in scratchpad.list_dir(pad, "repo")["files"]] == ["shot.png"]


def test_a_save_back_makes_it_theirs(pad):
    """They edited it, so a sweep leaves it — whoever wrote the original."""
    p = _plant(pad, "shot.png", b"agent output")
    assert not scratchpad.is_boss(p)
    scratchpad.write_back(pad, "shot.png", b"marked up")
    assert scratchpad.is_boss(p)


def test_a_save_back_to_a_file_that_went_away_is_not_a_create(pad):
    with pytest.raises(FileNotFoundError):
        scratchpad.write_back(pad, "gone.png", b"x")
    assert not (pad / "gone.png").exists()


def test_a_save_back_cannot_climb_out_of_the_pad(pad):
    outside = pad.parent / "secret.txt"
    outside.write_bytes(b"untouched")
    with pytest.raises(FileNotFoundError):
        scratchpad.write_back(pad, "../secret.txt", b"x")
    assert outside.read_bytes() == b"untouched"


def test_a_save_back_refuses_a_directory(pad):
    (pad / "repo").mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        scratchpad.write_back(pad, "repo", b"x")
    assert (pad / "repo").is_dir()


# ── the routes ──

def test_scratchpad_list_roundtrip(client, pad):
    _plant(pad, "a.png", b"bytes")
    r = client.get(f"/api/jremote/v1/sessions/{SID}/scratchpad")
    assert r.status_code == 200
    assert [f["name"] for f in r.json()["files"]] == ["a.png"]


def test_scratchpad_fetch_bytes(client, pad):
    _plant(pad, "a.txt", b"hello")
    r = client.get(f"/api/jremote/v1/sessions/{SID}/scratchpad/file",
                   params={"rel": "a.txt"})
    assert r.status_code == 200
    assert r.content == b"hello"


def test_scratchpad_fetch_missing_404(client, pad):
    r = client.get(f"/api/jremote/v1/sessions/{SID}/scratchpad/file",
                   params={"rel": "nope.txt"})
    assert r.status_code == 404


def test_scratchpad_unknown_session_404(client, pad):
    r = client.get("/api/jremote/v1/sessions/"
                   "cccccccc-1111-2222-3333-444444444444/scratchpad")
    assert r.status_code == 404


def test_delete_endpoint_removes_one(client, pad):
    _plant(pad, "a.txt")
    keep = _plant(pad, "b.txt")
    r = client.post(f"/api/jremote/v1/sessions/{SID}/scratchpad/delete",
                    json={"rel": "a.txt"})
    assert r.status_code == 200
    assert not (pad / "a.txt").exists()
    assert keep.exists()


def test_delete_endpoint_missing_404(client, pad):
    r = client.post(f"/api/jremote/v1/sessions/{SID}/scratchpad/delete",
                    json={"rel": "nope.txt"})
    assert r.status_code == 404


def test_clear_endpoint_wipes(client, pad):
    _plant(pad, "a.txt")
    _plant(pad, "b.txt")
    r = client.post(f"/api/jremote/v1/sessions/{SID}/scratchpad/clear")
    assert r.status_code == 200
    assert r.json()["cleared"] == 2
    assert list(pad.iterdir()) == []


def test_scratchpad_upload_roundtrip(client, pad):
    r = client.post(f"/api/jremote/v1/sessions/{SID}/scratchpad/upload",
                    params={"filename": "note.txt"}, content=b"hi")
    assert r.status_code == 200
    assert (pad / "note.txt").read_bytes() == b"hi"


def test_scratchpad_upload_empty_400(client, pad):
    r = client.post(f"/api/jremote/v1/sessions/{SID}/scratchpad/upload",
                    params={"filename": "note.txt"}, content=b"")
    assert r.status_code == 400


def test_attaching_to_a_brand_new_session_lands(client, pad):
    """The reported bug, at the route: attach a photo to a session spawned
    moments ago and it goes to the Mac, instead of 404ing until the first
    turn writes a transcript."""
    r = client.post(f"/api/jremote/v1/sessions/{FRESH}/scratchpad/upload",
                    params={"filename": "photo.heic"}, content=b"jpegbytes")
    assert r.status_code == 200, r.text
    assert (pad / "photo.heic").read_bytes() == b"jpegbytes"
    # ...and the Files pane shows it back in the same window.
    listing = client.get(f"/api/jremote/v1/sessions/{FRESH}/scratchpad")
    assert listing.status_code == 200
    assert [f["name"] for f in listing.json()["files"]] == ["photo.heic"]


def test_scratchpad_requires_token(monkeypatch, pad):
    monkeypatch.setattr(auth, "_expected_token", lambda: "test-token")
    bare = TestClient(app)
    assert bare.get(
        f"/api/jremote/v1/sessions/{SID}/scratchpad").status_code == 401


# ── the routes the pane actually uses: addressed by agent ──

def test_agent_files_lists_the_pad(client, pad):
    _plant(pad, "a.png", mtime=2000)
    _plant(pad, "repo/x.swift")
    r = client.get(f"/api/jremote/v1/agents/{AGENT}/files")
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == ""
    assert [d["name"] for d in body["dirs"]] == ["repo"]
    assert [f["name"] for f in body["files"]] == ["a.png"]


def test_agent_files_opens_a_folder(client, pad):
    _plant(pad, "repo/README.md")
    r = client.get(f"/api/jremote/v1/agents/{AGENT}/files",
                   params={"path": "repo"})
    assert r.status_code == 200
    assert [f["name"] for f in r.json()["files"]] == ["README.md"]


def test_agent_files_works_before_any_session_exists(client, pad):
    """The reason the pane is agent-addressed: a brand-new chat still shows
    the seat's folder, and a file parked from the share sheet is readable with
    nothing spawned."""
    scratchpad.save_drop(AGENT, "parked.png", b"x")
    r = client.get(f"/api/jremote/v1/agents/{AGENT}/files")
    assert r.status_code == 200
    names = [f["name"] for f in r.json()["files"]]
    assert any(n.endswith("-parked.png") for n in names)
    assert r.json()["files"][0]["boss"] is True


def test_agent_files_unknown_agent_404(client, pad):
    assert client.get("/api/jremote/v1/agents/nobody-chat/files").status_code == 404


def test_agent_files_missing_folder_404(client, pad):
    r = client.get(f"/api/jremote/v1/agents/{AGENT}/files",
                   params={"path": "nope"})
    assert r.status_code == 404


def test_agent_file_content_bytes(client, pad):
    _plant(pad, "repo/notes.md", b"# hi")
    r = client.get(f"/api/jremote/v1/agents/{AGENT}/files/content",
                   params={"rel": "repo/notes.md"})
    assert r.status_code == 200
    assert r.content == b"# hi"


def test_agent_file_delete_and_clear(client, pad):
    _plant(pad, "repo/a.txt")
    _plant(pad, "repo/b.txt")
    r = client.post(f"/api/jremote/v1/agents/{AGENT}/files/delete",
                    json={"rel": "repo/a.txt"})
    assert r.status_code == 200
    assert not (pad / "repo" / "a.txt").exists()

    r = client.post(f"/api/jremote/v1/agents/{AGENT}/files/clear",
                    json={"rel": "repo"})
    assert r.json()["cleared"] == 1
    assert list((pad / "repo").iterdir()) == []


def test_agent_files_clear_reaches_the_pad_itself(client, pad):
    """The pad is a screen the user stands on, so its Clear works like any
    folder's — there is no level above it to protect."""
    _plant(pad, "a.txt")
    r = client.post(f"/api/jremote/v1/agents/{AGENT}/files/clear",
                    json={"rel": ""})
    assert r.status_code == 200
    assert r.json()["cleared"] == 1


def test_agent_files_upload_lands_where_pointed(client, pad):
    (pad / "repo").mkdir(parents=True)
    r = client.post(f"/api/jremote/v1/agents/{AGENT}/files/upload",
                    params={"rel": "repo", "filename": "drop.txt"},
                    content=b"bytes")
    assert r.status_code == 200
    assert (pad / "repo" / "drop.txt").read_bytes() == b"bytes"


def test_agent_files_save_replaces_what_was_opened(client, pad):
    _plant(pad, "repo/shot.png", b"before")
    r = client.post(f"/api/jremote/v1/agents/{AGENT}/files/save",
                    params={"rel": "repo/shot.png"}, content=b"marked up")
    assert r.status_code == 200
    assert (pad / "repo" / "shot.png").read_bytes() == b"marked up"
    assert list((pad / "repo").iterdir()) == [pad / "repo" / "shot.png"]


def test_agent_files_save_404s_rather_than_creating(client, pad):
    r = client.post(f"/api/jremote/v1/agents/{AGENT}/files/save",
                    params={"rel": "gone.png"}, content=b"x")
    assert r.status_code == 404
    assert not (pad / "gone.png").exists()


def test_agent_files_save_rejects_empty(client, pad):
    _plant(pad, "shot.png", b"before")
    r = client.post(f"/api/jremote/v1/agents/{AGENT}/files/save",
                    params={"rel": "shot.png"}, content=b"")
    assert r.status_code == 400
    assert (pad / "shot.png").read_bytes() == b"before"


def test_agent_files_requires_token(monkeypatch, pad):
    monkeypatch.setattr(auth, "_expected_token", lambda: "test-token")
    bare = TestClient(app)
    assert bare.get(
        f"/api/jremote/v1/agents/{AGENT}/files").status_code == 401
