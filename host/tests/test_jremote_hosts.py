"""The `hosts` registry — P3's third part (docs/multi-host-access.md).

A device learns that another machine EXISTS by mirroring this table, and asks
that machine for a credential separately. So what matters here is not that a row
round-trips. It is that the table stays one-way and key-free: only the host
writes it, a device cannot push one, nothing in it is a credential, and the key
is the machine's own id rather than one minted at this end. Plus the mirror
rules a synced table lives or dies by — every write stamps a seq, and forgetting
tombstones rather than deletes, because a vanished row is one every device past
its cursor keeps drawing forever.

Each test names the break it would catch.
"""

import pytest
from fastapi.testclient import TestClient

from jstack_host.server import create_app

app = create_app()
from jstack_host import devices, enrolment, grants, router, tunnel
from jstack_host.router import SyncPush
from jstack_host.store import SessionStore

LEAF_ENV = "WG_ADDR=10.66.0.7/32\nWG_SUBNET=10.66.0.0/24\nWG_HUB=10.66.0.1\n"
LEAF_BUNDLE = {name: "..." for name in tunnel.LEAF_FILES} | {"leaf.env": LEAF_ENV}

CLIENT_CONF = """[Interface]
PrivateKey = REDACTED
Address = 10.66.0.7/32

[Peer]
PublicKey = REDACTED
Endpoint = example.invalid:51820
"""


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = SessionStore(db_path=tmp_path / "hosts.sqlite")
    monkeypatch.setattr(devices, "_store", lambda: s)
    monkeypatch.setattr(enrolment, "_store", lambda: s)
    monkeypatch.setattr("jstack_host.store.get_store", lambda: s)
    return s


@pytest.fixture(autouse=True)
def _no_live_tunnel_and_no_alerts(monkeypatch, tmp_path):
    """This Mac ships wg_peer.py; an unguarded redemption would add a real peer
    to the live tunnel. The tests that need a peer stub `issue` themselves.
    HOME is faked too: forgetting a machine rewrites the hub's ~/.ssh/config."""
    monkeypatch.setattr(tunnel, "can_pair", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path / "test-home"))
    sent = []
    monkeypatch.setattr("jstack_host.hostenv.security_alert", sent.append)
    return sent


@pytest.fixture
def paired(monkeypatch):
    """A host that owns the mesh. A machine gets the leaf bundle, a device gets
    the client conf — the same split `issue` makes."""
    monkeypatch.setattr(tunnel, "can_pair", lambda: True)
    monkeypatch.setattr(
        tunnel, "issue",
        lambda d, leaf=False: {"device": d, "created": True} |
        ({"bundle": LEAF_BUNDLE} if leaf else {"config": CLIENT_CONF}))


def _mint(name="work-mac", kind=enrolment.KIND_HOST, created_by="", ttl=600):
    out = enrolment.mint_code(name, created_by, ttl, kind)
    return enrolment.normalize(out["code"]), out


# ── the table's own rules ────────────────────────────────────────────────────

def test_a_host_row_carries_no_column_a_credential_could_sit_in(store):
    """The enforcement is the schema, not a convention. If a token column ever
    appears here, a stolen mirror stops being a map and becomes a key."""
    store.upsert_host("host-key-aaaa", "Laptop", "10.66.0.7", 9090)
    columns = set(store.list_hosts()[0])
    assert columns == {"key", "name", "address", "port", "enrolled_at",
                       "deleted", "updated_at", "seq", "device_id", "sees_home",
                       # Public halves only: the pubkey is the machine's to
                       # show, the private key never left it (#131).
                       "sees_leaves", "shell_pubkey", "shell_user"}
    assert not any(c in columns for c in ("token", "token_hash", "secret",
                                          "password", "key_hash"))


def test_every_write_stamps_a_seq_or_no_device_ever_pulls_it(store):
    """A row changed without a seq bump sits behind every device's cursor
    forever — the change happened and nothing mirrors it."""
    before = store.current_seq()
    store.upsert_host("host-key-aaaa", "Laptop", "10.66.0.7")
    after_insert = store.current_seq()
    assert after_insert > before

    assert store.rename_host("host-key-aaaa", "Studio")
    after_rename = store.current_seq()
    assert after_rename > after_insert

    assert store.forget_host("host-key-aaaa")
    assert store.current_seq() > after_rename


