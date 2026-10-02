"""Shortcuts, seats, and the palette that has to answer for the seat.

Three contracts, one screen behind them — the app's Agents tab, where the user
picks a directory and keeps it as a card:

* **`shortcuts` is a synced user-meta table like `marks`.** Newest-`updated_at`
  wins, deletes are tombstones, and the **row id is the identity** — two cards
  onto one seat are two launchers with two configs, not one card added twice.
  `config` is the app's launch settings, carried and never read here. Deleting
  a card must never touch the directory — that is the whole reason the table
  exists rather than a folder move.
* **A seat is a directory holding a AGENTS.md.** The same rule `bin/msg`, the
  session-start injector and the review engine already run on. Pinned here
  because the older answer (`submode_dirs`, direct children against a name
  blocklist) is blind to `code/go` and wrongly prunes `missions/`.
* **The command palette is the seat's chain, not the agent's root.** It listed
  the agent root's commands and none of the seat's own, so the phone offered
  commands the session would refuse and hid the ones it would run.
"""

import json
from pathlib import Path

import pytest

from jstack_host import commands, seats
from jstack_host import plugin_paths
from jstack_host.store import SessionStore


@pytest.fixture
def store(tmp_path):
    return SessionStore(db_path=tmp_path / "store.sqlite")


def _sc(sid, seat, label="", ts=100.0, deleted=False, tag=""):
    return {"id": sid, "seat_id": seat, "tag": tag, "label": label, "emoji": "",
            "sort_index": 0, "updated_at": ts, "deleted": deleted}


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A two-agent host under the portable profile — the real seam, not a stub.

    Iris:  root seat, chat, code (seat) → code/go (seat), a non-seat `notes`
            dir with a seat under it, plus `pad/` and `git/` holding AGENTS.md
            files that must NOT read as seats.
    Atlas:  root seat only.
    """
    from jstack_host import hostenv
    root = tmp_path / "Agents"
    for rel in ("Iris", "Iris/chat", "Iris/code", "Iris/code/go",
                "Iris/notes/deep", "Iris/pad/issue-6", "Iris/git/assets",
                "Atlas"):
        d = root / rel
        d.mkdir(parents=True)
        (d / "AGENTS.md").write_text(f"# {rel}\n")
    (root / "Iris" / "notes").mkdir(exist_ok=True)      # non-seat, has a seat under it
    monkeypatch.setenv("JREMOTE_HOST_PROFILE", "default")
    monkeypatch.setenv("JREMOTE_INSTANCE_ROOT", str(root))
    hostenv.reset_profile()
    seats.invalidate()
    yield root
    hostenv.reset_profile()
    seats.invalidate()


# ── the synced table ──

_UP = "AF116D56-8A91-4081-824F-A98D01FF6257"
_LOW = _UP.lower()


def test_a_uuid_is_one_card_however_it_is_spelled(store):
    """The end-to-end break, in three lines. Foundation writes the uppercase
    form, a script writes the lowercase one, and a TEXT primary key made them
    two live cards on one seat — which the twin merge resolved by tombstoning
    one. The app parses both spellings back to the same `UUID`, so it landed
    that tombstone on the card it was showing and the card disappeared."""
    store.apply_push({"shortcuts": [_sc(_LOW, "iris-code-go", "Wordy", ts=100)]})
    store.apply_push({"shortcuts": [_sc(_UP, "iris-code-go", "Wordy", ts=200)]})

    assert [(r["id"], r["label"]) for r in store.shortcuts()] \
        == [(_LOW, "Wordy")], "one live card, at the canonical id"


def test_a_non_uuid_id_is_left_exactly_as_it_came(store):
    """The column is TEXT and nothing here decides what a non-UUID may mean —
    the host's own probe rows use `probe-1`."""
    store.apply_push({"shortcuts": [_sc("Probe-1", "iris-chat", "P", ts=100)]})
    assert store.shortcuts()[0]["id"] == "Probe-1"


