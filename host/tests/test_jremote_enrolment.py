"""One-time enrolment codes — the P3 contract (docs/multi-host-access.md).

What matters here is not that a code round-trips. It is that the four
properties standing in for the source-address gate actually hold: single-use
survives a race, expiry is enforced where it is redeemed, the refusal is one
answer for every cause so the endpoint is not an oracle, and guessing is
metered by the same limiter as bearer auth. Plus the two that make the widening
survivable — every enrolment is attributable, and revoking the minting device
kills the codes it left outstanding.

Each test names the break it would catch.
"""

import time

import pytest
from fastapi.testclient import TestClient

from jstack_host.server import create_app

app = create_app()
from jstack_host import auth, devices, enrolment, tunnel
from jstack_host.store import SessionStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = SessionStore(db_path=tmp_path / "enrolment.sqlite")
    monkeypatch.setattr(devices, "_store", lambda: s)
    monkeypatch.setattr(enrolment, "_store", lambda: s)
    return s


@pytest.fixture(autouse=True)
def _no_live_tunnel_and_no_alerts(monkeypatch):
    """This Mac ships wg_peer.py, so an unguarded redemption test would add a
    real peer to the live tunnel config. Off by default; the pairing tests
    stub `issue` explicitly. Alerts are captured rather than printed so the
    announce path is asserted instead of merely tolerated."""
    monkeypatch.setattr(tunnel, "can_pair", lambda: False)
    sent = []
    monkeypatch.setattr("jstack_host.hostenv.security_alert", sent.append)
    return sent


def _mint(store, name="work-mac", created_by="", ttl=600):
    """A code, minted through the module — returns its canonical form."""
    out = enrolment.mint_code(name, created_by, ttl)
    return enrolment.normalize(out["code"]), out


# ── the code itself ──────────────────────────────────────────────────────────

def test_a_minted_code_is_grouped_and_carries_no_lookalike_characters(store):
    _, out = _mint(store)
    assert out["code"][4] == "-" and len(out["code"]) == 9
    body = out["code"].replace("-", "")
    assert len(body) == enrolment.CODE_LEN
    assert not (set(body) & set("01IO")), "a code read aloud must not be ambiguous"


def test_the_table_stores_a_hash_never_the_code(store):
    code, out = _mint(store)
    rows = store.list_enrolment_codes()
    assert code not in str(rows) and out["code"] not in str(rows)
    assert rows[0]["code_hash"].startswith("sha256:")


def test_normalize_treats_case_and_grouping_as_presentation(store):
    code, out = _mint(store)
    typed = out["code"].lower().replace("-", " ")
    assert enrolment.normalize(typed) == code
    assert enrolment.normalize(out["code"]) == code


def test_normalize_refuses_anything_that_is_not_a_whole_code():
    """A dropped ambiguous character shortens the string, and a short string
    must fail loudly rather than hash to something that might match."""
    assert enrolment.normalize("MFQ4-7K2") == ""       # seven
    assert enrolment.normalize("MFQ4-7K2PP") == ""     # nine
    assert enrolment.normalize("MFQO-47K2") == ""      # O dropped → seven
    assert enrolment.normalize("") == ""


def test_ttl_is_clamped_to_the_declared_window(store):
    assert _mint(store, ttl=5)[1]["expires_in"] == enrolment.MIN_TTL
    assert _mint(store, ttl=99999)[1]["expires_in"] == enrolment.MAX_TTL
    assert _mint(store, ttl="nonsense")[1]["expires_in"] == enrolment.DEFAULT_TTL


# ── redemption ───────────────────────────────────────────────────────────────

def test_redeeming_yields_a_working_token_named_by_the_code(store):
    code, _ = _mint(store, name="My Laptop")
    out = enrolment.redeem(code, "198.51.100.4")
    assert devices.authenticate(out["token"]) == out["device"]["id"]
    assert out["device"]["name"] == "My Laptop"
    assert out["token"] not in str(store.list_devices())


def test_a_code_is_spent_exactly_once(store):
    """The whole anti-farming property. A second redemption must never mint."""
    code, _ = _mint(store)
    enrolment.redeem(code, "198.51.100.4")
    with pytest.raises(enrolment.EnrolmentError):
        enrolment.redeem(code, "198.51.100.5")
    assert len(store.list_devices()) == 1