def test_changes_since_carries_hosts_past_the_cursor_and_nothing_before(store):
    store.upsert_host("host-a", "A", "10.66.0.7")
    cursor = store.current_seq()
    store.upsert_host("host-b", "B", "10.66.0.8")

    delta = store.changes_since(cursor)
    assert [h["key"] for h in delta["hosts"]] == ["host-b"]
    assert [h["key"] for h in store.changes_since(0)["hosts"]] == ["host-a", "host-b"]
    assert store.changes_since(delta["seq"])["hosts"] == []


def test_forgetting_tombstones_rather_than_deletes(store):
    """A DELETE would be invisible to every device already past that seq: the
    row is gone here and drawn forever there. Only a tombstone travels."""
    store.upsert_host("host-a", "A", "10.66.0.7")
    cursor = store.current_seq()
    assert store.forget_host("host-a")

    assert store.list_hosts() == []                     # gone from the reader
    forgotten = store.list_hosts(include_forgotten=True)
    assert len(forgotten) == 1 and forgotten[0]["deleted"] == 1
    travelling = store.changes_since(cursor)["hosts"]   # and gone on every device
    assert len(travelling) == 1 and travelling[0]["deleted"] == 1


def test_forgetting_twice_is_not_an_error_the_second_time_is_a_no_op(store):
    store.upsert_host("host-a", "A", "10.66.0.7")
    assert store.forget_host("host-a") is True
    assert store.forget_host("host-a") is False
    assert store.forget_host("never-existed") is False


def test_renaming_refuses_a_forgotten_host(store):
    """A rename that resurrected a tombstone would put the tile back on every
    device without anybody granting access again."""
    store.upsert_host("host-a", "A", "10.66.0.7")
    store.forget_host("host-a")
    assert store.rename_host("host-a", "Back Again") is False
    assert store.list_hosts() == []


def test_re_enrolling_is_how_a_forgotten_host_returns(store):
    store.upsert_host("host-a", "A", "10.66.0.7")
    store.forget_host("host-a")
    row = store.upsert_host("host-a", "A Again", "10.66.0.9", 9091)
    assert row["deleted"] == 0
    assert row["name"] == "A Again" and row["address"] == "10.66.0.9"
    assert row["port"] == 9091
    assert [h["key"] for h in store.list_hosts()] == ["host-a"]


def test_a_device_cannot_push_a_host_row(store):
    """A device that could push one could invent a machine — and the tile a
    user taps to hand over credentials must name a machine this host let in."""
    assert "hosts" not in SyncPush.model_fields
    store.apply_push({"hosts": [{"key": "invented", "name": "Not Real",
                                 "address": "10.66.0.99"}]})
    assert store.list_hosts(include_forgotten=True) == []


# ── enrolment writes it ─────────────────────────────────────────────────────

def test_a_host_code_registers_the_machine_at_the_address_it_was_issued(
        store, paired):
    """The row has to carry a dialable address, and the only thing that knows
    it is the config the tunnel just wrote."""
    code, _ = _mint(name="Laptop")
    out = enrolment.redeem(code, "198.51.100.4", host_key="host-key-aaaa")

    assert out["kind"] == "host"
    assert out["host"]["key"] == "host-key-aaaa"
    assert out["host"]["address"] == "10.66.0.7"
    assert out["host"]["name"] == "Laptop"
    assert store.list_hosts()[0]["key"] == "host-key-aaaa"


def test_the_key_is_the_machines_own_never_one_minted_here(store, paired):
    """Local-first routing compares this against what the host on loopback
    answers for `/host`. A key minted at this end would let a device reach a
    Mac and be told it is somewhere else."""
    code, _ = _mint()
    out = enrolment.redeem(code, "198.51.100.4", host_key="claimed-by-the-machine")
    assert out["host"]["key"] == "claimed-by-the-machine"
    assert out["host"]["key"] != out["device"]["id"]