def test_a_pre_canonical_row_folds_in_on_open(tmp_path):
    """Ingest normalises from here on, but a row written before it would sit
    beside the normalised form of itself. Newest stamp wins, and the loser goes
    rather than lingering as a second card on the seat."""
    path = tmp_path / "old.sqlite"
    db = SessionStore(db_path=path)
    with db._conn() as raw:                     # write past the normalising push
        for sid, ts in ((_UP, 200.0), (_LOW, 100.0)):
            raw.execute("INSERT INTO shortcuts (id, seat_id, label, updated_at) "
                        "VALUES (?,?,?,?)", (sid, "iris-code-go", sid[:2], ts))

    reopened = SessionStore(db_path=path)
    rows = reopened.shortcuts()
    assert [(r["id"], r["label"]) for r in rows] == [(_LOW, "AF")]


def test_shortcuts_round_trip_and_newest_wins(store):
    store.apply_push({"shortcuts": [_sc("a", "iris-code-go", "Go", ts=100)]})
    got = store.changes_since(0)["shortcuts"]
    assert [(r["id"], r["seat_id"], r["label"]) for r in got] \
        == [("a", "iris-code-go", "Go")]

    store.apply_push({"shortcuts": [_sc("a", "iris-code-go", "STALE", ts=50)]})
    assert store.shortcuts()[0]["label"] == "Go"

    store.apply_push({"shortcuts": [_sc("a", "iris-code-go", "Golang", ts=200)]})
    assert store.shortcuts()[0]["label"] == "Golang"


def test_delete_is_a_tombstone_that_still_ships(store, tree):
    """Un-pinning takes the card and leaves the directory — the case the table
    exists for. The row stays so every other device hears about it."""
    store.apply_push({"shortcuts": [_sc("a", "iris-code-go", ts=100)]})
    cursor = store.current_seq()
    store.apply_push({"shortcuts": [_sc("a", "iris-code-go", ts=300, deleted=True)]})

    assert store.shortcuts() == []                       # gone from the board
    shipped = store.changes_since(cursor)["shortcuts"]   # but not from the wire
    assert [(r["id"], r["deleted"]) for r in shipped] == [("a", 1)]
    assert (tree / "Iris" / "code" / "go" / "AGENTS.md").is_file()


def test_changes_since_returns_only_rows_past_the_cursor(store):
    store.apply_push({"shortcuts": [_sc("a", "iris-chat", ts=100)]})
    cursor = store.current_seq()
    store.apply_push({"shortcuts": [_sc("b", "iris-pm", ts=200)]})
    assert [r["id"] for r in store.changes_since(cursor)["shortcuts"]] == ["b"]


def test_a_row_without_a_seat_is_refused(store):
    """A card with no seat names nothing spawnable — it would be a dead row on
    every device forever."""
    store.apply_push({"shortcuts": [{"id": "a", "label": "orphan"}]})
    assert store.shortcuts() == []


def test_two_shortcuts_onto_one_seat_both_stand(store):
    """The id is the identity, and a shortcut carries a launch config — so two
    onto one seat are two launchers, not one card added twice.

    This replaces a twin merge that tombstoned the second row on a (seat, tag)
    pair. It was right while a shortcut said only *this way in*; it would now
    silently delete "Iris on Codex" minutes after the user made it, on every
    device they own."""
    store.apply_push({"shortcuts": [
        _sc("aaa", "iris-code-go", "Go", ts=100),
        _sc("bbb", "iris-code-go", "Go · Codex", ts=100),
    ]})
    assert sorted(r["id"] for r in store.shortcuts()) == ["aaa", "bbb"]
    assert all(r["deleted"] == 0 for r in store.changes_since(0)["shortcuts"])


def test_different_seats_both_stand_too(store):
    store.apply_push({"shortcuts": [
        _sc("aaa", "iris-code-go", ts=100),
        _sc("bbb", "iris-code-web", ts=100),
    ]})
    assert len(store.shortcuts()) == 2


# ── subjects: the same table, narrowed to a tag ──

