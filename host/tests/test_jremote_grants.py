"""Delegated minting — the two ends of it, and the gate between them.

The thing under test is a promise, not a function: a device paired to a hub gets
into a machine the hub adopted without pairing to that machine. So the tests are
written as the two halves of that sentence — what the leaf will accept, and what
the hub will do on a device's behalf — plus the refusals that keep a grant from
being anything more than a mint button.
"""

import pytest
from fastapi.testclient import TestClient

from jstack_host import devices, grants, store


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def paired():
    """A live device row on this host, and its token — the caller that has
    already proved itself to the hub."""
    row, token = devices.mint("a-phone")
    return row, token


# ── the leaf's end: issuing, verifying, refusing ──

def test_a_grant_authenticates_and_names_the_parent_it_was_issued_to():
    token = grants.issue("http://studio.local:9090")
    assert grants.authenticate(token) == "http://studio.local:9090"


def test_a_device_token_is_not_a_grant_and_a_grant_is_not_a_device_token(paired):
    """The two namespaces are the enforcement. A `jr1.` presented to the grant
    gate must not parse, or a device could mint on a parent's behalf; a `jrg1.`
    presented to the bearer gate must not authenticate, or a grant would be a
    full API credential."""
    _, device_token = paired
    grant = grants.issue("parent")
    assert grants.authenticate(device_token) is None
    assert devices.authenticate(grant) is None


def test_a_revoked_grant_stops_authenticating():
    token = grants.issue("parent")
    assert grants.revoke_issued() == 1
    assert grants.authenticate(token) is None


def test_revoking_one_parents_grants_leaves_anothers_alone():
    a = grants.issue("parent-a")
    b = grants.issue("parent-b")
    assert grants.revoke_issued("parent-a") == 1
    assert grants.authenticate(a) is None
    assert grants.authenticate(b) == "parent-b"


def test_a_host_that_never_attached_authenticates_nobody():
    """No grandfathering, ever. `devices.authenticate` folds a token file into
    the table on an empty store; a machine with no grants must simply refuse."""
    assert grants.authenticate("jrg1.abc.whatever") is None


# ── the route the grant opens, and the ones it does not ──

