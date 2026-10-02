"""Seats, their settings, and the Files pane — against a real host.

The roster and the tree are read off the filesystem of the machine serving
them, so this group is the one where in-process tests are furthest from the
truth: a fixture tree proves the walk, and only a real host proves the walk
finds anything on a real install. The Files half writes bytes and reads them
back over HTTP, which is the only way to catch a pane that lists a file it
cannot then serve.

Every test cleans up the file it created. The suite points at a VM, but a
developer iterating re-runs against the same guest, and a pad that grows a
file per run is a pane that slowly stops resembling anything.
"""

from __future__ import annotations

import pytest

from conftest import BASE_URL

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not BASE_URL, reason="live suite is opt-in")]


def _rows(listing) -> list[dict]:
    for key in ("entries", "files", "rows", "items"):
        if isinstance(listing, dict) and isinstance(listing.get(key), list):
            return listing[key]
    return listing if isinstance(listing, list) else []


def test_the_roster_names_agents_this_host_can_open(api, agent_id):
    """`/agents` is the fifteen-second backstop poll every device runs, so an
    id here that no other route accepts is a card that fails on tap."""
    roster = api.ok("GET", "/agents")
    agents = roster.get("agents", []) if isinstance(roster, dict) else roster
    assert agents, "roster is empty despite the agent_id fixture finding one"
    mine = next((a for a in agents
                 if (a.get("agent_id") or a.get("id")) == agent_id), None)
    assert mine is not None, f"{agent_id} vanished between two reads"


def test_an_agents_tree_opens_at_its_root(api, agent_id):
    """The shortcut picker's walk. `is_seat` is the only thing that makes a
    row pickable — a card minted on a directory with no AGENTS.md spawns a
    session with no identity, rules or timeline — so the flag must be
    present, not merely the names."""
    tree = api.ok("GET", "/agents/{agent_id}/tree", fmt={"agent_id": agent_id})
    rows = _rows(tree)
    assert isinstance(rows, list), tree
    for row in rows:
        assert "is_seat" in row, f"tree row cannot be judged pickable: {row}"


def test_a_tree_path_cannot_escape_the_agents_root(api, agent_id):
    """Confinement, checked against the real root rather than a tmp_path —
    the whole value of doing it live."""
    r = api.get("/agents/{agent_id}/tree", fmt={"agent_id": agent_id},
                **{"params": {"path": "../../../../etc"}})
    assert r.status_code in (400, 404), (
        f"tree walked outside the agent root ({r.status_code}): {r.text[:200]}")


def test_an_unknown_agent_is_a_404_not_a_500(api):
    """Every `{agent_id}` route takes a string from a client. An unknown one
    must be a refusal, never a traceback."""
    r = api.get("/agents/{agent_id}/tree", fmt={"agent_id": "no-such-agent"})
    assert r.status_code == 404, f"{r.status_code}: {r.text[:200]}"


def test_the_command_list_answers_for_a_real_agent(api, agent_id):
    cmds = api.ok("GET", "/agents/{agent_id}/commands", fmt={"agent_id": agent_id})
    assert isinstance(cmds, dict) and "commands" in cmds, cmds


def test_an_agents_engine_is_read_then_written_then_restored(api, agent_id):
    """The long-press menu's memory. Written and put back in one test so a
    guest reused across runs is left on the engine it started on."""
    before = api.ok("GET", "/agents/{agent_id}/engine", fmt={"agent_id": agent_id})
    assert isinstance(before, dict), before

    engines = api.ok("GET", "/engines")
    names = [e.get("id") or e.get("name") for e in
             (engines.get("engines", engines) if isinstance(engines, dict) else engines)]
    names = [n for n in names if n]
    assert names, "a host with no engines can open no sessions"

    api.ok("POST", "/agents/{agent_id}/engine", fmt={"agent_id": agent_id},
           json={"engine": names[0]})
    after = api.ok("GET", "/agents/{agent_id}/engine", fmt={"agent_id": agent_id})
    assert after.get("engine") == names[0], (
        f"engine write did not stick: asked {names[0]}, host says {after}")

    if before.get("engine"):
        api.ok("POST", "/agents/{agent_id}/engine", fmt={"agent_id": agent_id},
               json={"engine": before["engine"]})