def test_a_subject_rides_the_shortcut_table(store):
    """A subject shortcut is a seat plus what it is about. It travels for the
    same reason a card does — the vocabulary is the Mac's and the user's filing
    means the same thing on every device they own."""
    store.apply_push({"shortcuts": [_sc("aaa", "iris-code-wordy", tag="md")]})
    assert [(r["seat_id"], r["tag"]) for r in store.shortcuts()] \
        == [("iris-code-wordy", "md")]
    assert store.changes_since(0)["shortcuts"][0]["tag"] == "md"


def test_a_row_that_names_no_subject_is_a_plain_card(store):
    """A host row written before subjects existed, and a device that never
    sends the field, both mean the same thing: a seat card."""
    store.apply_push({"shortcuts": [{"id": "aaa", "seat_id": "iris-code-go",
                                     "updated_at": 100.0}]})
    assert store.shortcuts()[0]["tag"] == ""


def test_a_subject_is_not_a_twin_of_its_own_seats_card(store):
    """The merge keys on the pair. A seat card and a subject on that seat are
    two ways in, not one card added twice — folding them together would delete
    something the user made."""
    store.apply_push({"shortcuts": [
        _sc("aaa", "iris-code-wordy", "Wordy", ts=100),
        _sc("bbb", "iris-code-wordy", ts=100, tag="md"),
        _sc("ccc", "iris-code-wordy", ts=100, tag="jremote"),
    ]})
    assert sorted(r["id"] for r in store.shortcuts()) == ["aaa", "bbb", "ccc"]


def test_the_same_subject_twice_on_one_seat_is_two_launchers(store):
    """Also no longer merged, and for the reason the config exists: the same
    subject on the same seat, one opening Claude and one opening Codex, is two
    deliberate cards."""
    store.apply_push({"shortcuts": [
        _sc("aaa", "iris-code-wordy", ts=100, tag="md"),
        _sc("bbb", "iris-code-wordy", ts=100, tag="md"),
    ]})
    assert sorted(r["id"] for r in store.shortcuts()) == ["aaa", "bbb"]


# ── config: carried, never read ──

def test_config_round_trips_untouched(store):
    """The host stores the app's launch settings and has no opinion about
    them — whatever went in comes back byte for byte."""
    blob = '{"engine":"codex","model":"gpt-5.6-sol","tags":["md","jremote"]}'
    store.apply_push({"shortcuts": [
        dict(_sc("aaa", "iris-code-go", ts=100), config=blob)]})
    assert store.shortcuts()[0]["config"] == blob
    assert store.changes_since(0)["shortcuts"][0]["config"] == blob


def test_a_push_that_omits_config_keeps_the_stored_one(store):
    """Every device pushes its whole shortcut table every sync. A build that
    predates the column — or predates one key in it — sends rows without it,
    and taking that as "clear it" would wipe the user's engine choice off every
    card the moment an old phone synced."""
    blob = '{"engine":"codex"}'
    store.apply_push({"shortcuts": [
        dict(_sc("aaa", "iris-code-go", "Go", ts=100), config=blob)]})
    store.apply_push({"shortcuts": [_sc("aaa", "iris-code-go", "Golang", ts=200)]})
    row = store.shortcuts()[0]
    assert row["label"] == "Golang"          # the edit landed
    assert row["config"] == blob             # the config it could not see did not


def test_an_explicit_empty_config_does_clear(store):
    """Present-and-empty is a real value: it is what a card with no settings
    at all looks like, and clearing has to be reachable."""
    store.apply_push({"shortcuts": [
        dict(_sc("aaa", "iris-code-go", ts=100), config='{"engine":"codex"}')]})
    store.apply_push({"shortcuts": [
        dict(_sc("aaa", "iris-code-go", ts=200), config="")]})
    assert store.shortcuts()[0]["config"] == ""


# ── which shortcut opened a session ──

def test_a_launch_is_remembered_past_the_close(store):
    """The spawn is the only witness, and a shortcut's history is mostly
    sittings that are over — so this lives in the store, not in the open
    registry that is popped when a session ends."""
    store.record_launch("sid-1", "aaa")
    store.record_launch("sid-2", "aaa")
    store.record_launch("sid-3", "bbb")
    assert store.launched_by("aaa") == {"sid-1", "sid-2"}
    assert store.launched_by("bbb") == {"sid-3"}
    assert store.launched_by("nobody") == set()
    assert store.launch_shortcuts()["sid-3"] == "bbb"