def test_an_expired_code_is_refused_even_though_it_is_still_in_the_table(store):
    """Expiry is enforced at redemption, not by the sweep — a sweep that has
    not run yet must not become a window in which a stale code still works."""
    code = "MFQ47K2P"
    store.add_enrolment_code(enrolment._hash(code), "late", int(time.time()) - 1, "")
    with pytest.raises(enrolment.EnrolmentError):
        enrolment.redeem(code, "198.51.100.4")


def test_every_bad_code_gets_the_same_answer(store):
    """Unknown, expired and already-used must be indistinguishable, or the
    endpoint tells a guesser which of its tries was structurally right."""
    used, _ = _mint(store)
    enrolment.redeem(used, "198.51.100.4")
    expired = "MFQ47K2P"
    store.add_enrolment_code(enrolment._hash(expired), "late",
                             int(time.time()) - 1, "")
    answers = set()
    for code in (used, expired, "22222222", "not-a-code"):
        with pytest.raises(enrolment.EnrolmentError) as e:
            enrolment.redeem(code, "198.51.100.4")
        answers.add(str(e.value))
    assert answers == {enrolment.REFUSED}


def test_the_row_records_which_device_the_code_let_in(store):
    """The audit trail that makes the widening survivable — an enrolment with
    no device id on it cannot be traced back from the registry."""
    code, _ = _mint(store)
    out = enrolment.redeem(code, "198.51.100.4")
    row = store.enrolment_code(enrolment._hash(code))
    assert row["used_at"] is not None
    assert out["device"]["id"] in row["used_by"]
    assert "198.51.100.4" in row["used_by"]


def test_an_enrolment_announces_itself(store, _no_live_tunnel_and_no_alerts):
    """A credential minted from off the LAN is the event the address gate used
    to make impossible. It must not be silent."""
    code, _ = _mint(store, name="work-mac")
    enrolment.redeem(code, "198.51.100.4")
    for _ in range(50):                       # _announce is threaded
        if _no_live_tunnel_and_no_alerts:
            break
        time.sleep(0.02)
    assert any("work-mac" in b and "198.51.100.4" in b
               for b in _no_live_tunnel_and_no_alerts)


# ── re-pairing: one row per device, not one per pairing ──────────────────────
#
# Redemption used to mint unconditionally, so re-running the installer — which
# is supported and meant to be a no-op — left another live credential behind
# every time: eleven pairings of one Mac, eight device rows, measured on a
# clean VM. The device presents the credential it already holds, the host
# re-keys that row, and the secret it replaced dies in the same write.

def _wait_for_alerts(alerts):
    for _ in range(50):                       # _announce is threaded
        if alerts:
            return alerts
        time.sleep(0.02)
    return alerts


def test_re_pairing_re_keys_the_one_row_instead_of_adding_another(store):
    first = enrolment.redeem(_mint(store, name="My Laptop")[0], "198.51.100.4")
    second = enrolment.redeem(_mint(store, name="work-mac")[0], "198.51.100.4",
                              device_token=first["token"])
    assert second["device"]["id"] == first["device"]["id"]
    assert len(store.list_devices()) == 1
    assert (first["superseded"], second["superseded"]) == (False, True)
    # The name is the row's, not the second code's: a device that exists has a
    # name the user may have chosen, and pairing again is not renaming.
    assert second["device"]["name"] == "My Laptop"


def test_the_superseded_token_stops_working(store):
    """Retired, not merely orphaned. A row that kept working under its old
    secret would be a credential nobody was told about and nobody would revoke
    — which is what eight leftover rows on a VM actually were."""
    first = enrolment.redeem(_mint(store)[0], "198.51.100.4")
    second = enrolment.redeem(_mint(store)[0], "198.51.100.4",
                              device_token=first["token"])
    assert second["token"] != first["token"]
    assert devices.authenticate(first["token"]) is None
    assert devices.authenticate(second["token"]) == second["device"]["id"]


def test_a_prior_token_that_proves_nothing_costs_the_caller_nothing(store):
    """Unknown, mistyped, malformed: the code is already spent by the time the
    token is looked at, so every one of them falls through to a plain mint. A
    redemption that refused over a token it merely offered would take a
    credential the caller can never ask for again."""
    for offered in ("", "nonsense", "jr1.", "jr1.nosuchdevice.secret"):
        out = enrolment.redeem(_mint(store)[0], "198.51.100.4",
                               device_token=offered)
        assert devices.authenticate(out["token"]) == out["device"]["id"]
        assert out["superseded"] is False
    assert len(store.list_devices()) == 4