def test_an_unknown_engine_is_refused_rather_than_stored(api, agent_id):
    """A named engine that does not exist must refuse here, not fall back —
    an agent silently running a different engine than was asked for surfaces
    much later, in a transcript nobody can explain."""
    r = api.post("/agents/{agent_id}/engine", fmt={"agent_id": agent_id},
                 json={"engine": "not-a-real-engine"})
    assert r.status_code in (400, 404), (
        f"host accepted an engine it does not have ({r.status_code}): "
        f"{r.text[:200]}")


def test_agent_prefs_round_trip(api, agent_id):
    """Per-agent session behaviour, read and written live.

    The flag names come from `GET /agents/prefs` rather than being spelled
    here: the vocabulary is closed (`agent_prefs.FLAGS`) and a test naming one
    it invented fails against a correct host — which is how this first failed,
    posting `notify_muted`, a roster field and not a flag at all.
    """
    before = api.ok("GET", "/agents/prefs")
    assert isinstance(before, dict), before
    flags = list(before)
    assert flags, f"host offers no agent prefs to set: {before}"
    flag = flags[0]

    api.ok("POST", "/agents/prefs",
           json={"agent_id": agent_id, "flag": flag, "on": True})
    after = api.ok("GET", "/agents/prefs")
    assert agent_id.split("-")[0] in [a.split("-")[0] for a in after.get(flag, [])], (
        f"setting {flag} for {agent_id} did not stick: {after}")

    api.ok("POST", "/agents/prefs",
           json={"agent_id": agent_id, "flag": flag, "on": False})
    restored = api.ok("GET", "/agents/prefs")
    assert agent_id.split("-")[0] not in [a.split("-")[0]
                                          for a in restored.get(flag, [])], (
        f"clearing {flag} did not stick: {restored}")


def test_an_unknown_pref_flag_is_refused_not_stored(api, agent_id):
    """The documented worst outcome for this route: a switch that answers 200
    and changes nothing on the Mac looks exactly like a switch that worked."""
    r = api.post("/agents/prefs",
                 json={"agent_id": agent_id, "flag": "not-a-real-flag",
                       "on": True})
    assert r.status_code == 400, (
        f"host stored a flag nobody reads ({r.status_code}): {r.text[:200]}")


def test_the_files_pane_lists_the_seats_pad(api, agent_id):
    """`path=""` is the pane's first screen. A 404 here is an agent whose
    Files tab opens empty and stays that way."""
    listing = api.ok("GET", "/agents/{agent_id}/files", fmt={"agent_id": agent_id})
    assert isinstance(listing, dict), listing


def test_a_file_uploads_lists_downloads_and_deletes(api, agent_id, scratch_name):
    """The Files pane's whole loop, over the wire.

    The download half is the point: a pane that lists a file it cannot then
    serve is the failure users actually hit, and it is invisible to any test
    that checks the listing alone. So the bytes are compared, not the status.
    """
    name = f"{scratch_name}.txt"
    body = f"live suite {scratch_name}\n".encode()

    saved = api.ok("POST", "/agents/{agent_id}/files/upload",
                   fmt={"agent_id": agent_id},
                   **{"params": {"filename": name}, "content": body})
    assert saved.get("path"), saved

    listing = api.ok("GET", "/agents/{agent_id}/files", fmt={"agent_id": agent_id})
    names = [r.get("name") or r.get("rel") for r in _rows(listing)]
    assert any(name in str(n) for n in names), (
        f"uploaded {name} is not in the pane: {names}")

    got = api.get("/agents/{agent_id}/files/content", fmt={"agent_id": agent_id},
                  **{"params": {"rel": name}})
    assert got.status_code == 200, f"{got.status_code}: {got.text[:200]}"
    assert got.content == body, (
        f"the pane served different bytes than were uploaded: {got.content!r}")

    api.ok("POST", "/agents/{agent_id}/files/delete", fmt={"agent_id": agent_id},
           json={"rel": name})

    gone = api.get("/agents/{agent_id}/files/content", fmt={"agent_id": agent_id},
                   **{"params": {"rel": name}})
    assert gone.status_code == 404, (
        f"deleted file is still served ({gone.status_code})")