def test_a_launch_is_stamped_under_the_canonical_id(store):
    """Same rule as the shortcut row itself: a UUID is one value however it is
    spelled, or a card made on the phone would not recognise its own
    sittings."""
    store.record_launch("sid-1", "AF116D56-0000-4000-8000-000000000001")
    assert store.launched_by("af116d56-0000-4000-8000-000000000001") == {"sid-1"}


def test_a_launch_needs_both_halves(store):
    store.record_launch("", "aaa")
    store.record_launch("sid-1", "")
    assert store.launch_shortcuts() == {}


def test_shortcuts_do_not_disturb_the_other_meta_tables(store):
    """It rides the one cursor with marks and filings — and must not merge,
    repoint or renumber any of them."""
    store.apply_push({"marks": [{"id": "m1", "name": "Ship", "color_hex": "#f00",
                                 "updated_at": 100.0}]})
    store.apply_push({"shortcuts": [_sc("a", "iris-chat", ts=200)]})
    delta = store.changes_since(0)
    assert [m["id"] for m in delta["marks"]] == ["m1"]
    assert delta["marks"][0]["updated_at"] == 100.0
    assert [s["id"] for s in delta["shortcuts"]] == ["a"]


# ── the seat rule ──

def test_walk_finds_nested_seats_and_the_root(tree):
    assert seats.walk("iris") == ["", "chat", "code", "code/go", "notes/deep"]
    assert seats.walk("atlas") == [""]


def test_pad_and_git_are_never_seats(tree):
    """Both hold a AGENTS.md here. The Law reserves them: `git/` is what the
    seat saves and `pad/` is the shared shelf, and work under a pad carries its
    own instructions — which is exactly why `Nova/chat/pad/issue-6` on the
    real machine has one."""
    found = seats.walk("iris")
    assert not [s for s in found if s.startswith(("pad", "git"))]
    assert seats.is_seat(tree / "Iris" / "pad" / "issue-6")   # the file IS there


def test_a_non_seat_directory_can_still_hold_one(tree):
    """`missions/` is the real case: not a seat, two seats under it. The old
    name blocklist pruned the subtree and lost both."""
    assert "notes/deep" in seats.walk("iris")
    assert not seats.is_seat(tree / "Iris" / "notes")


def test_walk_is_cached_until_invalidated(tree):
    assert "later" not in seats.walk("iris")
    new = tree / "Iris" / "later"
    new.mkdir()
    (new / "AGENTS.md").write_text("# later\n")
    assert "later" not in seats.walk("iris")     # TTL still holding
    seats.invalidate("iris")
    assert "later" in seats.walk("iris")


def test_seat_id_round_trips_through_the_spawn_resolver(tree):
    """The id a card stores is the id `/sessions/open-new` already takes."""
    from jstack_host.hostenv import workspace
    assert seats.seat_id("iris", "code/go") == "iris-code-go"
    assert workspace("iris-code-go") == tree / "Iris" / "code" / "go"


def test_the_slug_is_the_readable_spelling_of_the_same_pair(tree):
    """What a card says about itself where no agent header sits above it. The
    root seat is the bare agent name — `iris/` would name a directory nobody
    stands in."""
    assert seats.slug("iris", "code/go") == "iris/code/go"
    assert seats.slug("iris", "") == "iris"


def test_project_dir_name_flattens_both_separators():
    """Why nothing decodes: `/` and `.` land on the same character, so
    `social/threads-iris.words` and `social/threads/iris/words` come back
    indistinguishable. Encoding a known seat is exact; reading one out is not."""
    assert seats.project_dir_name("/a/b.c") == "-a-b-c"
    assert (seats.project_dir_name("/x/social/threads-iris.words")
            == seats.project_dir_name("/x/social/threads-iris/words"))