def test_a_device_code_writes_no_host_row_however_it_is_redeemed(store, paired):
    """The kind is fixed at mint. A redeemer that could declare itself a host
    would write its own tile into the grid the user reads."""
    code, _ = _mint(kind=enrolment.KIND_DEVICE)
    out = enrolment.redeem(code, "198.51.100.4", host_key="host-key-aaaa")
    assert out["kind"] == "device" and out["host"] is None
    assert store.list_hosts(include_forgotten=True) == []


def test_a_host_redeem_hands_back_this_hosts_grant_and_identity(
        store, paired, monkeypatch):
    """#62's parent half. Adopting a leaf establishes trust both ways: the leaf
    hands this host a grant (the direction that already worked), and this host
    hands the leaf one back — a credential to mint on THIS host — plus the id,
    name and mesh address the leaf needs to show its own devices a tile for it.
    Without both, a leaf's devices never reach the home they are a leaf of."""
    monkeypatch.setenv("JREMOTE_HOST_ID", "parent-host-0001")
    monkeypatch.setenv("JREMOTE_HOST_NAME", "Studio")
    monkeypatch.setattr(enrolment, "_own_mesh_address", lambda: "10.66.0.1")

    code, _ = _mint(name="Work Mac")
    out = enrolment.redeem(code, "198.51.100.4", host_key="host-key-aaaa")

    # A real grant, in the grant namespace, that this host now authenticates —
    # not the device token, which lives in a different namespace entirely.
    grant = out["leaf_grant"]
    assert grant.startswith("jrg1.") and grants.authenticate(grant)
    assert grant != out["token"]
    # This host, named and addressed, so the leaf has something to point at.
    assert out["parent_identity"] == {
        "key": "parent-host-0001", "name": "Studio",
        "address": "10.66.0.1", "port": 9090}


def test_a_device_redeem_carries_no_parent_grant_or_identity(store, paired):
    """A phone has no machine to mint back onto. The reverse-direction fields
    are a host-code thing; a device code gets empty ones, never a stray grant
    minted for a caller that has nowhere to spend it."""
    code, _ = _mint(kind=enrolment.KIND_DEVICE)
    out = enrolment.redeem(code, "198.51.100.4", host_key="host-key-aaaa")
    assert out["leaf_grant"] == "" and out["parent_identity"] == {}


def test_a_host_code_without_a_key_is_refused_and_stays_claimable(store):
    """Same blank refusal as an unknown code — naming this cause would tell a
    guesser the code was real — and unconsumed, so the machine it was minted
    for can still spend it."""
    code, _ = _mint()
    with pytest.raises(enrolment.EnrolmentError) as exc:
        enrolment.redeem(code, "198.51.100.4")
    assert str(exc.value) == enrolment.REFUSED
    assert store.enrolment_code(enrolment._hash(code))["used_at"] is None

    out = enrolment.redeem(code, "198.51.100.4", host_key="host-key-aaaa")
    assert out["host"]["key"] == "host-key-aaaa"


def test_a_malformed_key_is_refused_before_the_code_is_looked_at(store):
    """Its own exception, and it may be specific — it is decided from the
    request alone, so it cannot answer differently for a real code."""
    code, _ = _mint()
    for bad in ("", "   ", "short", "has space", "x" * 129, "!!!!!!!!"):
        with pytest.raises((enrolment.HostKeyRefused, enrolment.EnrolmentError)):
            enrolment.redeem(code, "198.51.100.4", host_key=bad)
    # a real code survives all of it
    assert store.enrolment_code(enrolment._hash(code))["used_at"] is None
    # and an unknown code answers the SAME 400, which is what proves the order
    with pytest.raises(enrolment.HostKeyRefused):
        enrolment.redeem("22222222", "198.51.100.4", host_key="nope")


def test_a_port_outside_the_range_is_refused(store):
    code, _ = _mint()
    for bad in (0, -1, 65536, "nonsense"):
        with pytest.raises(enrolment.HostKeyRefused):
            enrolment.redeem(code, "198.51.100.4", host_key="host-key-aaaa",
                             port=bad)
    assert store.enrolment_code(enrolment._hash(code))["used_at"] is None


