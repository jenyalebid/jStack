"""jRemote session store — the single SQLite source for session meta.

Pins the contracts sync leans on: incremental indexing bills tokens once per
assistant message across read boundaries and never folds a half-written line;
vanished transcripts tombstone with a seq bump so devices drop them; user-meta
pushes upsert newest-wins and exact twin marks merge with filings repointed;
`changes_since` returns exactly the rows past the cursor.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from jstack_host.store import SessionStore

#: Claude Code encodes a project dir by replacing every `/` in the working
#: directory with `-`, so this key is only decodable against the instance root
#: it was built from. A literal one resolved on exactly one machine; the `home`
#: fixture builds the root and rewrites this to match.
KEY = "-Users-nova-Agents-Nova-chat"
SID = "11111111-2222-3333-4444-555555550000"


def _line(obj) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def _user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant(text, mid="msg_1", in_tok=100, out_tok=10):
    return {
        "type": "assistant",
        "message": {
            "id": mid, "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 2000},
        },
    }


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A home whose agent root this test owns, and a KEY that decodes against it."""
    import sys
    from jstack_host import hostenv
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "Agents"
    (root / "Nova" / "chat").mkdir(parents=True)
    (root / "Finch" / "chat").mkdir(parents=True)
    monkeypatch.setenv("JREMOTE_INSTANCE_ROOT", str(root))
    monkeypatch.setenv("JREMOTE_HOST_PROFILE", "default")
    hostenv.reset_profile()
    monkeypatch.setattr(sys.modules[__name__], "KEY",
                        str(root).replace("/", "-") + "-Nova-chat")
    proj = tmp_path / ".claude" / "projects" / KEY
    proj.mkdir(parents=True)
    return tmp_path


@pytest.fixture
def store(tmp_path):
    return SessionStore(db_path=tmp_path / "store.sqlite")


def _transcript(home: Path, sid: str = SID) -> Path:
    return home / ".claude" / "projects" / KEY / f"{sid}.jsonl"


def _write(f: Path, *objs, partial: bytes = b""):
    f.write_bytes(b"".join(_line(o) for o in objs) + partial)


def _row(store, sid=SID):
    with store._conn() as db:
        r = db.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
    return dict(r) if r else None


# ── indexing ──

def test_backfill_extracts_summary(home, store):
    f = _transcript(home)
    _write(
        f,
        {"type": "ai-title", "aiTitle": "menu work", "sessionId": SID},
        _user("<system>injected</system>fix the menu"),
        _user("fix the menu please"),
        # Two lines of one message (same id) — billed once.
        _assistant("thinking...", mid="msg_a"),
        _assistant("done, menu fixed", mid="msg_a"),
        _assistant("anything else?", mid="msg_b", in_tok=5000, out_tok=20),
    )
    assert store.refresh() == 1
    row = _row(store)
    assert row["agent_id"] == "nova"
    assert row["sub_mode"] == "chat"
    assert row["ai_title"] == "menu work"
    assert row["first_real_user_msg"] == "fix the menu"
    assert row["first_msg"] == "fix the menu please"
    assert row["last_msg"] == "anything else?"
    assert row["calls"] == 2                       # msg_a once + msg_b
    assert row["in_tokens"] == 2100 + 7000         # 100+2000 cache, 5000+2000
    assert row["out_tokens"] == 30
    assert row["last_context"] == 7000
    assert row["seq"] > 0


def test_incremental_append_no_double_billing(home, store):
    f = _transcript(home)
    _write(f, _user("hello"), _assistant("hi", mid="msg_a"))
    store.refresh()
    first = _row(store)

    with f.open("ab") as fh:
        fh.write(_line(_assistant("more", mid="msg_a")))   # same message, late block
        fh.write(_line(_assistant("bye", mid="msg_b")))
    assert store.refresh() == 1
    row = _row(store)
    assert row["calls"] == first["calls"] + 1              # msg_a not re-billed
    assert row["last_msg"] == "bye"
    assert row["byte_offset"] == f.stat().st_size
    assert row["seq"] > first["seq"]


