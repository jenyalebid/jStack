"""Whether a machine's client draws Home's Usage section — and whose word that is.

Two authorities, never one. A hub answers for itself; a leaf's answer belongs
to the hub that adopted it, cached locally so the verdict survives a hub that
is asleep. Every test below names the break it would catch, and three of them
exist because the failure they catch is silent: a section that vanishes with no
control to bring it back, a policy handed to a phone that was never entitled to
it, and a leaf writing a value its own next pull throws away.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jstack_host import devices, managed_access, router, usage_reporting
from jstack_host.store import SessionStore

API = "/api/jremote/v1"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_reporting, "_STATE",
                        tmp_path / "jremote_usage_reporting.json")
    # Default to a hub. A leaf is the exception each test that needs one asks for.
    monkeypatch.setattr(managed_access, "is_leaf", lambda: False)


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = SessionStore(db_path=tmp_path / "usage.sqlite")
    monkeypatch.setattr(devices, "_store", lambda: s)
    monkeypatch.setattr("jstack_host.store.get_store", lambda: s)
    return s


@pytest.fixture
def client(app, store):
    _, token = devices.mint("test-device")
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


@pytest.fixture
def as_leaf(monkeypatch):
    """Stand this machine up as a managed one, and let an ordinary token in.

    A real leaf refuses a locally minted device token outright — the hub owns
    who reaches it, which is `test_managed_access`'s ground and not this file's.
    Stubbing the authorization is what leaves these tests asserting the gate
    they are about: what the Usage routes do once a caller is through the door.
    """
    monkeypatch.setattr(managed_access, "is_leaf", lambda: True)
    monkeypatch.setattr(managed_access, "authorize", lambda device_id, request: None)


@pytest.fixture
def on_console(monkeypatch):
    """Act as the hub's own menu bar — TestClient's address is not loopback."""
    monkeypatch.setattr(managed_access, "console", lambda request: True)


# ── the three words ──────────────────────────────────────────────────────────

def test_only_the_three_states_are_storable():
    """A typo that stores cleanly is a control that appears to work and does not."""
    for state in ("client", "hidden", "available"):
        assert usage_reporting.normalise(state) == state
    for bad in ("Hidden", "visible", "", None, True, 1):
        with pytest.raises(usage_reporting.UnknownState):
            usage_reporting.normalise(bad)


def test_the_refusal_names_what_it_would_have_accepted():
    """Caught by `hub/usage-reporting` on its first run: the 400 said what was
    wrong and not what was right, which sends whoever reads it into this file
    to find out what to send. The route passes this text through as `detail`,
    so the message is the API's answer and not only a log line."""
    with pytest.raises(usage_reporting.UnknownState) as raised:
        usage_reporting.normalise("sideways")
    said = str(raised.value)
    assert "sideways" in said
    for state in usage_reporting.STATES:
        assert state in said


def test_a_machine_that_never_chose_leaves_the_choice_to_the_client():
    """The default every already-adopted machine lives under.

    `hidden` on upgrade would take a section away from everyone at once, with
    no act by anybody that asked for it.
    """
    assert usage_reporting.own() == usage_reporting.CLIENT
    assert usage_reporting.cached_parent() == usage_reporting.CLIENT
    assert usage_reporting.effective() == usage_reporting.CLIENT


def test_its_own_word_survives_being_set():
    assert usage_reporting.set_own("hidden") == "hidden"
    assert usage_reporting.own() == "hidden"
    assert usage_reporting.set_own("client") == "client"
    assert usage_reporting.own() == "client"


def test_a_state_this_build_never_heard_of_reads_as_client():
    """Forward compatibility in the safe direction.

    A state file written by a newer host must not leave this one enforcing a
    word it cannot interpret — and the unsafe reading of an uninterpretable
    word is any reading that hides something.
    """
    usage_reporting._store(own="obscured", parent="obscured")
    assert usage_reporting.own() == usage_reporting.CLIENT
    assert usage_reporting.cached_parent() == usage_reporting.CLIENT