def test_a_host_with_no_mesh_is_still_recorded_as_a_machine_we_let_in(store):
    """The code is already spent by this point. Dropping the row over a tunnel
    half the hosts running this package do not even have would lose the only
    record of the enrolment."""
    code, _ = _mint(name="Leaf Mac")
    out = enrolment.redeem(code, "198.51.100.4", host_key="host-key-aaaa")
    assert out["tunnel"] is None
    assert out["host"]["address"] == ""
    assert store.list_hosts()[0]["name"] == "Leaf Mac"


def test_mesh_address_reads_whichever_artefact_it_was_handed():
    """A client conf says `Address =`; a leaf's env says `WG_ADDR=`, because
    `wg setconf` rejects an Address line. One of them blanking the registry is
    the failure this covers."""
    assert enrolment.mesh_address({"config": CLIENT_CONF}) == "10.66.0.7"
    assert enrolment.mesh_address({"bundle": LEAF_BUNDLE}) == "10.66.0.7"
    assert enrolment.mesh_address({"config": "[Interface]\nPrivateKey = x\n"}) == ""
    assert enrolment.mesh_address(None) == ""


def test_a_machine_is_paired_as_a_leaf_and_a_device_as_a_client(store, monkeypatch):
    """They come off the same peer entry and are NOT interchangeable: the
    client conf is wg-quick syntax a machine's `wg setconf` refuses, so handing
    a machine one looks like a pairing and never comes up."""
    seen = []
    monkeypatch.setattr(tunnel, "can_pair", lambda: True)
    monkeypatch.setattr(
        tunnel, "issue",
        lambda d, leaf=False: seen.append(leaf) or
        {"device": d, "created": True} |
        ({"bundle": LEAF_BUNDLE} if leaf else {"config": CLIENT_CONF}))

    enrolment.redeem(_mint(kind=enrolment.KIND_HOST)[0], "198.51.100.4",
                     host_key="host-key-aaaa")
    enrolment.redeem(_mint(name="phone", kind=enrolment.KIND_DEVICE)[0],
                     "198.51.100.4")
    assert seen == [True, False]


# ── the endpoints ────────────────────────────────────────────────────────────

@pytest.fixture
def client(store):
    _, token = devices.mint("test-device")
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


@pytest.fixture
def on_console(monkeypatch):
    """Act as the hub's own menu bar. Minting an enrolment code (adopting a Mac /
    adding a device) is hub-console-only now; TestClient's address is not
    loopback, so without this the mint answers 403. The /hosts registry routes
    are not gated — those are the leaf tiles, not device management."""
    monkeypatch.setattr(router, "_hub_console", lambda request: True)
    monkeypatch.setattr("jstack_host.managed_access.console", lambda request: True)


def test_the_whole_trip_over_http(client, store, paired, on_console):
    minted = client.post("/api/jremote/v1/enrolment/codes",
                         json={"name": "Laptop", "kind": "host"})
    assert minted.status_code == 200 and minted.json()["kind"] == "host"

    anon = TestClient(app)
    redeemed = anon.post("/api/jremote/v1/enrolment/redeem",
                         json={"code": minted.json()["code"],
                               "host_key": "host-key-aaaa", "port": 9091})
    assert redeemed.status_code == 200
    assert redeemed.json()["host"]["address"] == "10.66.0.7"

    listed = client.get("/api/jremote/v1/hosts").json()["hosts"]
    assert [h["key"] for h in listed] == ["host-key-aaaa"]
    assert listed[0]["port"] == 9091 and listed[0]["deleted"] is False

    assert client.post("/api/jremote/v1/hosts/host-key-aaaa/rename",
                       json={"name": "Studio"}).status_code == 200
    assert client.get("/api/jremote/v1/hosts").json()["hosts"][0]["name"] == "Studio"

    assert client.post("/api/jremote/v1/hosts/host-key-aaaa/forget").status_code == 200
    assert client.get("/api/jremote/v1/hosts").json()["hosts"] == []