# ── what the poll is allowed to carry ──
#
# The first cut put every seat on the host into `/agents` — 51 rows on a
# fifteen-second poll, describing a tree that changes when the user makes a
# directory. The cache on the walk hid none of it: the walk was never the
# expensive part. These pin the payload to the changing part only.

def test_resolve_walks_only_the_agents_that_have_a_card(tree, monkeypatch):
    """A host with fifty seats and five cards pays for the five."""
    walked = []
    real = seats.walk
    monkeypatch.setattr(seats, "walk", lambda b: (walked.append(b), real(b))[1])
    found = seats.resolve(["iris-code-go"], ["iris", "atlas"])
    assert walked == ["iris"]                    # atlas never touched
    assert found == {"iris-code-go": ("iris", "code/go")}


def test_resolve_answers_by_encoding_not_by_decoding(tree):
    """Every seat id in the tree maps back to its real path — including the
    root, whose `rel` is empty. `split_id()` guesses this; the walk knows."""
    ids = [seats.seat_id("iris", r) for r in seats.walk("iris")]
    found = seats.resolve(ids, ["iris", "atlas"])
    assert found["iris"] == ("iris", "")
    assert found["iris-notes-deep"] == ("iris", "notes/deep")
    assert len(found) == len(ids)


def test_resolve_omits_a_card_whose_directory_is_gone(tree):
    assert seats.resolve(["iris-code-gone"], ["iris"]) == {}


def test_the_poll_carries_carded_seats_only(tree, monkeypatch, tmp_path):
    """`/agents` ships the cards, not the catalogue."""
    from jstack_host import board
    from jstack_host.store import SessionStore

    db = SessionStore(db_path=tmp_path / "poll.sqlite")
    db.apply_push({"shortcuts": [_sc("a", "iris-code-go", "Go")]})
    monkeypatch.setattr(board, "_seat_stats", lambda: (None, {}))
    monkeypatch.setattr("jstack_host.store.get_store", lambda: db)

    rows = board.carded_seats()
    assert [r["seat_id"] for r in rows] == ["iris-code-go"]
    assert rows[0]["path"] == "code/go" and not rows[0]["missing"]
    assert rows[0]["slug"] == "iris/code/go"
    # The other seven seats in this tree are real and none of them ship.
    assert len(seats.walk("iris")) > 1


def test_a_seat_two_shortcuts_name_is_polled_once(tree, monkeypatch, tmp_path):
    """A subject shortcut names a seat the poll still owes stats for — and the
    seat card beside it names the same one. One row, not two: the card and the
    subject draw from the same pulse."""
    from jstack_host import board
    from jstack_host.store import SessionStore

    db = SessionStore(db_path=tmp_path / "subject.sqlite")
    db.apply_push({"shortcuts": [_sc("a", "iris-code-go", "Go"),
                                 _sc("b", "iris-code-go", tag="md")]})
    monkeypatch.setattr(board, "_seat_stats", lambda: (None, {}))
    monkeypatch.setattr("jstack_host.store.get_store", lambda: db)

    rows = board.carded_seats()
    assert [r["seat_id"] for r in rows] == ["iris-code-go"]
    assert rows[0]["slug"] == "iris/code/go"


def test_no_cards_means_no_seat_payload_at_all(tree, monkeypatch, tmp_path):
    from jstack_host import board
    from jstack_host.store import SessionStore

    db = SessionStore(db_path=tmp_path / "empty.sqlite")
    monkeypatch.setattr("jstack_host.store.get_store", lambda: db)
    assert board.carded_seats() == []


def test_a_card_outliving_its_directory_renders_as_missing(tree, monkeypatch, tmp_path):
    """The delete semantic runs the other way — a card can be deleted without
    touching its folder — so the inverse must not draw a live-looking card."""
    from jstack_host import board
    from jstack_host.store import SessionStore

    db = SessionStore(db_path=tmp_path / "gone.sqlite")
    db.apply_push({"shortcuts": [_sc("a", "iris-deleted-thing", "Gone")]})
    monkeypatch.setattr(board, "_seat_stats", lambda: (None, {}))
    monkeypatch.setattr("jstack_host.store.get_store", lambda: db)

    row = board.carded_seats()[0]
    assert row["missing"] and row["session_count"] == 0 and not row["live"]
    # No path, so no slug: a missing card must not claim a place on disk.
    assert row["slug"] == ""