def test_partial_line_left_for_next_pass(home, store):
    f = _transcript(home)
    whole = _line(_user("hello"))
    partial = _line(_assistant("done", mid="msg_x"))[:-10]  # no newline
    f.write_bytes(whole + partial)
    store.refresh()
    row = _row(store)
    assert row["byte_offset"] == len(whole)                 # stopped before it
    assert row["calls"] == 0

    with f.open("ab") as fh:                                # line completes
        fh.write(_line(_assistant("done", mid="msg_x"))[-10:])
    store.refresh()
    row = _row(store)
    assert row["calls"] == 1
    assert row["last_msg"] == "done"


def test_truncated_file_reparses_clean(home, store):
    f = _transcript(home)
    _write(f, _user("hello"), _assistant("hi", mid="m1"),
           _assistant("more", mid="m2"))
    store.refresh()
    _write(f, _user("hello"), _assistant("hi", mid="m1"))   # rewritten smaller
    store.refresh()
    row = _row(store)
    assert row["calls"] == 1                                # not 2 + 1
    assert row["last_msg"] == "hi"


def test_deleted_transcript_tombstones(home, store):
    f = _transcript(home)
    _write(f, _user("hello"))
    store.refresh()
    seq_before = store.current_seq()
    f.unlink()
    assert store.refresh() == 1
    row = _row(store)
    assert row["deleted"] == 1
    assert row["seq"] > seq_before


def test_unchanged_tick_writes_nothing(home, store):
    f = _transcript(home)
    _write(f, _user("hello"))
    store.refresh()
    seq = store.current_seq()
    assert store.refresh() == 0
    assert store.current_seq() == seq


# ── last prompt (whose finger typed it) ──

def _prompt(text, sid=SID):
    return {"type": "last-prompt", "lastPrompt": text, "sessionId": sid}


def test_compact_never_becomes_the_last_prompt(home, store):
    """`compact_on_delivery` types `/compact` at the end of a heavy turn, so
    the newest `last-prompt` on the busiest sessions is the machine's. The
    card's exchange line must stay on what the user actually said."""
    f = _transcript(home)
    _write(f, _prompt("ship the pict tool"), _prompt("/compact"))
    store.refresh()
    assert _row(store)["last_prompt"] == "ship the pict tool"

    # And across a read boundary: the incremental fold sees only the machine's
    # line and must leave the field it already holds alone.
    with f.open("ab") as fh:
        fh.write(_line(_prompt("/compact keep the API details")))
    store.refresh()
    assert _row(store)["last_prompt"] == "ship the pict tool"


def test_a_slash_command_the_user_typed_is_still_their_prompt(home, store):
    """The skip list is closed at `/compact` — `/push` is the user talking."""
    _write(_transcript(home), _prompt("ship the pict tool"), _prompt("/push"))
    store.refresh()
    assert _row(store)["last_prompt"] == "/push"


def test_rows_holding_a_machine_prompt_are_repaired_on_open(home, tmp_path):
    """Rows folded before the store knew to skip it: the index is incremental
    and a finished transcript is never re-read, so they are repaired in place
    when the store opens."""
    db_path = tmp_path / "store.sqlite"
    f = _transcript(home)
    _write(f, _prompt("ship the pict tool"), _prompt("/compact"))
    stale = SessionStore(db_path=db_path)
    stale.refresh()
    with stale._conn() as db:
        db.execute("UPDATE sessions SET last_prompt='/compact'")   # pre-fix row

    reopened = SessionStore(db_path=db_path)
    assert _row(reopened)["last_prompt"] == "ship the pict tool"
    # Idempotent: nothing left to match, nothing rewritten.
    assert _row(SessionStore(db_path=db_path))["last_prompt"] == "ship the pict tool"


def test_repair_clears_a_prompt_no_file_can_vouch_for(home, tmp_path):
    """An empty exchange line beats a false one — a transcript that is gone,
    or that never carried a human prompt, leaves the field blank."""
    db_path = tmp_path / "store.sqlite"
    f = _transcript(home)
    _write(f, _prompt("/compact"))
    store = SessionStore(db_path=db_path)
    store.refresh()
    with store._conn() as db:
        db.execute("UPDATE sessions SET last_prompt='/compact'")
    assert _row(SessionStore(db_path=db_path))["last_prompt"] == ""