def test_the_mint_route_mints_an_ordinary_device_row(client):
    grant = grants.issue("http://studio.local:9090")
    resp = client.post("/api/jremote/v1/delegate/mint", json={"name": "a-phone"},
                       headers={"Authorization": f"Bearer {grant}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["token"].startswith("jr1.")
    assert body["device"]["name"] == "a-phone"
    # Ordinary: it is in the roster, and it drives the API like any other.
    assert devices.authenticate(body["token"]) == body["device"]["id"]
    assert any(d["id"] == body["device"]["id"] for d in devices.list_all())
    # …but the digest does not cross to the parent. It is in the roster this
    # machine shows its own owner; it has no business on another machine.
    assert "token_hash" not in body["device"]


def test_the_mint_route_refuses_a_device_token(client, paired):
    """The gate is the grant. A phone that already has a credential here must
    not be able to hand itself another one through this door — that is exactly
    the unbounded supply `devices.mint_allowed_from` exists to prevent."""
    _, device_token = paired
    resp = client.post("/api/jremote/v1/delegate/mint", json={"name": "x"},
                       headers={"Authorization": f"Bearer {device_token}"})
    assert resp.status_code == 401


def test_the_mint_route_refuses_a_revoked_grant(client):
    grant = grants.issue("parent")
    grants.revoke_issued()
    resp = client.post("/api/jremote/v1/delegate/mint", json={"name": "x"},
                       headers={"Authorization": f"Bearer {grant}"})
    assert resp.status_code == 401


def test_a_grant_opens_that_route_and_nothing_else(client):
    """The narrowness, pinned. If a future route forgets which gate it is on,
    this is what fails."""
    grant = grants.issue("parent")
    auth = {"Authorization": f"Bearer {grant}"}
    for path in ("/api/jremote/v1/devices", "/api/jremote/v1/agents",
                 "/api/jremote/v1/hosts", "/api/jremote/v1/sessions"):
        assert client.get(path, headers=auth).status_code == 401, path


def test_minting_stamps_the_grant_so_an_abused_one_is_visible(client):
    grant = grants.issue("parent")
    for _ in range(3):
        client.post("/api/jremote/v1/delegate/mint", json={"name": "d"},
                    headers={"Authorization": f"Bearer {grant}"})
    row = grants.issued()[0]
    assert row["minted"] == 3
    assert row["last_used_at"]


# ── the hub's end: holding a grant and spending it for a device ──

def _adopted(key="leaf-mac-01", address="10.66.0.7", port=9090):
    """A machine in the registry, with a grant held for it — the state attach
    leaves behind."""
    row = store.get_store().upsert_host(key, "Work Mac", address, port)
    grants.remember(key, "jrg1.deadbeef.secret", "http://hub:9090")
    return row


def test_a_device_asks_the_hub_and_gets_a_token_minted_on_the_leaf(client, paired):
    _adopted()
    _, device_token = paired
    sent = {}

    def poster(url, payload, token):
        sent.update(url=url, payload=payload, token=token)
        return 200, {"device": {"id": "abc123", "name": payload["name"]},
                     "token": "jr1.abc123.minted-over-there"}

    import jstack_host.grants as g
    g_orig = g._httpx_post
    g._httpx_post = poster
    try:
        resp = client.post("/api/jremote/v1/hosts/leaf-mac-01/grant", json={},
                           headers={"Authorization": f"Bearer {device_token}"})
    finally:
        g._httpx_post = g_orig

    assert resp.status_code == 200
    body = resp.json()
    # The device gets a credential minted by the leaf, and the facts to reach it.
    assert body["token"] == "jr1.abc123.minted-over-there"
    assert body["address"] == "10.66.0.7"
    assert body["port"] == 9090
    # The hub presented its grant, to the address in ITS registry — never one
    # the caller supplied.
    assert sent["token"] == "jrg1.deadbeef.secret"
    assert sent["url"] == "http://10.66.0.7:9090/api/jremote/v1/delegate/mint"
    # The row on the leaf is named after the device that asked, so revoking it
    # there is legible.
    assert sent["payload"]["name"] == "a-phone"


def test_asking_for_a_machine_this_hub_never_adopted_is_404(client, paired):
    _, device_token = paired
    resp = client.post("/api/jremote/v1/hosts/nobody/grant", json={},
                       headers={"Authorization": f"Bearer {device_token}"})
    assert resp.status_code == 404


def test_asking_for_a_forgotten_machine_is_404_not_a_reachable_one(client, paired):
    """A forgotten tile is invisible to `list_hosts`; it must be invisible here
    too, or a device could reach a machine it was never shown."""
    _adopted()
    store.get_store().forget_host("leaf-mac-01")
    _, device_token = paired
    resp = client.post("/api/jremote/v1/hosts/leaf-mac-01/grant", json={},
                       headers={"Authorization": f"Bearer {device_token}"})
    assert resp.status_code == 404


def test_a_machine_with_a_tile_but_no_grant_says_so_instead_of_failing_blankly(
        client, paired):
    """The state a leaf on an older build leaves: on the mesh, in the grid, not
    delegating. The 502 has to name the fix, because the tile looks identical to
    one that works."""
    store.get_store().upsert_host("old-leaf", "Old Mac", "10.66.0.9", 9090)
    _, device_token = paired
    resp = client.post("/api/jremote/v1/hosts/old-leaf/grant", json={},
                       headers={"Authorization": f"Bearer {device_token}"})
    assert resp.status_code == 502
    assert "re-attach" in resp.json()["detail"].lower()


def test_a_machine_with_no_address_is_refused_before_a_request_is_made(client,
                                                                      paired):
    """Enrolled without a mesh peer — a real state (`enrolment._register_host`
    writes the row whether or not the tunnel issued one). There is nothing to
    dial, and inventing an address would be worse than saying so."""
    store.get_store().upsert_host("no-route", "Stranded", "", 9090)
    grants.remember("no-route", "jrg1.x.y")
    _, device_token = paired
    resp = client.post("/api/jremote/v1/hosts/no-route/grant", json={},
                       headers={"Authorization": f"Bearer {device_token}"})
    assert resp.status_code == 502
    assert "no address" in resp.json()["detail"].lower()


def test_a_leaf_that_revoked_the_grant_is_reported_as_that_not_as_broken(
        client, paired):
    _adopted()
    _, device_token = paired

    def poster(url, payload, token):
        return 401, {"detail": "invalid or missing grant"}

    import jstack_host.grants as g
    g_orig = g._httpx_post
    g._httpx_post = poster
    try:
        resp = client.post("/api/jremote/v1/hosts/leaf-mac-01/grant", json={},
                           headers={"Authorization": f"Bearer {device_token}"})
    finally:
        g._httpx_post = g_orig
    assert resp.status_code == 502
    assert "revoked" in resp.json()["detail"].lower()


def test_forgetting_a_machine_drops_the_grant_with_the_tile(client, paired):
    _adopted()
    _, device_token = paired
    resp = client.post("/api/jremote/v1/hosts/leaf-mac-01/forget", json={},
                       headers={"Authorization": f"Bearer {device_token}"})
    assert resp.status_code == 200
    assert resp.json()["grant_dropped"] is True
    assert grants.held("leaf-mac-01") == ""


def test_the_grid_says_which_machines_a_device_can_actually_get_into(client,
                                                                    paired):
    """The tile and the access are two different facts, and the row that looks
    fine is the one that lies: a machine enrolled by an older build shows on
    every device and 502s the moment somebody taps it. `delegated` is that
    difference, on the row itself."""
    _adopted()
    store.get_store().upsert_host("old-leaf", "Old Mac", "10.66.0.9", 9090)
    _, device_token = paired
    resp = client.get("/api/jremote/v1/hosts",
                      headers={"Authorization": f"Bearer {device_token}"})
    assert resp.status_code == 200
    by_key = {h["key"]: h for h in resp.json()["hosts"]}
    assert by_key["leaf-mac-01"]["delegated"] is True
    assert by_key["old-leaf"]["delegated"] is False
    # And it is a computed answer, never a column: `host_grants` does not sync,
    # so nothing here may look like something a device could push back.
    assert "token" not in by_key["leaf-mac-01"]


def test_a_revoked_grant_stops_the_grid_claiming_access(client, paired):
    _adopted()
    grants.forget("leaf-mac-01")
    store.get_store().upsert_host("leaf-mac-01", "Work Mac", "10.66.0.7", 9090)
    _, device_token = paired
    resp = client.get("/api/jremote/v1/hosts",
                      headers={"Authorization": f"Bearer {device_token}"})
    assert resp.json()["hosts"][0]["delegated"] is False


def test_the_grant_roster_never_carries_the_tokens():
    """A listing that carries credentials is one log line from leaking them."""
    _adopted()
    for row in grants.holdings():
        assert "token" not in row


# ── the leaf's OWN devices reaching the parent it is a leaf of (#62) ──
#
# The mirror of everything above. A hub lists the machines it adopted and mints
# on them for a device; a LEAF wants the same reach to the one machine that
# adopted IT. That machine is in no `hosts` table — the leaf did not adopt its
# parent — so the router synthesises its tile from the attach record and the
# grant the leaf holds, and only while the grant is live. These pin that the
# synthesis is a real delegated row a device can spend, and that a machine with
# no parent invents nothing.

def _leaf_of(monkeypatch, key="parent-host-0001", address="10.66.0.1",
             port=9090, held="jrg1.parentgrant.secret"):
    """The state `attach` leaves on a leaf: a grant held for the parent, and a
    parent record naming it. `parent_record` is patched rather than a real file
    written, so the test never reads the developer machine's own parent.json."""
    grants.remember(key, held, "http://home:9090")
    monkeypatch.setattr(
        "jstack_host.attach_parent.parent_record",
        lambda: {"parent_key": key, "parent_name": "Home",
                 "parent_address": address, "parent_port": port})


def test_a_leaf_shows_its_parent_as_a_delegated_tile(client, paired, monkeypatch):
    _leaf_of(monkeypatch)
    _, device_token = paired
    resp = client.get("/api/jremote/v1/hosts",
                      headers={"Authorization": f"Bearer {device_token}"})
    parent = next(h for h in resp.json()["hosts"] if h["key"] == "parent-host-0001")
    # Ahead of any adopted machine, delegated because the grant backs it, and
    # named/addressed from the record — a row a device can tile and then spend.
    assert parent["delegated"] is True
    assert parent["name"] == "Home" and parent["address"] == "10.66.0.1"
    assert "token" not in parent


def test_a_leafs_device_mints_on_its_parent_the_reverse_of_a_hub(
        client, paired, monkeypatch):
    """The #62 close. The same delegated mint a hub does for a leaf, a leaf does
    for its parent: the caller proves a token here, and the leaf spends the
    grant it holds against the parent's own `/delegate/mint`."""
    _leaf_of(monkeypatch)
    _, device_token = paired
    sent = {}

    def poster(url, payload, token):
        sent.update(url=url, token=token, name=payload["name"])
        return 200, {"device": {"id": "z", "name": payload["name"]},
                     "token": "jr1.z.minted-at-home"}

    import jstack_host.grants as g
    g_orig = g._httpx_post
    g._httpx_post = poster
    try:
        resp = client.post("/api/jremote/v1/hosts/parent-host-0001/grant",
                           json={},
                           headers={"Authorization": f"Bearer {device_token}"})
    finally:
        g._httpx_post = g_orig

    assert resp.status_code == 200
    assert resp.json()["token"] == "jr1.z.minted-at-home"
    # The grant the leaf holds, spent against the parent's mesh address — the
    # one in the record, never one the caller could name.
    assert sent["token"] == "jrg1.parentgrant.secret"
    assert sent["url"] == "http://10.66.0.1:9090/api/jremote/v1/delegate/mint"


def test_a_parent_whose_grant_was_revoked_shows_no_tile(client, paired,
                                                        monkeypatch):
    """A parent record with a dead grant is not a machine to reach — the tile
    exists only while the grant does, so a revoked one is silence, not a row
    that 502s on the first tap."""
    _leaf_of(monkeypatch)
    grants.forget("parent-host-0001")
    _, device_token = paired
    resp = client.get("/api/jremote/v1/hosts",
                      headers={"Authorization": f"Bearer {device_token}"})
    assert resp.json()["hosts"] == []
    grant = client.post("/api/jremote/v1/hosts/parent-host-0001/grant", json={},
                        headers={"Authorization": f"Bearer {device_token}"})
    assert grant.status_code == 404


def test_a_machine_that_is_not_a_leaf_shows_no_parent_tile(client, paired,
                                                           monkeypatch):
    """A hub has no parent record; the synthesis must be silent there, never a
    phantom row invented from an empty one."""
    monkeypatch.setattr("jstack_host.attach_parent.parent_record", lambda: {})
    _, device_token = paired
    resp = client.get("/api/jremote/v1/hosts",
                      headers={"Authorization": f"Bearer {device_token}"})
    assert resp.json()["hosts"] == []