def test_the_roster_carries_each_agents_own_seat_as_a_slug(tree):
    """Iris has a `chat/` and is scoped into it; Atlas has none and stands at
    their root. The slug says which, without anyone parsing an id."""
    from jstack_host import board

    rows = {r["base"]: r for r in board.list_agents((board._SeatStats(), {}))}
    assert rows["iris"]["agent_id"] == "iris-chat"
    assert rows["iris"]["slug"] == "iris/chat"
    assert rows["atlas"]["agent_id"] == "atlas"
    assert rows["atlas"]["slug"] == "atlas"


# ── the picker ──

def test_browse_starts_at_the_agent_root_whatever_seat_is_addressed(tree):
    for agent_id in ("iris", "iris-chat", "iris-code-go"):
        page = seats.browse(agent_id)
        assert page["root"] == str(tree / "Iris")
        assert page["path"] == ""
        assert page["parent"] is None


def test_browse_marks_what_can_be_picked_and_what_can_be_entered(tree):
    rows = {r["name"]: r for r in seats.browse("iris")["entries"]}
    assert set(rows) == {"chat", "code", "notes"}        # pad/git pruned
    assert rows["code"]["is_seat"] and rows["code"]["has_children"]
    assert not rows["notes"]["is_seat"] and rows["notes"]["has_children"]
    assert rows["chat"]["is_seat"] and not rows["chat"]["has_children"]


def test_browse_descends_and_reports_the_way_back(tree):
    page = seats.browse("iris", "code")
    assert page["is_seat"] and page["seat_id"] == "iris-code"
    assert page["parent"] == ""
    assert [r["seat_id"] for r in page["entries"]] == ["iris-code-go"]
    assert seats.browse("iris", "code/go")["parent"] == "code"


@pytest.mark.parametrize("bad", ["..", "../..", "code/../..", "/etc"])
def test_browse_cannot_escape_the_agent_root(tree, bad):
    with pytest.raises((ValueError, KeyError)):
        seats.browse("iris", bad)


def test_browse_of_an_unknown_agent_raises(tree):
    with pytest.raises(KeyError):
        seats.browse("nobody")


# ── the palette ──

def _cmd(d, name, body="---\ndescription: from {where}\n---\n"):
    (d / ".claude" / "commands").mkdir(parents=True, exist_ok=True)
    (d / ".claude" / "commands" / f"{name}.md").write_text(
        body.format(where=d.name))


def _skill(d, name):
    sk = d / ".claude" / "skills" / name
    sk.mkdir(parents=True, exist_ok=True)
    (sk / "SKILL.md").write_text(f"---\ndescription: skill from {d.name}\n---\n")


@pytest.fixture
def palette(tree, tmp_path, monkeypatch):
    user = tmp_path / "user"
    _cmd(user, "push", "---\ndescription: from global\n---\n")
    _cmd(user, "shared", "---\ndescription: from global\n---\n")
    _skill(user, "xcode-build-fixer")
    monkeypatch.setattr(commands, "_USER", user / ".claude")
    monkeypatch.setattr(commands, "_plugin_commands", lambda: [])
    _cmd(tree / "Iris", "create-agent")
    _cmd(tree / "Iris", "shared")
    _cmd(tree / "Iris" / "code", "build")
    _cmd(tree / "Iris" / "code" / "go", "testflight")
    _cmd(tree / "Iris" / "code" / "go", "shared")
    _skill(tree / "Iris" / "code" / "go", "swiftui-expert")
    return tree


def _names(agent_id):
    return [c["name"] for c in commands.list_commands(agent_id)]


def _desc(agent_id, name):
    return [c for c in commands.list_commands(agent_id)
            if c["name"] == name][0]["description"]