# ── session color (Claude Code's `/color`) ──

def _color(value, sid=SID):
    return {"type": "agent-color", "agentColor": value, "sessionId": sid}


def test_color_folds_last_wins(home, store):
    """Claude Code re-appends the line as the session runs; the newest one is
    the session's color."""
    f = _transcript(home)
    _write(f, _user("hi"), _color("pink"), _assistant("hey"))
    store.refresh()
    assert _row(store)["agent_color"] == "pink"
    assert store.session_colors() == {SID: "pink"}

    _write(f, _user("hi"), _color("pink"), _assistant("hey"),
           _user("again"), _color("cyan"))
    store.refresh()
    assert _row(store)["agent_color"] == "cyan"


def test_color_reset_reads_as_no_color(home, store):
    """`/color default` writes the literal "default" — a session reset that
    way has to be indistinguishable from one that never set a color, or the
    tint outlives the choice that made it."""
    f = _transcript(home)
    _write(f, _user("hi"), _color("orange"))
    store.refresh()
    assert store.session_colors() == {SID: "orange"}

    _write(f, _user("hi"), _color("orange"), _color("default"))
    store.refresh()
    assert _row(store)["agent_color"] == ""
    assert store.session_colors() == {}


def test_unknown_color_name_is_dropped(home, store):
    """The transcript is Claude Code's file, not our schema. A name no client
    can render must not reach one — it would be rendered as a guess."""
    _write(_transcript(home), _user("hi"), _color("chartreuse"))
    store.refresh()
    assert _row(store)["agent_color"] == ""


def test_color_rides_the_served_row(home, store):
    _write(_transcript(home), _user("hi"), _color("blue"))
    store.refresh()
    assert store.query_sessions()[0]["agent_color"] == "blue"


def test_board_takes_the_color_from_the_index(home, store, monkeypatch):
    """The board asks the index rather than parsing for itself.

    Both payloads describe the same session and land on the same record on the
    device, so a second reader here is not redundancy — it is a field that
    flickers. And they would disagree: the board's summary parser samples a
    large transcript, this index folds every line."""
    from jstack_host import board
    from jstack_host import store as store_module
    monkeypatch.setattr(store_module, "_store", store)
    _write(_transcript(home), _user("hi"), _color("green"))
    store.refresh()
    assert board._session_colors() == {SID: "green"}


def test_board_keeps_its_board_when_the_index_cannot_answer(monkeypatch):
    """A store that can't answer costs the tint, never the board."""
    from jstack_host import board
    from jstack_host import store as store_module

    def boom():
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(store_module, "get_store", boom)
    assert board._session_colors() == {}


def test_existing_store_gains_new_columns(tmp_path):
    """A column added to `_SCHEMA` reaches a store that already exists.

    `CREATE TABLE IF NOT EXISTS` is a no-op on one, so without this the first
    INSERT naming the column fails and the whole index goes dark — not one
    field arriving blank."""
    path = tmp_path / "old.sqlite"
    SessionStore(db_path=path)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE sessions DROP COLUMN agent_color")
        db.execute("INSERT INTO sessions(session_id) VALUES('legacy')")
    SessionStore(db_path=path)
    with sqlite3.connect(path) as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(sessions)")}
        rows = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert "agent_color" in cols
    assert rows == 1          # additive only — nothing is rebuilt or dropped


# ── on-demand queries ──

def test_query_filters_and_pages(home, store):
    other = (home / ".claude" / "projects"
             / (str(home / "Agents").replace("/", "-") + "-Finch-chat"))
    other.mkdir(parents=True)
    _write(_transcript(home), _user("nova session about menus"))
    _write(other / "99999999-8888-7777-6666-555555550000.jsonl",
           _user("finch session about widgets"))
    store.refresh()

    assert len(store.query_sessions()) == 2
    mine = store.query_sessions(agent="nova-chat")
    assert [s["session_id"] for s in mine] == [SID]
    assert mine[0]["preview"] == "nova session about menus"
    assert store.query_sessions(q="widgets")[0]["agent_id"] == "finch"
    assert store.query_sessions(q="nothing-matches") == []

    newest = store.query_sessions(limit=1)
    assert len(newest) == 1
    older = store.query_sessions(before=newest[0]["last_activity"])
    assert len(older) >= 1
    assert all(s["session_id"] != newest[0]["session_id"] for s in older)