def test_a_revoked_device_cannot_re_key_its_way_back_in(store):
    """The re-key must not be a way around revocation. A revoked device still
    holding its token gets a fresh row — one the user can see and revoke
    again — and never its old one back."""
    first = enrolment.redeem(_mint(store)[0], "198.51.100.4")
    devices.revoke(first["device"]["id"])
    second = enrolment.redeem(_mint(store)[0], "198.51.100.4",
                              device_token=first["token"])
    assert second["device"]["id"] != first["device"]["id"]
    assert second["superseded"] is False
    assert store.device(first["device"]["id"])["revoked_at"] is not None
    assert devices.authenticate(first["token"]) is None


def test_the_shared_rows_are_never_re_keyed_by_a_pairing(store):
    """`legacy` is the token file and `host-internal` is the host's own
    plumbing — both are held by parties that are not the device pairing. One
    app re-pairing must not re-key the command line out of its credential."""
    for shared, secret in ((devices.LEGACY_ID, "the-token-file"),
                           (devices.INTERNAL_ID, "internal-secret")):
        store.add_device(shared, shared, devices._hash(secret))
        presented = (secret if shared == devices.LEGACY_ID
                     else f"{devices.TOKEN_PREFIX}.{shared}.{secret}")
        out = enrolment.redeem(_mint(store)[0], "198.51.100.4",
                               device_token=presented)
        assert out["device"]["id"] != shared
        assert out["superseded"] is False
        assert devices.authenticate(presented) == shared   # still theirs


def test_a_prior_token_never_grandfathers_a_row_onto_an_empty_host(store,
                                                                   monkeypatch):
    """The re-key reads the table directly instead of calling `authenticate`,
    which folds the token file into row `legacy` on first sight. Routed through
    that, an unauthenticated redemption presenting junk would WRITE a device
    row — the exact accumulation this whole path exists to stop."""
    monkeypatch.setattr("jstack_host.auth._expected_token",
                        lambda: "the-token-file")
    out = enrolment.redeem(_mint(store)[0], "198.51.100.4",
                           device_token="the-token-file")
    rows = store.list_devices()
    assert [r["id"] for r in rows] == [out["device"]["id"]]
    assert devices.LEGACY_ID not in [r["id"] for r in rows]


def test_a_re_pairing_says_so_instead_of_claiming_a_new_device(store,
                                                              _no_live_tunnel_and_no_alerts):
    """A device that was not here before is the alarming event. An alert that
    described a routine re-pairing in the same words would spend the alarm
    until nobody read either."""
    first = enrolment.redeem(_mint(store, name="work-mac")[0], "198.51.100.4")
    _wait_for_alerts(_no_live_tunnel_and_no_alerts).clear()
    enrolment.redeem(_mint(store)[0], "198.51.100.4",
                     device_token=first["token"])
    said = " ".join(_wait_for_alerts(_no_live_tunnel_and_no_alerts))
    assert "re-paired with" in said and "no longer works" in said
    assert "joined" not in said


# ── revoking the minter revokes its codes ────────────────────────────────────

def test_a_code_minted_by_a_revoked_device_is_dead(store):
    """Revoking a lost phone must also kill what it left outstanding —
    otherwise a stolen token buys enrolments for the whole TTL after the user
    has already done the one thing they were told would stop it."""
    row, _ = devices.mint("my-iphone")
    code, _ = _mint(store, created_by=row["id"])
    devices.revoke(row["id"])
    with pytest.raises(enrolment.EnrolmentError):
        enrolment.redeem(code, "198.51.100.4")


def test_refusing_a_revoked_minters_code_does_not_burn_it(store):
    """The check runs before the consume. A refused code that came back marked
    used could never be honoured again even if the revoke were undone."""
    row, _ = devices.mint("my-iphone")
    code, _ = _mint(store, created_by=row["id"])
    devices.revoke(row["id"])
    with pytest.raises(enrolment.EnrolmentError):
        enrolment.redeem(code, "198.51.100.4")
    assert store.enrolment_code(enrolment._hash(code))["used_at"] is None