def test_palette_carries_the_seats_own_commands(palette):
    """The bug, in one line: this listed the agent root's commands and none of
    `code/go`'s."""
    assert "/testflight" in _names("iris-code-go")


def test_palette_inherits_down_the_chain(palette):
    got = _names("iris-code-go")
    assert "/push" in got            # global
    assert "/create-agent" in got    # agent root
    assert "/build" in got           # an intervening directory


def test_a_sibling_seat_does_not_leak_in(palette):
    assert "/testflight" not in _names("iris-chat")
    assert "/build" not in _names("iris-chat")
    assert "/create-agent" in _names("iris-chat")


def test_nearest_wins(palette):
    """The harness resolves a command by walking up from the session's own
    directory, so the palette has to break ties the same way or it describes a
    command the session will not run."""
    assert _desc("iris-code-go", "/shared") == "from go"
    assert _desc("iris-chat", "/shared") == "from Iris"
    assert _desc("atlas", "/shared") == "from global"


def test_skills_are_listed_as_commands(palette):
    """A seat skill is invoked as `/{name}` exactly like a command. Leaving
    them out hid all three of `nova/chat`'s."""
    assert "/swiftui-expert" in _names("iris-code-go")
    assert "/swiftui-expert" not in _names("iris-chat")


def test_a_user_level_skill_reaches_every_seat(palette):
    """`~/.claude/skills/` is available to every session on the machine, so it
    belongs in every palette. It was in none: The user level read its commands
    and not its skills, hiding the ten Apple SDK skills from root seats and
    subs alike."""
    for agent_id in ("iris-chat", "iris-code", "iris-code-go"):
        assert "/xcode-build-fixer" in _names(agent_id), agent_id


def test_a_seat_may_override_a_user_level_skill(palette):
    """Same nearest-wins tie-break as commands — a seat's own `swiftui-expert`
    is the one that would run, so it is the one described."""
    assert _desc("iris-code", "/xcode-build-fixer") == "skill from user"
    _skill(palette / "Iris" / "code", "xcode-build-fixer")
    assert _desc("iris-code", "/xcode-build-fixer") == "skill from code"
    assert _desc("iris-chat", "/xcode-build-fixer") == "skill from user"


def test_plugin_commands_never_override_a_local_file(tree, tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "_USER", tmp_path / "nope")
    monkeypatch.setattr(commands, "_plugin_commands",
                        lambda: [{"name": "/build", "description": "from plugin"},
                                 {"name": "/jstack:push", "description": "ns"}])
    _cmd(tree / "Iris" / "code", "build")
    got = {c["name"]: c["description"] for c in commands.list_commands("iris-code")}
    assert got["/build"] == "from code"
    assert got["/jstack:push"] == "ns"


def test_an_unresolvable_agent_still_answers(tree, tmp_path, monkeypatch):
    """A palette that raises is a thread with no autocomplete at all; the
    global commands are still true."""
    user = tmp_path / "user"
    _cmd(user, "push", "x\n")
    monkeypatch.setattr(commands, "_USER", user / ".claude")
    monkeypatch.setattr(commands, "_plugin_commands", lambda: [])
    assert _names("nobody") == ["/push"]


# ── a directory marketplace is read live, not from the install snapshot ──

def _plugin_tree(root: Path, plugin: str, skill: str, desc: str):
    """A plugin root on disk: its manifest plus one skill."""
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": plugin, "version": "1.0.0"}))
    sk = root / "skills" / skill
    sk.mkdir(parents=True, exist_ok=True)
    (sk / "SKILL.md").write_text(f"---\ndescription: {desc}\n---\n")