def test_a_hubs_answer_is_its_own_and_a_leafs_is_its_hubs(monkeypatch):
    usage_reporting._store(own="available", parent="hidden")
    assert usage_reporting.effective() == "available"
    monkeypatch.setattr(managed_access, "is_leaf", lambda: True)
    assert usage_reporting.effective() == "hidden"


def test_a_missing_field_from_the_hub_retires_a_cached_hidden():
    """The hub dropping its policy must stop the leaf enforcing the old one."""
    usage_reporting.note_parent("hidden")
    assert usage_reporting.cached_parent() == "hidden"
    assert usage_reporting.note_parent(None) == usage_reporting.CLIENT
    assert usage_reporting.cached_parent() == usage_reporting.CLIENT


def test_an_unchanged_verdict_does_not_rewrite_the_state_dir(monkeypatch):
    """`note_parent` rides the roster pull, four times a minute per client."""
    writes = []
    monkeypatch.setattr(usage_reporting, "_store",
                        lambda **f: writes.append(f))
    usage_reporting.note_parent(usage_reporting.CLIENT)
    assert writes == []
    usage_reporting.note_parent("hidden")
    assert writes == [{"parent": "hidden"}]


# ── the column a running hub's store does not have yet ───────────────────────

#: The `hosts` table exactly as it stood before this policy — the shape every
#: already-adopting hub's store file has on disk.
OLD_HOSTS = """CREATE TABLE hosts (
  key TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
  address TEXT NOT NULL DEFAULT '', port INTEGER NOT NULL DEFAULT 9090,
  enrolled_at INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL DEFAULT 0, seq INTEGER NOT NULL DEFAULT 0,
  device_id TEXT NOT NULL DEFAULT '', sees_home INTEGER NOT NULL DEFAULT 1,
  sees_leaves INTEGER NOT NULL DEFAULT 1,
  shell_pubkey TEXT NOT NULL DEFAULT '', shell_user TEXT NOT NULL DEFAULT '')"""


def test_a_store_that_predates_the_column_gains_it_and_defaults_to_client(tmp_path):
    """The store is OPENED by a running hub, never created fresh on an upgrade.

    Without the migration the first flip on an upgraded Mac raises, and every
    already-adopted machine reads as having no policy at all — which is the
    right answer by luck and the wrong one the moment somebody sets `hidden`.
    """
    import sqlite3

    db = tmp_path / "old.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute(OLD_HOSTS)
        conn.execute("INSERT INTO hosts (key, name) VALUES ('leaf-old', 'Office')")

    s = SessionStore(db_path=db)
    assert s.host_row("leaf-old")["usage_reporting"] == usage_reporting.CLIENT
    assert s.set_host_usage_reporting("leaf-old", "hidden") is True
    assert s.host_row("leaf-old")["usage_reporting"] == "hidden"


# ── /host: the one route that answers before any screen ──────────────────────

def test_the_policy_is_answered_to_loopback_and_to_nobody_else(client, monkeypatch):
    """A phone must never be handed a flag about somebody else's Mac.

    Withheld at the server and not by client courtesy: the field governs the
    client running on that machine, and a remote device that received it would
    be a remote device deciding it applied to itself.
    """
    usage_reporting.set_own("hidden")
    monkeypatch.setattr(router, "_is_loopback", lambda ip: False)
    assert "usage_reporting" not in client.get(f"{API}/host").json()
    monkeypatch.setattr(router, "_is_loopback", lambda ip: True)
    assert client.get(f"{API}/host").json()["usage_reporting"] == "hidden"


# ── the hub's word about a leaf ──────────────────────────────────────────────

def test_setting_a_leafs_policy_writes_the_row_and_grades_the_poke(
        client, store, on_console, monkeypatch):
    """The row is the authority; the poke is a nicety.

    An unreachable leaf is a machine that catches up on its next roster pull,
    so a refused poke must not read as a refused flip — the operator would
    click again, and again, against a setting that already took.
    """
    store.upsert_host("leaf-one", "Office", "10.66.0.9")
    monkeypatch.setattr(usage_reporting, "poke",
                        lambda key, **kw: {"step": f"usage-refresh:{key}",
                                           "ok": False, "note": "unreachable"})
    answer = client.post(f"{API}/hosts/leaf-one/usage", json={"state": "hidden"})
    assert answer.status_code == 200
    assert answer.json()["usage_reporting"] == "hidden"
    assert answer.json()["steps"][0]["ok"] is False
    assert store.host_row("leaf-one")["usage_reporting"] == "hidden"