def test_a_code_from_an_unknown_minter_still_works(store):
    """Host tooling mints with no device id. Treating unknown as revoked would
    break provisioning to close nothing."""
    code, _ = _mint(store, created_by="")
    assert enrolment.redeem(code, "198.51.100.4")["token"]


# ── the limiter ──────────────────────────────────────────────────────────────

def test_guessing_codes_locks_the_address_out(store, monkeypatch):
    """Redemption is unauthenticated by design, so without this it is the one
    unmetered guessing surface on the host. The bound is written out rather
    than read from the module — a test that derives its ceiling from the
    constant it is testing passes for any ceiling."""
    assert auth._FAIL_MAX == 5
    ip = "203.0.113.44"
    for i in range(5):
        with pytest.raises(enrolment.EnrolmentError):
            enrolment.redeem(f"2222222{enrolment.ALPHABET[i]}", ip)
    good, _ = _mint(store)
    with pytest.raises(enrolment.EnrolmentLockedOut) as e:
        enrolment.redeem(good, ip)
    assert e.value.seconds > 0
    assert store.enrolment_code(enrolment._hash(good))["used_at"] is None


def test_a_locked_out_address_cannot_burn_a_valid_code(store):
    """The limiter is asked before the code is read. A guesser that got locked
    out and then guessed right must not consume it."""
    ip = "203.0.113.45"
    for i in range(5):
        with pytest.raises(enrolment.EnrolmentError):
            enrolment.redeem(f"3333333{enrolment.ALPHABET[i]}", ip)
    code, _ = _mint(store)
    with pytest.raises(enrolment.EnrolmentLockedOut):
        enrolment.redeem(code, ip)
    assert enrolment.redeem(code, "198.51.100.9")["token"]   # still spendable


# ── the registry surface ─────────────────────────────────────────────────────

def test_listing_codes_never_publishes_the_digest(store):
    """40 bits behind a published SHA-256 is not behind anything — the digest
    is the code with an afternoon of compute in front of it."""
    code, _ = _mint(store)
    rows = enrolment.list_codes()
    assert rows and "code_hash" not in rows[0]
    assert enrolment._hash(code) not in str(rows)


def test_listing_states_a_code_as_live_used_or_expired(store):
    live, _ = _mint(store, name="live")
    used, _ = _mint(store, name="used")
    enrolment.redeem(used, "198.51.100.4")
    store.add_enrolment_code(enrolment._hash("MFQ47K2P"), "expired",
                             int(time.time()) - 1, "")
    by_name = {r["name"]: r for r in enrolment.list_codes()}
    assert by_name["live"]["state"] == "live"
    assert by_name["used"]["state"] == "used"
    assert "expired" not in by_name, "an expired unused code is swept, not listed"


def test_the_sweep_never_touches_a_used_row(store):
    """A used row is the record of which device this host let in. Housekeeping
    that erased it would erase the audit trail."""
    code, _ = _mint(store, ttl=60)
    enrolment.redeem(code, "198.51.100.4")
    stale, _ = _mint(store, name="never-used", ttl=60)
    # An hour past both expiries: the used row must survive it, the unused
    # one must not. A sweep run at `now` would pass this test by doing nothing.
    assert store.sweep_enrolment_codes(int(time.time()) + 3600) == 1
    assert store.enrolment_code(enrolment._hash(stale)) is None
    assert store.enrolment_code(enrolment._hash(code)) is not None


def test_revoking_takes_an_unused_code_out_and_leaves_a_used_one(store):
    live, live_out = _mint(store, name="live")
    used, _ = _mint(store, name="used")
    enrolment.redeem(used, "198.51.100.4")
    assert enrolment.revoke(live_out["code"].lower()) is True
    assert enrolment.revoke(live) is False          # already gone
    assert enrolment.revoke(used) is False          # used rows are permanent
    with pytest.raises(enrolment.EnrolmentError):
        enrolment.redeem(live, "198.51.100.4")


def test_enrolment_codes_never_sync(store):
    """Same law as `devices`: a table of credentials-in-waiting pooling across
    hosts would let one compromised store enrol devices everywhere."""
    _mint(store)
    assert "enrolment_codes" not in store.changes_since(0)