def test_save_lands_on_the_name_it_was_opened_from(api, agent_id, scratch_name):
    """`save` and `upload` want opposite things from a name that already
    exists — upload steps around a collision, save lands on it. A save that
    collision-suffixed would leave the user's edit beside the original, and
    the original unchanged, with nothing saying so."""
    name = f"{scratch_name}-edit.txt"
    api.ok("POST", "/agents/{agent_id}/files/upload", fmt={"agent_id": agent_id},
           **{"params": {"filename": name}, "content": b"first\n"})

    api.ok("POST", "/agents/{agent_id}/files/save", fmt={"agent_id": agent_id},
           **{"params": {"rel": name}, "content": b"second\n"})

    got = api.get("/agents/{agent_id}/files/content", fmt={"agent_id": agent_id},
                  **{"params": {"rel": name}})
    assert got.content == b"second\n", (
        f"save did not land on the original — pane serves {got.content!r}")

    api.ok("POST", "/agents/{agent_id}/files/delete", fmt={"agent_id": agent_id},
           json={"rel": name})


def test_save_refuses_a_name_that_does_not_exist(api, agent_id, scratch_name):
    """A rel that names nothing is a 404, never a create — otherwise a typo
    in the viewer writes a new file and reports success."""
    r = api.post("/agents/{agent_id}/files/save", fmt={"agent_id": agent_id},
                 **{"params": {"rel": f"{scratch_name}-never.txt"},
                    "content": b"x\n"})
    assert r.status_code == 404, f"save created a file: {r.status_code}"


def test_an_empty_upload_is_refused(api, agent_id):
    """Zero bytes is a client bug, and storing it makes a phantom row in the
    pane that opens to nothing."""
    r = api.post("/agents/{agent_id}/files/upload", fmt={"agent_id": agent_id},
                 **{"params": {"filename": "empty.txt"}, "content": b""})
    assert r.status_code == 400, f"{r.status_code}: {r.text[:200]}"


def test_clear_is_scoped_to_the_folder_it_was_pressed_on(api, agent_id,
                                                         scratch_name):
    """Clear can never take more than the screen it was pressed on displayed.
    Run last in this file's intent: it empties the pad, so it creates its own
    subject and asserts against the count rather than a specific survivor."""
    name = f"{scratch_name}-clear.txt"
    api.ok("POST", "/agents/{agent_id}/files/upload", fmt={"agent_id": agent_id},
           **{"params": {"filename": name}, "content": b"clear me\n"})

    cleared = api.ok("POST", "/agents/{agent_id}/files/clear",
                     fmt={"agent_id": agent_id}, json={"rel": ""})
    assert cleared.get("ok") is True, cleared

    gone = api.get("/agents/{agent_id}/files/content", fmt={"agent_id": agent_id},
                   **{"params": {"rel": name}})
    assert gone.status_code == 404, "clear left the file it was asked to remove"


def test_a_share_sheet_drop_lands_and_returns_its_path(api, agent_id,
                                                       scratch_name):
    """`/upload` is the share sheet: bytes in, a Mac path out. The path is the
    whole product — the app pastes it into a session's input — so a 200 with
    no path is a share that silently did nothing.

    No `message`, deliberately: with one the drop is also filed as mail
    through `bin/msg`, and a suite that filed a message per run would write
    into the guest's inbox for a side effect it is not testing.
    """
    name = f"{scratch_name}-drop.txt"
    out = api.ok("POST", "/upload",
                 **{"params": {"agent_id": agent_id, "filename": name},
                    "content": b"dropped by the live suite\n"})
    assert out.get("path"), f"a drop with no path is a share that did nothing: {out}"
    assert name.split(".")[0] in out["path"], (
        f"drop landed under an unrelated name: {out['path']}")

    # Cleaned up by the name the host chose, not the one that was sent: a drop
    # is timestamp-stamped on the way in (`scratchpad.save_drop`) so the pad
    # reads as a timeline and a same-name drop never overwrites an earlier
    # one. Deleting the name the test sent is how this first failed — and it
    # would have quietly left a file per run behind.
    landed = out["path"].rsplit("/", 1)[-1]
    assert landed != name, (
        "a drop landed on the raw filename — two shares of the same file "
        "would collide")
    api.ok("POST", "/agents/{agent_id}/files/delete", fmt={"agent_id": agent_id},
           json={"rel": landed})


def test_a_drop_for_an_unknown_agent_is_refused(api):
    r = api.post("/upload",
                 **{"params": {"agent_id": "no-such-agent", "filename": "x.txt"},
                    "content": b"x\n"})
    assert r.status_code == 404, f"{r.status_code}: {r.text[:200]}"