# ── user-meta sync ──

def test_push_pull_roundtrip(store):
    seq0 = store.current_seq()
    store.apply_push({
        "marks": [{"id": "A", "name": "Later", "color_hex": "#0A84FF",
                   "sort_index": 0, "updated_at": 100.0}],
        "session_meta": [{"session_id": SID, "mark_id": "A",
                          "updated_at": 100.0}],
        "settings": {"speakReplies": True},
    })
    delta = store.changes_since(seq0)
    assert [m["id"] for m in delta["marks"]] == ["A"]
    assert delta["session_meta"][0]["mark_id"] == "A"
    assert json.loads(delta["settings"][0]["value"]) is True
    assert delta["seq"] == store.current_seq()
    # A caught-up cursor gets nothing.
    empty = store.changes_since(delta["seq"])
    assert empty["marks"] == [] and empty["session_meta"] == []


def test_stale_push_loses(store):
    store.apply_push({"session_meta": [
        {"session_id": SID, "mark_id": "NEW", "updated_at": 200.0}]})
    store.apply_push({"session_meta": [
        {"session_id": SID, "mark_id": "OLD", "updated_at": 100.0}]})
    delta = store.changes_since(0)
    assert delta["session_meta"][0]["mark_id"] == "NEW"


def test_twin_marks_merge_and_repoint(store):
    store.apply_push({
        "marks": [
            {"id": "AAA", "name": "Later", "color_hex": "#0A84FF",
             "updated_at": 100.0},
            {"id": "BBB", "name": "Later", "color_hex": "#0A84FF",
             "updated_at": 101.0},
        ],
        "session_meta": [{"session_id": SID, "mark_id": "BBB",
                          "updated_at": 101.0}],
    })
    delta = store.changes_since(0)
    alive = [m for m in delta["marks"] if not m["deleted"]]
    assert [m["id"] for m in alive] == ["AAA"]
    dead = [m for m in delta["marks"] if m["deleted"]]
    assert [m["id"] for m in dead] == ["BBB"]
    assert delta["session_meta"][0]["mark_id"] == "AAA"


def test_merged_rows_outstamp_the_device_that_pushed_them(store):
    """The device applies a delta row only when its stamp beats the one it
    holds — so a merge that keeps the loser's stamp is a merge no device ever
    sees, and the duplicate stays on its screen forever."""
    held = {"BBB": 101.0, SID: 101.0}
    store.apply_push({
        "marks": [
            {"id": "AAA", "name": "Later", "color_hex": "#0A84FF",
             "updated_at": 100.0},
            {"id": "BBB", "name": "Later", "color_hex": "#0A84FF",
             "updated_at": held["BBB"]},
        ],
        "session_meta": [{"session_id": SID, "mark_id": "BBB",
                          "updated_at": held[SID]}],
    })
    delta = store.changes_since(0)
    tombstone = next(m for m in delta["marks"] if m["id"] == "BBB")
    assert tombstone["updated_at"] > held["BBB"]
    filing = next(s for s in delta["session_meta"] if s["session_id"] == SID)
    assert filing["updated_at"] > held[SID]


def test_filing_on_a_dead_mark_heals(store):
    """An offline device files under a twin the host already tombstoned. The
    filing must follow the surviving mark, not dangle on a dead id."""
    store.apply_push({"marks": [
        {"id": "AAA", "name": "Later", "color_hex": "#0A84FF", "updated_at": 100.0},
        {"id": "BBB", "name": "Later", "color_hex": "#0A84FF", "updated_at": 101.0},
    ]})
    seq = store.current_seq()
    store.apply_push({"session_meta": [
        {"session_id": SID, "mark_id": "BBB", "updated_at": 200.0}]})
    delta = store.changes_since(seq)
    filing = next(s for s in delta["session_meta"] if s["session_id"] == SID)
    assert filing["mark_id"] == "AAA"
    assert filing["updated_at"] > 200.0