def test_redeeming_over_http_records_the_shell_identity(client, store, paired, on_console):
    """The route model must carry the shell halves through to `redeem` — a
    dropped field here loses shell access silently while the attach succeeds,
    which is exactly what the first live shell_adopt run caught."""
    minted = client.post("/api/jremote/v1/enrolment/codes",
                         json={"name": "Laptop", "kind": "host"})
    anon = TestClient(app)
    redeemed = anon.post("/api/jremote/v1/enrolment/redeem",
                         json={"code": minted.json()["code"],
                               "host_key": "host-key-aaaa", "port": 9091,
                               "ssh_pubkey": "ssh-ed25519 AAAAexampleA host-key-aaaa",
                               "ssh_user": "admin"})
    assert redeemed.status_code == 200
    assert "shell" in redeemed.json()
    row = store.host_row("host-key-aaaa")
    assert row["shell_pubkey"] == "ssh-ed25519 AAAAexampleA host-key-aaaa"
    assert row["shell_user"] == "admin"


def test_the_host_registry_is_behind_the_token(client, store):
    """A tile naming every machine the user owns is a map of the estate."""
    anon = TestClient(app)
    assert anon.get("/api/jremote/v1/hosts").status_code == 401
    assert anon.post("/api/jremote/v1/hosts/x/forget").status_code == 401
    assert anon.post("/api/jremote/v1/hosts/x/rename",
                     json={"name": "y"}).status_code == 401


def test_a_machine_forgets_itself_and_only_itself_off_the_console(store):
    """Detach's `_tell_parent` posts the forget from the mesh, not loopback:
    the machine's own credential must be able to end its adoption, and must
    not be able to end anybody else's."""
    row, token = devices.mint("Update lab leaf")
    store.upsert_host("host-key-aaaa", "Laptop", "10.66.0.7", 9090)
    store.bind_host_device("host-key-aaaa", row["id"])
    store.upsert_host("host-key-bbbb", "Other", "10.66.0.8", 9090)
    leaf = TestClient(app)
    leaf.headers.update({"Authorization": f"Bearer {token}"})
    assert leaf.post("/api/jremote/v1/hosts/host-key-bbbb/forget").status_code == 403
    answer = leaf.post("/api/jremote/v1/hosts/host-key-aaaa/forget")
    assert answer.status_code == 200
    assert store.host_row("host-key-aaaa")["deleted"]
    # The credential goes with the tile: the machine could not revoke it
    # afterwards (the tombstone ends its reach, and a machine credential may
    # not disconnect), and alive it would still open the hub's /managed/ routes.
    assert answer.json()["credential_revoked"] == row["id"]
    assert store.device(row["id"])["revoked_at"] is not None
    assert leaf.post("/api/jremote/v1/hosts/host-key-aaaa/forget").status_code == 401


def test_the_console_forgetting_a_machine_keeps_its_own_credential(client, store, on_console):
    store.upsert_host("host-key-aaaa", "Laptop", "10.66.0.7", 9090)
    answer = client.post("/api/jremote/v1/hosts/host-key-aaaa/forget")
    assert answer.status_code == 200
    assert answer.json()["credential_revoked"] == ""
    assert client.get("/api/jremote/v1/host").status_code == 200


def test_managing_a_host_that_is_not_there_is_a_404(client, store, on_console):
    assert client.post("/api/jremote/v1/hosts/ghost/rename",
                       json={"name": "y"}).status_code == 404
    assert client.post("/api/jremote/v1/hosts/ghost/forget").status_code == 404


def test_an_unknown_kind_is_refused_at_mint(client, store, on_console):
    r = client.post("/api/jremote/v1/enrolment/codes",
                    json={"name": "x", "kind": "superuser"})
    assert r.status_code == 400


def test_a_malformed_key_over_http_is_a_400_not_a_401(client, store, on_console):
    """400 is the one specific answer this endpoint gives, and it is safe
    because it is decided before the code is looked up."""
    code = client.post("/api/jremote/v1/enrolment/codes",
                       json={"name": "x", "kind": "host"}).json()["code"]
    anon = TestClient(app)
    r = anon.post("/api/jremote/v1/enrolment/redeem",
                  json={"code": code, "host_key": "no"})
    assert r.status_code == 400
    assert "host key" in r.json()["detail"]