@pytest.fixture
def plugins(tmp_path, monkeypatch):
    """One plugin installed twice over: a live `directory` marketplace holding
    the skill added since, and the frozen cache copy the install recorded.

    Plus a github-sourced plugin, whose cache copy stays authoritative — it is
    version-pinned, so its clone under `marketplaces/` may run ahead of what
    the session actually loads.
    """
    live = tmp_path / "jStack"
    _plugin_tree(live / "plugins" / "jstack", "jstack", "pict", "live wording")
    (live / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (live / ".claude-plugin" / "marketplace.json").write_text(json.dumps(
        {"name": "jStack",
         "plugins": [{"name": "jstack", "source": "./plugins/jstack"}]}))

    cache = tmp_path / "cache"
    _plugin_tree(cache / "jstack" / "0.1.0", "jstack", "stale", "stale wording")
    _plugin_tree(cache / "other" / "1.0.0", "other", "thing", "from cache")

    (tmp_path / "known_marketplaces.json").write_text(json.dumps({
        "jStack": {"source": {"source": "directory", "path": str(live)},
                   "installLocation": str(live)},
        "official": {"source": {"source": "github", "repo": "a/b"},
                     "installLocation": str(tmp_path / "marketplaces" / "official")},
    }))
    (tmp_path / "installed_plugins.json").write_text(json.dumps({"plugins": {
        "jstack@jStack": [{"installPath": str(cache / "jstack" / "0.1.0")}],
        "other@official": [{"installPath": str(cache / "other" / "1.0.0")}],
    }}))

    monkeypatch.setattr(commands, "_USER", tmp_path / "nouser")
    # Both of these were module constants until 98d9e1b9 made them functions,
    # so that the answer is resolved per call on a machine whose plugin dir can
    # move. Patch the callables — patching the old names bound nothing and the
    # tests errored at setup without ever running.
    monkeypatch.setattr(plugin_paths, "known_marketplaces",
                        lambda: tmp_path / "known_marketplaces.json")
    # No cache-root fallback in reach: the snapshot these tests mean is the
    # one `installed_plugins.json` names, not a version scan.
    monkeypatch.setattr(plugin_paths, "plugin_cache", lambda: tmp_path / "nocache")
    monkeypatch.setattr(commands, "_INSTALLED_PLUGINS",
                        tmp_path / "installed_plugins.json")
    return tmp_path


def test_a_directory_marketplace_is_read_where_it_lives(tree, plugins):
    """`~/jStack` is a `directory` marketplace: editing it changes what the
    next session can invoke, with no reinstall. Reading the frozen
    `plugins/cache/` copy instead left the palette hours behind the machine —
    `/jstack:pict` had shipped and the phone had never heard of it."""
    got = {c["name"]: c["description"] for c in commands.list_commands("atlas")}
    assert "/jstack:pict" in got
    assert "/jstack:stale" not in got


def test_the_live_copy_answers_for_descriptions_too(tree, plugins):
    """Not just which skills exist — what each one says. The trim landed in the
    repo and the palette went on quoting the old pitch."""
    got = {c["name"]: c["description"] for c in commands.list_commands("atlas")}
    assert got["/jstack:pict"] == "live wording"


def test_a_github_marketplace_still_reads_its_installed_version(tree, plugins):
    """Version-pinned, so the snapshot is the truth — only directory sources
    are redirected."""
    got = {c["name"]: c["description"] for c in commands.list_commands("atlas")}
    assert got["/other:thing"] == "from cache"


def test_an_unresolvable_live_root_falls_back_to_the_snapshot(tree, plugins,
                                                              monkeypatch):
    """A marketplace manifest that has moved on — renamed plugin, deleted
    source dir — must leave the installed copy standing, not blank the
    namespaced half of the palette."""
    (plugins / "jStack" / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "jStack",
                    "plugins": [{"name": "jstack", "source": "./gone"}]}))
    got = {c["name"]: c["description"] for c in commands.list_commands("atlas")}
    assert got["/jstack:stale"] == "stale wording"


def test_a_description_survives_to_the_length_jstack_enforces(tree, plugins):
    """140 chars is jStack's own ceiling; the palette cut at 120 and ended
    `/jstack:recall` mid-word."""
    long = "Use when " + "x" * 125          # 134 chars
    sk = plugins / "jStack" / "plugins" / "jstack" / "skills" / "pict"
    (sk / "SKILL.md").write_text(f"---\ndescription: {long}\n---\n")
    got = {c["name"]: c["description"] for c in commands.list_commands("atlas")}
    assert got["/jstack:pict"] == long