# ── the tunnel half ──────────────────────────────────────────────────────────

def test_a_host_with_no_mesh_still_hands_over_the_token(store):
    """A leaf has no peer to mint. That is a fact about the host, not a failure
    — and the code is already burned, so losing the token here would be
    unrecoverable."""
    code, _ = _mint(store)
    out = enrolment.redeem(code, "198.51.100.4")
    assert out["token"] and out["tunnel"] is None
    assert "does not run the tunnel" in out["tunnel_note"]


def test_a_machine_that_sent_no_grant_is_told_why_it_is_not_delegated(store,
                                                                      monkeypatch):
    """The live 2026-09-11 adoption: the Mac ran a `jstack-host` older than the
    build that sends `grant_token`, so `attach` succeeded, the tunnel came up
    and the row landed — with no grant behind it. The hub could never mint onto
    that machine again, and nothing anywhere said so. It surfaced days later as
    `pair-by-hand` in a menu, which reads as a setting somebody chose.

    `delegated: False` is already returned. The missing half is the reason.
    """
    monkeypatch.setattr(tunnel, "can_pair", lambda: True)
    monkeypatch.setattr(tunnel, "issue",
                        lambda d, leaf=False: {"device": d, "config": "[I]",
                                               "created": True})
    out = enrolment.mint_code("work-mac", "", 600, kind=enrolment.KIND_HOST)
    code = enrolment.normalize(out["code"])
    result = enrolment.redeem(code, "198.51.100.4", host_key="work-key",
                              port=9090)            # no grant_token — old build
    assert result["delegated"] is False
    assert "grant" in (result["tunnel_note"] or ""), (
        f"no reason given for a half-adoption: {result['tunnel_note']!r}")


def test_a_pairing_that_blows_up_never_costs_the_caller_its_token(store,
                                                                  monkeypatch):
    monkeypatch.setattr(tunnel, "can_pair", lambda: True)
    monkeypatch.setattr(tunnel, "issue", lambda d: 1 / 0)
    code, _ = _mint(store)
    out = enrolment.redeem(code, "198.51.100.4")
    assert devices.authenticate(out["token"])
    assert out["tunnel"] is None and "failed" in out["tunnel_note"]


def test_redemption_pairs_with_the_slug_of_the_device_name(store, monkeypatch):
    seen = {}
    monkeypatch.setattr(tunnel, "can_pair", lambda: True)
    monkeypatch.setattr(tunnel, "issue",
                        lambda d, leaf=False: seen.setdefault("peer", d) and
                        {"device": d, "config": "[Interface]", "created": True})
    code, _ = _mint(store, name="My Laptop")
    out = enrolment.redeem(code, "198.51.100.4")
    assert seen["peer"] == "my-laptop"
    assert out["tunnel"]["config"] == "[Interface]"


def test_peer_name_only_yields_names_wg_peer_will_take():
    assert enrolment.peer_name("My Laptop") == "my-laptop"
    assert enrolment.peer_name("  --Work_Mac!!  ") == "work-mac"
    assert enrolment.peer_name("!!!") == ""
    assert enrolment.peer_name("") == ""
    assert len(enrolment.peer_name("x" * 80)) == 31


def test_issue_drops_the_lan_rule_and_keeps_every_other_one(monkeypatch):
    """The enrolment path must bypass ONLY the source-address gate. A leaf
    still has no mesh, and a name wg_peer would reject is still rejected."""
    monkeypatch.setattr(tunnel, "can_pair", lambda: False)
    with pytest.raises(tunnel.PairingUnsupported):
        tunnel.issue("work-mac")
    monkeypatch.setattr(tunnel, "can_pair", lambda: True)
    with pytest.raises(tunnel.PairingRefused):
        tunnel.issue("Not A Peer Name")


# ── the endpoints ────────────────────────────────────────────────────────────

@pytest.fixture
def client(store):
    _, token = devices.mint("test-device")
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


def test_redeeming_needs_no_bearer_token_at_all(client, store):
    """The entire point of P3: the caller is a machine that has no token yet,
    and getting one is what it is here for."""
    r = client.post("/api/jremote/v1/enrolment/codes", json={"name": "work-mac"})
    assert r.status_code == 200
    code = r.json()["code"]

    anon = TestClient(app)                       # no Authorization header
    assert anon.get("/api/jremote/v1/devices").status_code == 401
    r = anon.post("/api/jremote/v1/enrolment/redeem", json={"code": code})
    assert r.status_code == 200
    assert devices.authenticate(r.json()["token"]) == r.json()["device"]["id"]