def test_a_leafs_policy_is_console_only(client, store):
    """Off the menu bar it is a device rewriting another machine's visibility."""
    store.upsert_host("leaf-one", "Office", "10.66.0.9")
    assert client.post(f"{API}/hosts/leaf-one/usage",
                       json={"state": "hidden"}).status_code == 403
    assert store.host_row("leaf-one")["usage_reporting"] == usage_reporting.CLIENT


def test_an_unknown_state_or_an_unknown_machine_is_refused(client, store, on_console):
    store.upsert_host("leaf-one", "Office", "10.66.0.9")
    assert client.post(f"{API}/hosts/leaf-one/usage",
                       json={"state": "invisible"}).status_code == 400
    assert client.post(f"{API}/hosts/nobody/usage",
                       json={"state": "hidden"}).status_code == 404


def test_the_policy_stays_off_the_device_wire_and_rides_the_console_one(store):
    """Devices tile these rows. None of them is entitled to this field."""
    store.upsert_host("leaf-one", "Office", "10.66.0.9")
    store.set_host_usage_reporting("leaf-one", "hidden")
    row = store.host_row("leaf-one")
    assert "usage_reporting" not in router._serve_host(dict(row))
    assert router._serve_host(dict(row), policy=True)["usage_reporting"] == "hidden"


# ── a machine's word about itself ────────────────────────────────────────────

def test_a_hub_sets_its_own_word_from_its_own_console(client, on_console):
    assert client.post(f"{API}/usage/reporting",
                       json={"state": "available"}).status_code == 200
    assert usage_reporting.own() == "available"
    assert client.post(f"{API}/usage/reporting",
                       json={"state": "sideways"}).status_code == 400


def test_its_own_word_is_console_only(client):
    assert client.post(f"{API}/usage/reporting",
                       json={"state": "hidden"}).status_code == 403
    assert usage_reporting.own() == usage_reporting.CLIENT


def test_a_leaf_is_refused_its_own_word_rather_than_told_it_took(
        client, on_console, as_leaf):
    """A control that stores a value the next pull discards is worse than a no."""
    assert client.post(f"{API}/usage/reporting",
                       json={"state": "hidden"}).status_code == 409
    assert usage_reporting.own() == usage_reporting.CLIENT


# ── the poke's landing, and the answer it pulls ──────────────────────────────

def test_only_a_managed_machine_pulls_a_policy(client):
    assert client.post(f"{API}/usage/refresh").status_code == 409


def test_the_pull_takes_the_hubs_word_through_the_parent_credential(
        client, as_leaf, monkeypatch):
    """Nothing in the poke is trusted for the value — the leaf goes and asks."""
    monkeypatch.setattr(managed_access, "visible_hosts",
                        lambda: {"hosts": [], "self": {"usage_reporting": "hidden"}})
    answer = client.post(f"{API}/usage/refresh")
    assert answer.status_code == 200 and answer.json()["usage_reporting"] == "hidden"
    assert usage_reporting.cached_parent() == "hidden"


def test_the_roster_a_leaf_pulls_carries_that_leafs_own_verdict(store, monkeypatch):
    """One field on the call its client already makes, which is what lets the
    poke be a nicety: a leaf that never hears a poke still learns, four times a
    minute, from the roster it was already asking for.
    """
    store.upsert_host("leaf-one", "Office", "10.66.0.9")
    store.set_host_usage_reporting("leaf-one", "available")
    monkeypatch.setattr(router, "_managed_leaf",
                        lambda device_id: dict(store.host_row("leaf-one")))

    class _Req:
        url = type("U", (), {"port": 9090})()

    answer = router.managed_hosts(_Req(), device_id="dev")
    assert answer["self"] == {"usage_reporting": "available"}
    # Its own row is not in the roster it is handed — it is in `self`, once.
    assert "leaf-one" not in [r["key"] for r in answer["hosts"]]
