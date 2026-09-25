"""The Work surface over the wire — the environment layers, and the plan screens.

What these pin is not "does JSON come back". It is the three things a client
cannot recover from if the host gets them wrong:

  * **Every setting, every time.** A key absent from the list is
    indistinguishable, in the app, from a host that does not have that
    setting — so the list is asserted against the whole registry, not against
    the keys a test happened to write.
  * **`source`.** Inherited and set-here render differently and only the host
    can tell them apart; a value with the wrong provenance is a clear button
    that clears nothing, or one that is missing where it should be.
  * **`announced`.** In force and delivered are different states — a value can
    sit resolved for a whole session without one trigger firing — and the
    markers that know are on disk, where only the host can read them.
  * **`mode`, stated rather than inferred.** No plan and a plan with no stages
    yet are the same two empty collections to a decoder, and they are two
    different screens.

Plus the refusals, each checked for having stored nothing rather than merely
having answered 400, and the token boundary these routes share with every
other one in the router.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jstack_host.server import create_app

app = create_app()
from jstack_host import (auth, docfence, environment as env, hostenv, plans,
                         router, store)
from jstack_host.store import SessionStore

BASE = "/api/jremote/v1"
SID = "3eee625d-6976-482a-8709-b8e7ea4d6526"
OTHER_SID = "9f1c1d2e-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
#: The id `/agents` hands out for an agent with a chat seat — what a client
#: holds when it calls the agent env routes. The store keys on the base.
AGENT = "ops-chat"


def _index_agent(seat: Path) -> str:
    """The `agent_id` the session indexer writes for a transcript under `seat`
    — `store._fresh_state` takes it from `project_dir_to_agent` on Claude's
    project-dir encoding, so this asks the same function with the same input."""
    parsed = hostenv.project_dir_to_agent(str(seat).replace("/", "-"))
    assert parsed, f"the indexer would not attribute {seat} to any agent"
    return parsed[0]


@pytest.fixture(autouse=True)
def agent_tree(tmp_path, monkeypatch):
    """`Ops` with a chat seat, under a profile rooted here — the shape `/agents`
    scopes to `ops-chat`. Resolved, because `project_dir_to_agent` matches the
    encoded root as a string and macOS tmp paths sit behind `/private`."""
    root = (tmp_path / "Agents").resolve()
    (root / "Ops" / "chat").mkdir(parents=True)
    (root / "Ops" / "CLAUDE.md").write_text("# Ops\n")
    (root / "Ops" / "chat" / "CLAUDE.md").write_text("# Ops · chat\n")
    monkeypatch.setattr(hostenv, "_profile", hostenv.DefaultProfile(root))
    return root


@pytest.fixture(autouse=True)
def work_store(tmp_path, monkeypatch, agent_tree):
    """A store of this test's own, through the package's own injection point —
    the same seam `test_environment` uses, which conftest's isolation defers to.
    The session row carries what the real indexer writes for a transcript in
    the Ops chat seat, not the roster id: a fixture holding `ops-chat` there
    agreed with the route's own write and hid that no real session reads it.
    """
    monkeypatch.setattr(store, "_store",
                        SessionStore(db_path=tmp_path / "work.sqlite"))
    with store.get_store().conn() as db:
        db.execute("INSERT INTO sessions (session_id, agent_id) VALUES (?,?)",
                   (SID, _index_agent(agent_tree / "Ops" / "chat")))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "_expected_token", lambda: "test-token")
    c = TestClient(app)
    c.headers.update({"Authorization": "Bearer test-token"})
    return c


def _by_key(body: dict) -> dict:
    return {row["key"]: row for row in body["env"]}


# ── the environment layers ──────────────────────────────────────────────────

def test_an_untouched_session_lists_every_setting_as_a_default(client):
    """The registry, in full, before anyone has set anything. A screen that can
    only draw the keys somebody has written is a screen that starts empty."""
    body = client.get(f"{BASE}/sessions/{SID}/env").json()
    assert body["available"] is True
    rows = _by_key(body)
    assert list(rows) == [s.key for s in env.SETTINGS]
    for s in env.SETTINGS:
        assert rows[s.key] == {"key": s.key, "label": s.label, "kind": s.kind,
                               "values": list(s.values), "default": s.default,
                               "value": s.default, "source": "default",
                               "announced": False}


def test_both_layers_say_whether_a_setting_has_spoken(client, tmp_path,
                                                     monkeypatch):
    """`announced` on every row of both layers, and never true for an agent's.

    The field is what separates a setting in play from one merely armed, so the
    thing to pin is that it is always THERE: a key the client cannot find reads
    as a host too old to have the feature, and the screen would then draw every
    setting as if it had spoken, or none. The agent layer answers `False`
    because nothing announces there — an agent default is what a session will
    inherit, and no session's markers are in reach of that question.
    """
    monkeypatch.setenv("JSTACK_CACHE_ROOT", str(tmp_path / "cache"))
    client.post(f"{BASE}/sessions/{SID}/env",
                json={"key": "sim_verify", "value": "off"})
    client.post(f"{BASE}/agents/{AGENT}/env",
                json={"key": "sim_verify", "value": "off"})

    rows = _by_key(client.get(f"{BASE}/sessions/{SID}/env").json())
    assert all("announced" in row for row in rows.values())
    assert rows["sim_verify"]["announced"] is False

    marker = env.announce_marker(SID, "sim_verify", "off")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("0")
    session = _by_key(client.get(f"{BASE}/sessions/{SID}/env").json())
    assert session["sim_verify"]["announced"] is True

    agent = _by_key(client.get(f"{BASE}/agents/{AGENT}/env").json())
    assert all(row["announced"] is False for row in agent.values())
    # The Work view serves the same rows, so the mark reaches the screen that
    # exists to show it.
    work = _by_key(client.get(f"{BASE}/sessions/{SID}/work").json())
    assert work["sim_verify"]["announced"] is True


def test_a_posted_value_comes_back_as_set_on_this_session(client):
    posted = client.post(f"{BASE}/sessions/{SID}/env",
                         json={"key": "delivery_method", "value": "distribute"})
    assert posted.status_code == 200
    # The POST answers the stored truth, so the app never has to re-fetch to
    # redraw — and the GET must agree with it.
    assert _by_key(posted.json())["delivery_method"]["value"] == "distribute"

    row = _by_key(client.get(f"{BASE}/sessions/{SID}/env").json())["delivery_method"]
    assert (row["value"], row["source"]) == ("distribute", "session")
    assert env.get("delivery_method", session_id=SID) == "distribute"


def test_an_agent_value_reaches_the_sessions_of_that_agent(client):
    posted = client.post(f"{BASE}/agents/{AGENT}/env",
                         json={"key": "use_subagents", "value": "on"})
    written = _by_key(posted.json())["use_subagents"]
    assert (written["value"], written["source"]) == ("on", "agent")

    # The session never set it, so it reads the agent's word and is labelled
    # as having done so — the difference between a row the screen offers to
    # clear and one it does not.
    row = _by_key(client.get(f"{BASE}/sessions/{SID}/env").json())["use_subagents"]
    assert (row["value"], row["source"]) == ("on", "agent")


def test_the_roster_id_writes_the_layer_a_real_session_of_that_seat_reads(
        client, agent_tree):
    """The id a client actually holds — the one `/agents` served — must land
    where both readers look: the indexed session (`resolve` walks `sessions`)
    and the cold start (the entry hook passes the seat's agent explicitly,
    before any row exists). Keyed on `alpha-chat` verbatim, the write answered
    200, read back through this same route, and reached no session at all.
    """
    roster = client.get(f"{BASE}/agents").json()["agents"]
    scoped = next(a["agent_id"] for a in roster if a["base"] == "ops")
    assert scoped == "ops-chat", roster

    posted = client.post(f"{BASE}/agents/{scoped}/env",
                         json={"key": "delivery_method", "value": "testflight"})
    assert posted.status_code == 200, posted.text

    assert env.resolve(SID)["delivery_method"] == ("testflight", "agent")
    cold = "0b7c2a44-5f0e-4d1a-9a55-6f7e3c2b1d00"
    seat_agent = _index_agent(agent_tree / "Ops" / "chat")
    assert env.resolve(cold, seat_agent)["delivery_method"] == ("testflight", "agent")
    # Read back through the base id too: one row, whichever spelling asks.
    row = _by_key(client.get(f"{BASE}/agents/ops/env").json())["delivery_method"]
    assert (row["value"], row["source"]) == ("testflight", "agent")


def test_an_agent_nobody_has_is_a_404_and_stores_nothing(client):
    for method in ("get", "post"):
        r = client.request(method.upper(), f"{BASE}/agents/nobody-chat/env",
                           json={"key": "sim_verify", "value": "off"})
        assert r.status_code == 404, (method, r.text)
    with store.get_store().conn() as db:
        assert db.execute("SELECT count(*) FROM agent_env").fetchone()[0] == 0


def test_an_agent_layer_reads_its_own_rows_and_nothing_elses(client):
    """The agent screen shows the agent's defaults, never a session's pin —
    otherwise setting a default would appear to already be set."""
    client.post(f"{BASE}/sessions/{SID}/env",
                json={"key": "sim_verify", "value": "off"})
    row = _by_key(client.get(f"{BASE}/agents/{AGENT}/env").json())["sim_verify"]
    assert (row["value"], row["source"]) == ("on", "default")


def test_a_null_value_clears_back_to_what_was_inherited(client):
    client.post(f"{BASE}/agents/{AGENT}/env",
                json={"key": "sim_verify", "value": "off"})
    client.post(f"{BASE}/sessions/{SID}/env",
                json={"key": "sim_verify", "value": "on"})
    assert _by_key(client.get(f"{BASE}/sessions/{SID}/env").json()
                   )["sim_verify"]["source"] == "session"

    cleared = client.post(f"{BASE}/sessions/{SID}/env",
                          json={"key": "sim_verify", "value": None})
    row = _by_key(cleared.json())["sim_verify"]
    # Back to the agent's word, not to the registry's: "no opinion here" and
    # "off" are different, and a row holding '' would collapse them.
    assert (row["value"], row["source"]) == ("off", "agent")
    assert env.get("sim_verify", session_id=SID) == ""


def test_an_unknown_key_is_a_400_and_stores_nothing(client):
    r = client.post(f"{BASE}/sessions/{SID}/env",
                    json={"key": "sim_verfiy", "value": "off"})
    assert r.status_code == 400
    assert "sim_verfiy" in r.json()["detail"]
    with store.get_store().conn() as db:
        assert db.execute("SELECT count(*) FROM session_env").fetchone()[0] == 0


def test_a_value_outside_the_enum_is_a_400_and_stores_nothing(client):
    r = client.post(f"{BASE}/sessions/{SID}/env",
                    json={"key": "delivery_method", "value": "carrier_pigeon"})
    assert r.status_code == 400
    with store.get_store().conn() as db:
        assert db.execute("SELECT count(*) FROM session_env").fetchone()[0] == 0


def test_a_missing_key_is_a_400(client):
    assert client.post(f"{BASE}/sessions/{SID}/env",
                       json={"value": "on"}).status_code == 400


def test_a_malformed_session_id_never_reaches_the_store(client):
    assert client.get(f"{BASE}/sessions/not-a-session/env").status_code == 400
    assert client.post(f"{BASE}/sessions/not-a-session/env",
                       json={"key": "sim_verify", "value": "off"}).status_code == 400


def test_a_write_pokes_the_board_and_a_read_does_not(client, monkeypatch):
    """A connected app repaints on the action, not on the next tick — and a
    poll that poked would wake every device on every read."""
    pokes = []
    from jstack_host import board_watch
    monkeypatch.setattr(board_watch, "poke", lambda: pokes.append(1))

    client.get(f"{BASE}/sessions/{SID}/env")
    client.get(f"{BASE}/sessions/{SID}/work")
    assert pokes == []

    client.post(f"{BASE}/sessions/{SID}/env",
                json={"key": "sim_verify", "value": "off"})
    client.post(f"{BASE}/agents/{AGENT}/env",
                json={"key": "sim_verify", "value": "off"})
    assert len(pokes) == 2


# ── the Work view's three states ────────────────────────────────────────────

def test_no_plan_is_mode_none(client):
    body = client.get(f"{BASE}/sessions/{SID}/work").json()
    assert body["available"] is True
    assert body["mode"] == "none"
    assert body["plan"] is None and body["stages"] == [] and body["tasks"] == {}
    # The environment rides along in the env routes' own shape, so a client
    # opening the Work view does not need a second call to draw its settings.
    assert _by_key(body) == _by_key(client.get(f"{BASE}/sessions/{SID}/env").json())


def test_a_plan_with_no_stages_yet_is_mode_planning(client):
    plans.open_plan("the harness", session_id=SID)
    body = client.get(f"{BASE}/sessions/{SID}/work").json()
    assert body["mode"] == "planning"
    assert body["plan"]["title"] == "the harness" and body["stages"] == []


def test_stages_are_mode_stages(client):
    plan_id = plans.open_plan("the harness", session_id=SID)
    plans.set_stages(plan_id, [{"title": "the router", "verify_kind": "command",
                                "verify_spec": "true"},
                               {"title": "the client"}])
    plans.activate(plan_id)
    body = client.get(f"{BASE}/sessions/{SID}/work").json()
    assert body["mode"] == "stages"
    assert [s["title"] for s in body["stages"]] == ["the router", "the client"]
    assert body["tasks"] == {s["id"]: [] for s in body["stages"]}


def test_every_mode_served_is_one_the_vocabulary_declares(client):
    plans.open_plan("the harness", session_id=SID)
    assert client.get(f"{BASE}/sessions/{SID}/work").json()["mode"] in router.WORK_MODES


def test_another_sessions_plan_is_not_this_ones(client):
    plans.open_plan("the harness", session_id=OTHER_SID)
    assert client.get(f"{BASE}/sessions/{SID}/work").json()["mode"] == "none"


# ── the plan screens ────────────────────────────────────────────────────────

def test_plans_lists_what_the_host_knows(client):
    plans.open_plan("first", session_id=SID)
    plans.open_plan("second", session_id=SID)
    body = client.get(f"{BASE}/plans").json()
    assert body["available"] is True
    assert {p["title"] for p in body["plans"]} == {"first", "second"}


def test_a_plan_carries_its_stages_and_their_evidence(client):
    plan_id = plans.open_plan("the harness", session_id=SID)
    plans.set_stages(plan_id, [{"title": "the router", "verify_kind": "command",
                                "verify_spec": "true"}])
    stage_id = plans.stages(plan_id)[0]["id"]
    plans.add_proof(stage_id, "command", True, detail="true", exit_code=0)

    body = client.get(f"{BASE}/plans/{plan_id}").json()
    assert body["plan"]["id"] == plan_id
    assert [s["title"] for s in body["stages"]] == ["the router"]
    # Keyed by stage, because that is how the screen draws it.
    assert [p["kind"] for p in body["proofs"][stage_id]] == ["command"]


def test_an_unknown_plan_is_a_404(client):
    assert client.get(f"{BASE}/plans/nope").status_code == 404
    assert client.get(f"{BASE}/plans/nope/document").status_code == 404


def test_a_plans_document_is_served_through_the_read_fence(client, monkeypatch,
                                                           tmp_path):
    root = tmp_path / "roots"
    root.mkdir()
    monkeypatch.setattr(docfence, "read_roots", lambda: (root.resolve(),))
    doc = root / "plan.md"
    doc.write_text("# the harness\n")
    plan_id = plans.open_plan("the harness", plan_file=str(doc), session_id=SID)

    body = client.get(f"{BASE}/plans/{plan_id}/document").json()
    assert body["name"] == "plan.md" and body["text"] == "# the harness\n"


def test_a_document_outside_the_fence_is_refused_not_served(client, monkeypatch,
                                                            tmp_path):
    """The column was written by whatever authored the plan, so it is no more
    trustworthy than a path off the wire."""
    root = tmp_path / "roots"
    root.mkdir()
    monkeypatch.setattr(docfence, "read_roots", lambda: (root.resolve(),))
    outside = tmp_path / "elsewhere.md"
    outside.write_text("# private")
    plan_id = plans.open_plan("elsewhere", plan_file=str(outside), session_id=SID)
    assert client.get(f"{BASE}/plans/{plan_id}/document").status_code == 403


def test_a_plan_with_no_document_says_so(client):
    plan_id = plans.open_plan("no file", session_id=SID)
    r = client.get(f"{BASE}/plans/{plan_id}/document")
    assert r.status_code == 404 and "no document" in r.json()["detail"]


# ── a host whose store has none of this ─────────────────────────────────────

@pytest.fixture
def unmigrated(client):
    """A store that predates the migration: the tables are gone, and nothing
    recreates them until a `SessionStore` is built again."""
    with store.get_store().conn() as db:
        for table in router._ENV_TABLES + router._WORK_TABLES:
            db.execute(f"DROP TABLE {table}")
    return client


@pytest.mark.parametrize("path,empty", [
    (f"/sessions/{SID}/env", {"env": []}),
    (f"/agents/{AGENT}/env", {"env": []}),
    (f"/sessions/{SID}/work", {"plan": None, "stages": [], "tasks": {},
                               "env": [], "mode": "unavailable"}),
    ("/plans", {"plans": []}),
])
def test_absence_is_answered_in_words_never_as_an_empty_screen(unmigrated, path,
                                                               empty):
    r = unmigrated.get(BASE + path)
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert "not available" in body["reason"]
    assert {k: body[k] for k in empty} == empty


def test_an_unmigrated_host_refuses_a_write_rather_than_swallowing_it(unmigrated):
    assert unmigrated.post(f"{BASE}/sessions/{SID}/env",
                           json={"key": "sim_verify", "value": "off"}
                           ).status_code == 503
    assert unmigrated.post(f"{BASE}/agents/{AGENT}/env",
                           json={"key": "sim_verify", "value": "off"}
                           ).status_code == 503


def test_the_host_advertises_both_capabilities_before_a_screen_asks(client):
    features = client.get(f"{BASE}/host").json()["features"]
    assert features["session_env"] is True and features["work"] is True


def test_an_unmigrated_host_advertises_neither(unmigrated):
    """The probe answers from the same read the routes make, so the capability
    map and the screens cannot disagree about what this machine can do."""
    features = unmigrated.get(f"{BASE}/host").json()["features"]
    assert features["session_env"] is False and features["work"] is False


# ── the token boundary ──────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path", [
    ("get", f"/sessions/{SID}/env"),
    ("post", f"/sessions/{SID}/env"),
    ("get", f"/agents/{AGENT}/env"),
    ("post", f"/agents/{AGENT}/env"),
    ("get", f"/sessions/{SID}/work"),
    ("get", "/plans"),
    ("get", "/plans/any"),
    ("get", "/plans/any/document"),
])
def test_every_route_is_behind_the_token(method, path):
    r = TestClient(app).request(method.upper(), BASE + path, json={})
    assert r.status_code == 401