def test_the_api_carries_the_prior_token_through_to_the_re_key(client, store):
    """The one wire field the app fills in. Without it the host cannot tell a
    device pairing again from a device it has never seen — it cannot read one
    out of a code, and the token is the only thing in the request that proves
    anything."""
    def a_code():
        return client.post("/api/jremote/v1/enrolment/codes",
                           json={"name": "work-mac"}).json()["code"]

    anon = TestClient(app)                       # no Authorization header
    before = len(client.get("/api/jremote/v1/devices").json()["devices"])
    first = anon.post("/api/jremote/v1/enrolment/redeem",
                      json={"code": a_code()}).json()
    r = anon.post("/api/jremote/v1/enrolment/redeem",
                  json={"code": a_code(), "device_token": first["token"]})
    assert r.status_code == 200
    assert r.json()["device"]["id"] == first["device"]["id"]
    assert r.json()["superseded"] is True

    rows = client.get("/api/jremote/v1/devices").json()["devices"]
    assert len(rows) == before + 1               # two pairings, one new row
    assert devices.authenticate(first["token"]) is None


def test_minting_a_code_needs_a_token_but_not_the_lan(client, store):
    """TestClient's address is not a LAN address — which refuses `POST
    /devices` — and minting a code must still work, or the person minting it
    has to be standing at the Mac, which is the trip P3 removes."""
    assert client.post("/api/jremote/v1/devices",
                       json={"name": "x"}).status_code == 403
    r = client.post("/api/jremote/v1/enrolment/codes", json={"name": "work-mac"})
    assert r.status_code == 200

    anon = TestClient(app)
    assert anon.post("/api/jremote/v1/enrolment/codes",
                     json={"name": "x"}).status_code == 401


def test_the_minting_device_is_recorded_on_the_code(client, store):
    caller = client.get("/api/jremote/v1/devices").json()["devices"][0]["id"]
    client.post("/api/jremote/v1/enrolment/codes", json={"name": "work-mac"})
    rows = client.get("/api/jremote/v1/enrolment/codes").json()["codes"]
    assert rows[0]["created_by"] == caller
    assert "code_hash" not in rows[0]


def test_a_bad_code_is_a_401_that_says_nothing(client, store):
    anon = TestClient(app)
    r = anon.post("/api/jremote/v1/enrolment/redeem", json={"code": "22222222"})
    assert r.status_code == 401
    assert r.json()["detail"] == enrolment.REFUSED


def test_revoking_over_the_api(client, store):
    code = client.post("/api/jremote/v1/enrolment/codes",
                       json={"name": "work-mac"}).json()["code"]
    r = client.post("/api/jremote/v1/enrolment/codes/revoke", json={"code": code})
    assert r.status_code == 200
    assert client.post("/api/jremote/v1/enrolment/codes/revoke",
                       json={"code": code}).status_code == 404


# ── what `pair --open` is allowed to claim ───────────────────────────────────
#
# The gap these close shipped once, and a VM install caught it: the installer's
# pairing step printed "the app is open and connected to this Mac" on every
# fresh install while enrolling nothing, because `open` accepting a URL was
# being read as an app spending a code. A check that reports state it cannot
# observe is worse than no check.

def test_state_reads_a_code_by_the_code_itself(store):
    code, out = _mint(store)
    assert enrolment.state(out["code"]) == "live"
    assert enrolment.state(code.lower()) == "live", "case and grouping are display"
    assert enrolment.state("22222222") == "unknown"
    assert enrolment.state("nonsense") == "unknown"


def test_state_says_used_only_after_a_real_redemption(store):
    code, out = _mint(store)
    assert enrolment.state(out["code"]) == "live"
    enrolment.redeem(code, "198.51.100.4")
    assert enrolment.state(out["code"]) == "used"


def test_state_says_expired_and_never_consumes(store):
    store.add_enrolment_code(enrolment._hash("22222222"), "old",
                             int(time.time()) - 5, "", "device")
    assert enrolment.state("22222222") == "expired"
    assert enrolment.state("22222222") == "expired", "reading is not spending"
    assert store.enrolment_code(enrolment._hash("22222222"))["used_at"] is None