def test_merge_settles_and_stops_restamping(store):
    """Converged rows must not be rewritten on every push — a device that
    pulled them would re-pull the same rows forever."""
    push = {"marks": [
        {"id": "AAA", "name": "Later", "color_hex": "#0A84FF", "updated_at": 100.0},
        {"id": "BBB", "name": "Later", "color_hex": "#0A84FF", "updated_at": 101.0},
    ], "session_meta": [{"session_id": SID, "mark_id": "BBB", "updated_at": 101.0}]}
    store.apply_push(push)
    settled = {m["id"]: m["updated_at"] for m in store.changes_since(0)["marks"]}
    seq = store.current_seq()
    store.apply_push(push)          # the same device, re-pushing its truth
    after = store.changes_since(0)
    assert {m["id"]: m["updated_at"] for m in after["marks"]} == settled
    assert store.changes_since(seq)["marks"] == []


def test_unfile_survives_as_null_mark(store):
    store.apply_push({"session_meta": [
        {"session_id": SID, "mark_id": "A", "updated_at": 100.0}]})
    seq = store.current_seq()
    store.apply_push({"session_meta": [
        {"session_id": SID, "mark_id": None, "updated_at": 200.0}]})
    delta = store.changes_since(seq)
    assert delta["session_meta"][0]["mark_id"] is None


# ── endpoints (the real app, token-gated) ──

@pytest.fixture
def client(monkeypatch, store):
    from fastapi.testclient import TestClient
    from jstack_host.server import create_app
    app = create_app()
    from jstack_host import auth
    from jstack_host import store as store_module
    monkeypatch.setattr(auth, "_expected_token", lambda: "test-token")
    monkeypatch.setattr(store_module, "_store", store)
    c = TestClient(app)
    c.headers.update({"Authorization": "Bearer test-token"})
    return c


def test_endpoints_require_token(client, store):
    import httpx
    bare = httpx.Client(transport=client._transport, base_url=client.base_url)
    assert bare.get("/api/jremote/v1/sync").status_code in (401, 403)


def test_sync_endpoint_roundtrip(client, store):
    r = client.post("/api/jremote/v1/sync", json={
        "marks": [{"id": "A", "name": "Later", "color_hex": "#0A84FF",
                   "updated_at": 100.0}],
        "session_meta": [{"session_id": SID, "mark_id": "A",
                          "updated_at": 100.0}],
        "settings": {"speakReplies": False},
    })
    assert r.status_code == 200
    seq = r.json()["seq"]
    delta = client.get("/api/jremote/v1/sync", params={"since": 0}).json()
    assert delta["seq"] == seq
    assert delta["marks"][0]["id"] == "A"
    assert delta["session_meta"][0]["mark_id"] == "A"
    assert client.get("/api/jremote/v1/sync",
                      params={"since": seq}).json()["marks"] == []


def test_history_endpoint_queries_store(client, store, home):
    _write(_transcript(home), _user("searchable menu work"))
    store.refresh()
    rows = client.get("/api/jremote/v1/sessions/history",
                      params={"agent": "nova-chat", "q": "menu"}).json()["sessions"]
    assert [s["session_id"] for s in rows] == [SID]
    assert rows[0]["preview"] == "searchable menu work"
    none = client.get("/api/jremote/v1/sessions/history",
                      params={"q": "zzz-no-match"}).json()["sessions"]
    assert none == []


def test_a_store_call_closes_the_connection_it_opened(store, monkeypatch):
    """Every store call opens its own connection, so one that outlives the
    call is a descriptor leak paid per call — two of them, the file and its
    WAL. `with connection:` manages only the transaction, and on CPython 3.14
    a `sqlite3.Connection` sits in a reference cycle from birth, so refcounting
    never frees it: it waits for the cyclic collector, on the collector's
    schedule. The host learned this at launchd's 256-descriptor ceiling —
    accept() failing with EMFILE after half an hour of a device polling the
    board. Collector off, so the store has to have closed it itself."""
    import sqlite3
    original = sqlite3.connect
    opened = []

    def connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        opened.append(connection)  # retain it, so GC cannot hide missing close()
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    for _ in range(50):
        store.current_seq()
    assert len(opened) == 50
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
