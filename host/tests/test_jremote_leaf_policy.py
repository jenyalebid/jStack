"""The leaf contract: Standard and Headless, home's word, the leaf's cache.

Each test names the break it would catch. jStack-Project docs/leaf.md.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from jstack_host import devices, leaf_policy, managed_access, usage_reporting
from jstack_host.store import SessionStore

API = "/api/jremote/v1"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_reporting, "_STATE", tmp_path / "usage.json")
    monkeypatch.setattr(leaf_policy, "_STATE", tmp_path / "leaf.json")
    monkeypatch.setattr(managed_access, "is_leaf", lambda: False)


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = SessionStore(db_path=tmp_path / "leaf.sqlite")
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
def on_console(monkeypatch):
    monkeypatch.setattr(managed_access, "console", lambda request: True)
    monkeypatch.setattr(leaf_policy, "poke",
                        lambda key, poster=None: {"step": f"leaf-refresh:{key}", "ok": False})


def _adopt(store, key="bench"):
    store.upsert_host(key=key, name=key.title(), address="10.66.0.9", port=9090)
    return key


# ── resolve: the one place Headless applies ──────────────────────────────────

def test_headless_sees_nothing_and_hides_usage_whatever_the_switches_say():
    """Headless resolving through the Standard switches would hand a shared Mac
    Home the moment someone had left Sees Home on."""
    row = {"mode": "headless", "sees_home": 1, "sees_leaves": 1,
           "usage_reporting": "available", "agents_tab": 0}
    got = leaf_policy.resolve(row)
    assert got["sees_home"] is False and got["sees_leaves"] is False
    assert got["usage_reporting"] == "hidden"
    assert got["agents_tab"] is False


def test_standard_follows_its_switches_and_always_shows_agents():
    got = leaf_policy.resolve({"mode": "standard", "sees_home": 0, "sees_leaves": 1,
                               "usage_reporting": "client", "agents_tab": 0})
    assert (got["sees_home"], got["sees_leaves"], got["agents_tab"]) == (False, True, True)


def test_an_unknown_mode_is_standard():
    """A row from a build that never heard of modes is a Standard leaf."""
    assert leaf_policy.resolve({})["mode"] == "standard"


def test_headless_keeps_the_standard_switches_for_the_trip_back():
    row = {"mode": "headless", "sees_home": 0, "sees_leaves": 1, "usage_reporting": "available"}
    view = leaf_policy.console_view(row)
    assert (view["sees_home"], view["sees_leaves"], view["usage_reporting"]) == \
        (False, True, "available")


@pytest.mark.parametrize("fields", [{"mode": "locked"}, {"agents_tab": 1},
                                    {"line": "feature-x"}, {"usage_reporting": "on"}])
def test_bad_values_are_refused_not_stored(fields):
    with pytest.raises(leaf_policy.PolicyError):
        leaf_policy.normalise(fields)


# ── home: the policy route ──────────────────────────────────────────────────

def test_policy_writes_the_row_and_reports_the_poke(client, store, on_console):
    key = _adopt(store)
    r = client.post(f"{API}/hosts/{key}/policy", json={"mode": "headless", "agents_tab": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "headless" and body["agents_tab"] is False
    assert body["steps"][0]["step"] == f"leaf-refresh:{key}"
    assert store.host_row(key)["mode"] == "headless"


def test_policy_is_console_only(client, store):
    key = _adopt(store)
    assert client.post(f"{API}/hosts/{key}/policy", json={"mode": "headless"}).status_code == 403


def test_policy_for_an_unknown_machine_is_404(client, store, on_console):
    assert client.post(f"{API}/hosts/nobody/policy", json={"mode": "headless"}).status_code == 404


def test_console_roster_carries_the_stored_policy(client, store, on_console):
    key = _adopt(store)
    client.post(f"{API}/hosts/{key}/policy", json={"mode": "headless", "line": "main"})
    row = next(h for h in client.get(f"{API}/hosts").json()["hosts"] if h["key"] == key)
    assert row["mode"] == "headless" and row["line"] == "main" and row["sees_home"] is True


def test_a_device_roster_never_carries_policy(client, store):
    _adopt(store)
    for row in client.get(f"{API}/hosts").json()["hosts"]:
        assert not {"mode", "agents_tab", "line", "sees_home"} & row.keys()


# ── the leaf: what it is handed and what it keeps ───────────────────────────

def test_headless_leaf_is_handed_an_empty_roster_and_hidden_usage(store, monkeypatch):
    """The reach check, not the client, is what keeps a shared Mac to itself."""
    key = _adopt(store)
    store.set_host_policy(key, {"mode": "headless"})
    leaf = store.host_row(key)
    monkeypatch.setattr(managed_access, "leaf_for_device", lambda d: leaf)
    monkeypatch.setattr(devices, "row", lambda d: {"revoked_at": None})
    from jstack_host import hostenv
    assert managed_access.may_reach("leaf-cred", hostenv.host_id()) is False
    assert managed_access.may_reach("leaf-cred", "other-leaf") is False
    assert managed_access.may_reach("leaf-cred", key) is True


def test_leaf_caches_mode_and_agents_tab_from_the_pull():
    leaf_policy.note_parent({"self": {"mode": "headless", "agents_tab": False,
                                      "usage_reporting": "hidden"}})
    assert leaf_policy.cached() == {"mode": "headless", "agents_tab": False}
    assert usage_reporting.cached_parent() == "hidden"


def test_a_silent_hub_is_standard_with_agents():
    """An old hub says nothing; a cached Headless must not outlive it."""
    leaf_policy.note_parent({"self": {"mode": "headless"}})
    leaf_policy.note_parent({})
    assert leaf_policy.cached() == {"mode": "standard", "agents_tab": True}


def test_homes_line_lands_in_the_leafs_updater(tmp_path, monkeypatch):
    from jstack_host import hostenv
    monkeypatch.setattr(hostenv, "state_dir", lambda: tmp_path)
    (tmp_path / "updates").mkdir()
    (tmp_path / "updates" / "config.json").write_text(json.dumps({"channel": "dev"}))
    leaf_policy.note_parent({"self": {"line": "main"}})
    assert json.loads((tmp_path / "updates" / "config.json").read_text())["channel"] == "main"


def test_headless_leaf_refuses_its_own_line(monkeypatch):
    monkeypatch.setattr(managed_access, "is_leaf", lambda: True)
    leaf_policy.note_parent({"self": {"mode": "headless"}})
    with pytest.raises(leaf_policy.LocalLineRefused):
        leaf_policy.refuse_local_line()


def test_standard_leaf_may_switch_its_own_line(monkeypatch):
    monkeypatch.setattr(managed_access, "is_leaf", lambda: True)
    leaf_policy.note_parent({"self": {"mode": "standard"}})
    leaf_policy.refuse_local_line()


def test_host_answers_the_leaf_block_only_on_a_leaf(monkeypatch):
    assert leaf_policy.host_block() is None
    monkeypatch.setattr(managed_access, "is_leaf", lambda: True)
    assert leaf_policy.host_block() == {"mode": "standard", "agents_tab": True}