@pytest.fixture
def open_pair(store, monkeypatch):
    """`cli._hand_to_app` with its seams stubbed: what `open` did, and how long
    we are willing to wait for an answer. Returns a function that installs the
    `open_url` behaviour a given test wants."""
    from jstack_host import cli, desk, install_host
    monkeypatch.setattr(cli, "PAIR_WAIT", 0.3)
    monkeypatch.setattr(cli, "PAIR_POLL", 0.05)
    monkeypatch.setattr(install_host, "installed_port", lambda: 9090)
    monkeypatch.setattr("jstack_host.hostenv.host_name", lambda: "work-mac")

    def opens(behaviour):
        monkeypatch.setattr(desk, "open_url", behaviour)
        return cli
    return opens


def test_pair_open_refuses_to_call_a_fired_link_a_pairing(open_pair, store, capsys):
    """The exact shipped bug: Launch Services took the URL and no app spent the
    code. Non-zero, and the code printed — that is the recovery."""
    fired = []
    cli = open_pair(lambda url: bool(fired.append(url)) or True)
    _, out = _mint(store)
    assert cli._hand_to_app(out) == 1
    assert out["code"] in capsys.readouterr().out
    assert fired and fired[0].startswith("jremote://pair?")


def test_pair_open_reports_success_once_an_app_actually_redeems(open_pair, store):
    _, out = _mint(store)
    code = enrolment.normalize(out["code"])
    cli = open_pair(lambda url: bool(enrolment.redeem(code, "127.0.0.1")))
    assert cli._hand_to_app(out) == 0
    assert enrolment.state(out["code"]) == "used"


def test_pair_open_does_not_wait_on_a_link_nothing_took(open_pair, store):
    """No app at all: `open` failed, so there is nothing to wait for and the
    30-second budget must not be spent proving it."""
    cli = open_pair(lambda url: False)
    _, out = _mint(store)
    started = time.monotonic()
    assert cli._hand_to_app(out) == 1
    assert time.monotonic() - started < 0.3


def _pair_json(store, monkeypatch, capsys, inets):
    """Run `pair --json` against a host holding `inets`, return the payload."""
    import json
    from types import SimpleNamespace
    from jstack_host import addresses, cli, devices

    monkeypatch.setattr(cli, "_adopt", lambda a: None)
    monkeypatch.setattr(devices, "provisioned", lambda: True)
    monkeypatch.setattr(addresses, "_inet_ifaces",
                        lambda: {a: "en0" for a in inets})
    monkeypatch.setattr(addresses, "_hostname", lambda: "work-mac")

    args = SimpleNamespace(name="Friend phone", ttl=600, open=False,
                           json=True, state_dir=None, port=None)
    assert cli._cmd_pair(args) == 0
    return json.loads(capsys.readouterr().out)


def test_pair_json_puts_the_mesh_address_in_the_link(store, monkeypatch,
                                                     capsys):
    """What the menu bar's QR is made of. The tunnel is always-on, so the
    mesh address is the one a paired device reaches from any network — a QR
    carrying the LAN address instead scans cleanly and then times out the
    moment the phone is off this wifi, which the person reads as the pairing
    failing. This test used to pin the LAN address into the link on exactly
    that timeout argument, reversed: that was true of an on-demand tunnel
    and became the bug when the tunnel went always-on."""
    out = _pair_json(store, monkeypatch, capsys,
                     ["10.66.0.1", "192.168.0.106"])
    assert [a["kind"] for a in out["addresses"]] == ["lan", "local", "mesh"]
    assert out["link"].startswith("jremote://pair?")
    assert "url=http%3A%2F%2F10.66.0.1%3A9090" in out["link"]
    assert out["code"] in out["link"]


def test_pair_json_without_a_mesh_falls_back_to_the_first_address(
        store, monkeypatch, capsys):
    """A host running no tunnel has no roaming address to offer — the link
    carries the first address the host names, exactly as before."""
    out = _pair_json(store, monkeypatch, capsys, ["192.168.0.106"])
    assert "url=http%3A%2F%2F192.168.0.106%3A9090" in out["link"]
    assert out["code"] in out["link"]
