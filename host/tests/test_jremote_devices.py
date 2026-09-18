"""Per-device tokens — the P2 contract (docs/multi-host-access.md).

What is worth pinning here is not "a token round-trips" but the security
posture: the table is the ONLY authority once it exists, revocation is
absolute and immediate (live connections included), the shared file is a
one-time migration source and never a back door, and failures lock an address
out. Each test names the break it would catch.
"""

import argparse
import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from jstack_host.server import create_app

app = create_app()
from jstack_host import auth, devices, router
from jstack_host.store import SessionStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = SessionStore(db_path=tmp_path / "devices.sqlite")
    monkeypatch.setattr(devices, "_store", lambda: s)
    return s


@pytest.fixture
def on_console(monkeypatch):
    """Stand the caller up AS the hub's own menu bar — loopback on a hub.

    Device management is a hub-console action, and TestClient's
    address is neither loopback nor a hub, so every mint/rename/revoke-of-another
    is refused by default — which is the point. A test that means to act as the
    console says so by taking this fixture; the gate itself (`_is_loopback` AND
    `mode.is_hub`) is pinned separately below."""
    monkeypatch.setattr(router, "_hub_console", lambda request: True)
    monkeypatch.setattr("jstack_host.managed_access.console", lambda request: True)


@pytest.fixture
def legacy_file(tmp_path, monkeypatch):
    """A pre-P2 host: one shared token in a file, table empty."""
    path = tmp_path / "api-token"
    path.write_text("the-shared-token")
    monkeypatch.setattr(auth, "_cache", None)
    monkeypatch.setattr("jstack_host.hostenv.token_path", lambda: path)
    return path


# ── minting and the token shape ──────────────────────────────────────────────

def test_a_minted_token_authenticates_as_its_device(store):
    row, token = devices.mint("my-iphone")
    assert token.startswith("jr1.")
    assert devices.authenticate(token) == row["id"]
    assert store.device(row["id"])["name"] == "my-iphone"


def test_the_table_stores_a_hash_never_the_token(store):
    _, token = devices.mint("my-iphone")
    secret = token.split(".", 2)[2]
    for row in store.list_devices():
        assert token not in str(row.values())
        assert secret not in str(row.values())
        assert row["token_hash"].startswith("sha256:")


def test_a_right_id_with_a_wrong_secret_is_refused(store):
    row, _ = devices.mint("my-iphone")
    assert devices.authenticate(f"jr1.{row['id']}.wrong-secret") is None


def test_a_malformed_jr1_token_never_falls_through_to_legacy(store, legacy_file):
    """`jr1.` + the shared token must not be judged against the legacy row —
    a mangled new-style token that authenticated as `legacy` would make the
    prefix a disguise."""
    assert devices.authenticate("the-shared-token") == "legacy"  # migrates
    assert devices.authenticate("jr1.the-shared-token") is None
    assert devices.authenticate("jr1..secret") is None
    assert devices.authenticate("jr1.id.") is None


def test_empty_and_absent_tokens_fail_closed(store):
    assert devices.authenticate("") is None
    assert devices.authenticate("anything") is None  # empty table, no file


# ── re-pairing one physical device: identity keys the row ─────────────────────
#
# The break this catches: with no device identity, pairing the same Mac or iPad
# twice minted a second row, and the duplicates nobody would think to revoke
# piled up (one laptop under two names, one iPad as two rows). An app that sends
# a stable identity gets ONE row rotated in place instead; an app that sends none
# keeps the old every-pairing-is-a-row behavior, untouched.

def test_re_pairing_one_identity_rotates_one_row(store):
    """Same identity twice → the same row, a new secret, the old token dead, and
    exactly one row left behind — not a second live credential."""
    first, first_tok = devices.mint("My Laptop", identity="device-uuid-1")
    assert first["identity"] == "device-uuid-1"
    second, second_tok = devices.mint("my-laptop", identity="device-uuid-1")
    assert second["id"] == first["id"]                  # same row
    assert second_tok != first_tok                      # rotated secret
    assert devices.authenticate(first_tok) is None      # old token invalid
    assert devices.authenticate(second_tok) == first["id"]
    rows = [r for r in store.list_devices() if r["identity"] == "device-uuid-1"]
    assert len(rows) == 1                               # exactly one row
    assert rows[0]["name"] == "my-laptop"               # name refreshed


def test_re_pairing_revives_a_revoked_identity_in_place(store):
    """A device revoked and then paired again comes back as its OWN row with
    revocation cleared — not a ghost living on beside a fresh one.

    Deliberately unlike `rekey()`, which refuses to resurrect a revoked row: a
    re-pair presents a stable identity the user chose to keep using, and the one
    row it keys is the one the user sees and can revoke again."""
    first, first_tok = devices.mint("my-ipad", identity="ipad-uuid")
    assert devices.revoke(first["id"]) is True
    assert devices.authenticate(first_tok) is None
    second, second_tok = devices.mint("my-ipad", identity="ipad-uuid")
    assert second["id"] == first["id"]
    assert store.device(first["id"])["revoked_at"] is None        # revived
    assert devices.authenticate(second_tok) == first["id"]
    assert len([r for r in store.list_devices()
                if r["identity"] == "ipad-uuid"]) == 1


def test_minting_without_an_identity_still_makes_a_new_row_each_time(store):
    """Backward compatibility: no identity keys on nothing, so every pairing is
    a fresh random row under a NULL identity — the pre-field behavior."""
    a, _ = devices.mint("laptop")
    b, _ = devices.mint("laptop")
    assert a["id"] != b["id"]
    assert a["identity"] is None and b["identity"] is None
    assert store.count_devices() == 2


def test_the_partial_index_rejects_a_second_row_under_one_identity(store):
    """The cross-process backstop: even a direct insert cannot plant a second
    row under a non-null identity. NULL stays exempt — legacy, host-internal and
    every pre-field row carry it and must coexist."""
    import sqlite3
    devices.mint("phone", identity="shared-uuid")
    con = sqlite3.connect(store.db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO devices (id, name, token_hash, created_at, identity)"
                " VALUES ('other','phone2','sha256:x',0,'shared-uuid')")
            con.commit()
    finally:
        con.close()
    devices.mint("no-id-1")           # two NULL identities still coexist
    devices.mint("no-id-2")
    assert store.count_devices() == 3


# ── the legacy grandfather ───────────────────────────────────────────────────

def test_the_file_token_becomes_row_legacy_on_first_use(store, legacy_file):
    """The user's phone, iPad and laptop all carry the file token today — the
    upgrade must not lock out a single one of them."""
    assert devices.authenticate("the-shared-token") == "legacy"
    row = store.device("legacy")
    assert row is not None and row["revoked_at"] is None
    assert devices.authenticate("the-shared-token") == "legacy"  # and again


def test_rotating_the_file_after_migration_does_nothing(store, legacy_file):
    """Once the table exists, the file is history. A file write that minted a
    working credential would be a back door around revocation."""
    assert devices.authenticate("the-shared-token") == "legacy"
    legacy_file.write_text("a-brand-new-token")
    auth._cache = None
    assert devices.authenticate("a-brand-new-token") is None
    assert devices.authenticate("the-shared-token") == "legacy"


def test_host_internal_minting_first_does_not_block_the_grandfather(
        store, legacy_file, tmp_path, monkeypatch):
    """On a restarted host the spawn-routing call races the user's phone for the
    first request. If the host's self-minted row counted as "the table has
    decided", whichever boot had a machine call land first would lock every
    installed device out for good."""
    monkeypatch.setenv("JREMOTE_STATE_DIR", str(tmp_path / "state"))
    from jstack_host import hostenv
    hostenv.reset_profile()
    try:
        assert devices.internal_token()          # host-internal row exists…
        assert devices.authenticate("the-shared-token") == "legacy"  # …still migrates
    finally:
        monkeypatch.delenv("JREMOTE_STATE_DIR")
        hostenv.reset_profile()


def test_a_minted_device_blocks_the_grandfather(store, legacy_file):
    """A REAL device row is a decision: once one exists, the file is history
    and writing one mints nothing."""
    devices.mint("my-iphone")
    assert devices.authenticate("the-shared-token") is None


def test_a_revoked_legacy_is_not_regrandfathered(store, legacy_file):
    assert devices.authenticate("the-shared-token") == "legacy"
    assert devices.revoke("legacy")
    assert devices.authenticate("the-shared-token") is None


# ── adopting a freshly minted token file ─────────────────────────────────────

def test_a_new_install_names_its_own_token_not_legacy(store, legacy_file):
    """A Mac minutes old has no legacy. Left to the grandfather, its first
    authenticated call files the installer's token under `legacy` and the
    device list opens on a credential named after an era this machine never
    lived through — seen one second ago, on a board nobody has used yet."""
    assert devices.adopt_master_token("the-shared-token") is True
    row = store.device("legacy")
    assert row["name"] == devices.MASTER_NAME
    assert "legacy" not in row["name"].lower()
    # Still the working credential, and still under the id the wire uses.
    assert devices.authenticate("the-shared-token") == "legacy"


def test_adopting_never_overwrites_a_table_that_has_decided(store, legacy_file):
    """The same gate the grandfather keeps: a re-run installer must not be
    able to re-key a live row, or "write a new file" becomes a back door
    around revocation."""
    devices.mint("my-iphone")
    assert devices.adopt_master_token("a-different-token") is False
    assert store.device("legacy") is None


def test_adopting_leaves_a_real_legacy_host_alone(store, legacy_file):
    """A token file already on disk is a real legacy candidate — the
    installer only adopts one it minted this minute, so the rescue path is
    untouched and no install can lock a host out by claiming a history it
    does not have."""
    assert devices.authenticate("the-shared-token") == "legacy"
    assert store.device("legacy")["name"] == "legacy"
    assert devices.adopt_master_token("the-shared-token") is False


def test_adopting_nothing_is_not_a_row(store):
    assert devices.adopt_master_token("") is False
    assert store.device("legacy") is None


# ── revocation ───────────────────────────────────────────────────────────────

def test_revoke_is_immediate_and_idempotent(store):
    row, token = devices.mint("my-iphone")
    assert devices.authenticate(token) == row["id"]
    assert devices.revoke(row["id"]) is True
    assert devices.authenticate(token) is None
    assert devices.revoke(row["id"]) is False  # second tap: nothing to do
    assert devices.is_revoked(row["id"])


def test_revoking_one_device_leaves_the_others_alone(store):
    """The whole point of P2 — one lost phone is one dead token."""
    phone, phone_tok = devices.mint("my-iphone")
    ipad, ipad_tok = devices.mint("my-ipad")
    devices.revoke(phone["id"])
    assert devices.authenticate(phone_tok) is None
    assert devices.authenticate(ipad_tok) == ipad["id"]


def test_an_unknown_device_reads_as_revoked(store):
    assert devices.is_revoked("never-minted")


def test_wait_revoked_resolves_on_revoke(store):
    """The PTY holds this watcher — if it never resolved, a revoked phone's
    terminal would keep typing until it next reconnected."""
    row, _ = devices.mint("my-iphone")

    async def scenario():
        waiter = asyncio.create_task(devices.wait_revoked(row["id"]))
        await asyncio.sleep(0)  # let it register
        assert not waiter.done()
        devices.revoke(row["id"])
        await asyncio.wait_for(waiter, timeout=2)

    asyncio.run(scenario())


def test_wait_revoked_on_an_already_revoked_device_returns_at_once(store):
    row, _ = devices.mint("my-iphone")
    devices.revoke(row["id"])

    async def scenario():
        await asyncio.wait_for(devices.wait_revoked(row["id"]), timeout=2)

    asyncio.run(scenario())


# ── the registry ─────────────────────────────────────────────────────────────

def test_rename_and_last_seen(store):
    row, token = devices.mint("phone")
    assert devices.rename(row["id"], "my-iphone")
    assert store.device(row["id"])["name"] == "my-iphone"
    assert store.device(row["id"])["last_seen_at"] is None
    devices.authenticate(token)
    assert store.device(row["id"])["last_seen_at"] is not None


def test_the_devices_table_never_rides_sync(store):
    """If tokens travelled with MetaSync, revoking one device would revoke all
    and every host's credentials would pool in one store. The sync payload
    must not know the table exists."""
    devices.mint("my-iphone")
    payload = store.changes_since(0)
    assert "devices" not in payload
    assert "token_hash" not in str(payload)


# ── the host's own credential ────────────────────────────────────────────────

def test_internal_token_mints_once_and_is_stable(store, tmp_path, monkeypatch):
    monkeypatch.setenv("JREMOTE_STATE_DIR", str(tmp_path / "state"))
    from jstack_host import hostenv
    hostenv.reset_profile()
    try:
        first = devices.internal_token()
        assert devices.authenticate(first) == devices.INTERNAL_ID
        assert devices.internal_token() == first
    finally:
        monkeypatch.delenv("JREMOTE_STATE_DIR")
        hostenv.reset_profile()


def test_concurrent_callers_converge_on_one_internal_token(store, tmp_path,
                                                          monkeypatch):
    """The regression this exists for: the re-key is read-compare-write, and
    two callers racing it left the row on one secret and the file on another —
    a mismatch that made every later caller re-key again, so the host's own
    credential invalidated itself in a loop and /host answered 401 minutes
    after a verified 200.

    Whatever order they run in, the file and the row must agree afterwards and
    the token on disk must authenticate."""
    import threading
    monkeypatch.setenv("JREMOTE_STATE_DIR", str(tmp_path / "state"))
    from jstack_host import hostenv
    hostenv.reset_profile()
    try:
        devices.internal_token()  # the row exists
        # Force the mismatch that sends every caller down the re-key path —
        # without this they all hit the fast return and nothing races.
        stale = f"{devices.TOKEN_PREFIX}.{devices.INTERNAL_ID}.notthesecret"
        # `_credential_dir()`, not `state_dir()`: the plaintext lives beside the
        # table that validates it, so that redirecting the store takes the file
        # with it. A test that plants the mismatch in the state dir plants it
        # somewhere nothing reads, and then races nobody.
        (devices._credential_dir() / "internal-token").write_text(stale)
        got: list[str] = []
        barrier = threading.Barrier(8)

        def race():
            barrier.wait()
            got.append(devices.internal_token())

        threads = [threading.Thread(target=race) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        on_disk = (devices._credential_dir() / "internal-token").read_text().strip()
        row = devices._store().device(devices.INTERNAL_ID)
        _, secret = devices.parse(on_disk)
        assert devices._hash(secret) == row["token_hash"], \
            "the file and the row disagree — the race corrupted the credential"
        assert devices.authenticate(on_disk) == devices.INTERNAL_ID
        assert set(got) == {on_disk}, "callers got tokens the host will reject"
    finally:
        monkeypatch.delenv("JREMOTE_STATE_DIR")
        hostenv.reset_profile()


def test_a_revoked_internal_row_stays_revoked(store, tmp_path, monkeypatch):
    """The host may re-key itself, never resurrect itself — a revoke the user
    made must not be quietly undone by the next spawn routing call."""
    monkeypatch.setenv("JREMOTE_STATE_DIR", str(tmp_path / "state"))
    from jstack_host import hostenv
    hostenv.reset_profile()
    try:
        token = devices.internal_token()
        assert token
        devices.revoke(devices.INTERNAL_ID)
        assert devices.internal_token() == ""
        assert devices.authenticate(token) is None
    finally:
        monkeypatch.delenv("JREMOTE_STATE_DIR")
        hostenv.reset_profile()


# ── the rate limiter ─────────────────────────────────────────────────────────

def test_five_guesses_at_one_credential_lock_that_credential_and_alert(
        store, monkeypatch):
    alerts = []
    monkeypatch.setattr("jstack_host.hostenv.security_alert", alerts.append)
    ip = "192.168.1.66"
    row, token = devices.mint("my-iphone")
    device_id = row["id"]
    for _ in range(5):
        with pytest.raises(Exception) as e:
            auth._gate(ip, f"Bearer jr1.{device_id}.wrongsecret")
        assert e.value.status_code == 401
    with pytest.raises(Exception) as e:
        auth._gate(ip, f"Bearer jr1.{device_id}.wrongsecret")
    assert e.value.status_code == 429
    # Even the RIGHT secret is refused: this credential is what was hammered.
    with pytest.raises(Exception) as e:
        auth._gate(ip, f"Bearer {token}")
    assert e.value.status_code == 429
    deadline = time.time() + 2
    while not alerts and time.time() < deadline:  # alert is threaded
        time.sleep(0.02)
    assert len(alerts) == 1 and ip in alerts[0] and device_id in alerts[0]


def test_a_wrong_row_does_not_lock_out_the_device_s_working_row(store):
    """The live 2026-09-03 failure: a phone whose work-Mac row pointed at the
    home Mac burned five rejects, and a per-address lockout took its correct
    home-Mac row down with it for fifteen minutes.

    Both rows come from one address — that is the whole point. And since
    jStack#50, the wrong row locks NOTHING, itself included: this host has no
    row for that id, so there is no secret its retries converge on, and a 429
    here is what turned one misconfiguration into a fifteen-minute relock
    loop. Every attempt reads 401 — deny_reason names the unknown id in the
    host's log — and the working row never notices.
    """
    ip = "10.66.0.2"
    _, good = devices.mint("my-iphone")
    foreign = "jr1.670f3e8827e3." + "x" * 40      # another host's device id
    for i in range(8):                            # past _FAIL_MAX, same id
        with pytest.raises(Exception) as e:
            auth._gate(ip, f"Bearer {foreign}")
        assert e.value.status_code == 401, f"locked out at attempt {i}"
    assert auth._gate(ip, f"Bearer {good}")       # the good row still works


def test_an_orphaned_credential_never_locks_anything(store, monkeypatch):
    """jStack#50, end to end: a credential whose row is GONE — not revoked,
    gone — retries forever from one address, well past the ADDRESS ceiling.

    Under per-attempt counting this armed a lockout every window for three
    days: the credential tier first, and past fifty retries a minute the
    address tier took the phone's live credential down alongside. An orphan
    meters once per window per id, so no volume of retries on one dead id
    reaches either ceiling, no alert fires, and the live credential riding
    the same address never misses a beat.
    """
    alerts: list[str] = []
    monkeypatch.setattr("jstack_host.hostenv.security_alert", alerts.append)
    ip = "10.66.0.4"
    row, token = devices.mint("wiped-mac")
    store.delete_device(row["id"])                # the row is gone, not revoked
    _, good = devices.mint("my-iphone")
    for i in range(60):                           # past _SPRAY_MAX, one id
        with pytest.raises(Exception) as e:
            auth._gate(ip, f"Bearer {token}")
        assert e.value.status_code == 401, f"locked out at attempt {i}"
    assert auth._gate(ip, f"Bearer {good}")       # the live row never noticed
    time.sleep(0.1)                               # alert would be threaded
    assert alerts == [], f"an orphan's retries raised an alert: {alerts}"


def test_spraying_many_device_ids_still_locks_the_address(store, monkeypatch):
    """Per-credential keying must not become a way to probe forever. No real
    client presents more than its handful of tokens, so the address ceiling is
    unreachable by mistake and reachable by a scanner."""
    monkeypatch.setattr("jstack_host.hostenv.security_alert", lambda _b: None)
    # The bound is written out, not read from the module: a test that derives
    # its ceiling from the constant it is testing passes for any ceiling,
    # including one raised until the tier never fires.
    assert auth._SPRAY_MAX == 50
    ip = "203.0.113.9"
    for i in range(50):
        with pytest.raises(Exception) as e:
            auth._gate(ip, f"Bearer jr1.{i:012x}.guess")
        assert e.value.status_code == 401, f"locked too early at attempt {i}"
    _, good = devices.mint("my-iphone")
    with pytest.raises(Exception) as e:           # address is out, all rows
        auth._gate(ip, f"Bearer {good}")
    assert e.value.status_code == 429


def test_startup_reconciles_a_credential_that_drifted_from_its_row(store, monkeypatch):
    """The live 2026-09-11 "No Access": the menu bar reads the plaintext file
    directly, the file had drifted from its row, and only showdoc and spawn
    ever call the repair — so the menu sat refused over a host that was up and
    one call from fixing itself. Reinstalling did not help; install.sh touches
    neither half.

    The lifespan is the one moment that runs on every install and every reboot,
    so the guard is on the wiring, not just on `internal_token()` — the repair
    already worked before this bug, it was simply never reached.
    """
    from fastapi.testclient import TestClient
    from jstack_host import board_watch, feed, hostenv, managed, server
    from jstack_host import store as session_store

    # Exercise credential reconciliation through the real lifespan without
    # launching unrelated indexers that outlive it and resolve live profiles
    # after this test's state-directory fixtures have been restored.
    async def no_watch():
        pass

    monkeypatch.setattr(session_store, "start_indexer", lambda: None)
    monkeypatch.setattr(feed, "start_indexer", lambda: None)
    monkeypatch.setattr(managed, "reconcile", lambda: [])
    monkeypatch.setattr(board_watch, "add_consumer", lambda _consumer: None)
    monkeypatch.setattr(board_watch, "ensure_running", no_watch)

    devices.internal_token()                       # row + file agree
    path = devices._credential_dir() / "internal-token"
    path.write_text(f"{devices.TOKEN_PREFIX}.{devices.INTERNAL_ID}.drifted")
    assert devices.authenticate(path.read_text().strip()) is None

    try:
        with TestClient(server.create_app()):      # runs the lifespan
            pass
    finally:
        # The lifespan resolves the host profile, and `profile()` caches. Left
        # populated, it is read by any later test that blocks the `lib` import
        # to assert a demanded profile RAISES — which it then does not, because
        # nothing imports anything. Booting a real app in a unit test is the
        # only thing here that reaches that cache, so it cleans up after itself.
        hostenv.reset_profile()

    assert devices.authenticate(path.read_text().strip()) == devices.INTERNAL_ID


def test_a_revoked_device_presenting_its_own_token_never_locks_out(store):
    """The live 2026-09-11 failure: a Mac was re-adopted, its old device row
    stayed revoked, and the app on it went on presenting the token it already
    held every two seconds. Five rejects armed a fifteen-minute lockout, and
    every retry after that read 429 — so restoring the row hub-side did not
    restore service, because the lockout outlived the fix.

    A correct secret for a known row is not a guess. Producing it requires
    having been minted the token, which is the thing the limiter exists to
    stop people doing. Cancelling a credential must deny it, not brand its
    holder a brute-force attacker.
    """
    ip = "10.66.0.9"
    row, token = devices.mint("work-mac")
    devices.revoke(row["id"])
    for i in range(12):                           # well past _FAIL_MAX
        with pytest.raises(Exception) as e:
            auth._gate(ip, f"Bearer {token}")
        assert e.value.status_code == 401, f"locked out at attempt {i}"
    # And the lockout it must not have armed is not holding anything else
    # from that address either.
    _, good = devices.mint("my-iphone")
    assert auth._gate(ip, f"Bearer {good}")


def test_a_wrong_secret_for_a_revoked_row_still_locks_out(store, monkeypatch):
    """The carve-out is the correct secret, not the device id. A revoked row
    must not become a free-guessing oracle — naming it buys nothing."""
    monkeypatch.setattr("jstack_host.hostenv.security_alert", lambda _b: None)
    ip = "203.0.113.11"
    row, _token = devices.mint("work-mac")
    devices.revoke(row["id"])
    for _ in range(5):
        with pytest.raises(Exception) as e:
            auth._gate(ip, f"Bearer jr1.{row['id']}.wrongsecret")
        assert e.value.status_code == 401
    with pytest.raises(Exception) as e:
        auth._gate(ip, f"Bearer jr1.{row['id']}.wrongsecret")
    assert e.value.status_code == 429


def test_a_locked_credential_reports_the_longer_of_the_two_lockouts(store,
                                                                    monkeypatch):
    """Retry-After must never under-promise when both tiers hold a lock."""
    monkeypatch.setattr("jstack_host.hostenv.security_alert", lambda _b: None)
    ip = "203.0.113.10"
    now = time.time()
    auth._locked_until[f"{ip}|abc"] = now + 60
    auth._locked_until[ip] = now + 600
    try:
        assert auth._locked_out(ip, f"{ip}|abc") > 500
    finally:
        auth.reset_limiter()


def test_loopback_is_exempt_from_lockout_never_from_auth(store):
    """The health probe sends deliberate bad tokens from loopback; the Mac app
    lives there too. They must never brick the host — but a bad token is
    still a 401."""
    for _ in range(10):
        with pytest.raises(Exception) as e:
            auth._gate("127.0.0.1", "Bearer wrong")
        assert e.value.status_code == 401


def test_failures_from_different_addresses_do_not_pool(store):
    for i in range(4):
        with pytest.raises(Exception):
            auth._gate(f"192.168.1.{i}", "Bearer wrong")
    with pytest.raises(Exception) as e:
        auth._gate("192.168.1.99", "Bearer wrong")
    assert e.value.status_code == 401  # 5th failure, but 1st from this address


def test_tokens_with_no_readable_id_share_one_bucket(store):
    """Absent and malformed tokens name no credential, so they cannot be told
    apart. Pooling them is the conservative read — the alternative is an
    unbounded set of un-lockable scopes."""
    ip = "192.168.1.77"
    assert auth._scope(ip, "") == auth._scope(ip, "jr1.")
    for _ in range(5):
        with pytest.raises(Exception):
            auth._gate(ip, "Bearer jr1.")
    with pytest.raises(Exception) as e:
        auth._gate(ip, "")                        # different shape, same bucket
    assert e.value.status_code == 429


# ── the endpoints ────────────────────────────────────────────────────────────

@pytest.fixture
def client(store):
    _, token = devices.mint("test-device")
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


def test_the_device_list_marks_the_caller(client, store):
    r = client.get("/api/jremote/v1/devices")
    assert r.status_code == 200
    rows = r.json()["devices"]
    assert [d["name"] for d in rows] == ["test-device"]
    assert rows[0]["current"] is True
    assert "token_hash" not in rows[0]


def test_a_remote_sees_only_its_own_device(client, store):
    """A remote must not see other devices. The caller is
    off-console (TestClient is not loopback-on-a-hub), so the roster comes back
    as its one row even with others in the table — the host refuses to enumerate
    them, not the app."""
    devices.mint("someone-elses-phone")
    devices.mint("the-hub-mac")
    rows = client.get("/api/jremote/v1/devices").json()["devices"]
    assert [d["name"] for d in rows] == ["test-device"]
    assert rows[0]["current"] is True


def test_the_hub_console_sees_every_device(client, store, on_console):
    """The audit surface is intact for the one caller entitled to it: the hub's
    own menu bar sees the whole table, revoked rows included."""
    devices.mint("someone-elses-phone")
    revoked, _ = devices.mint("an-old-laptop")
    devices.revoke(revoked["id"])
    rows = client.get("/api/jremote/v1/devices").json()["devices"]
    names = {d["name"] for d in rows}
    assert {"test-device", "someone-elses-phone", "an-old-laptop"} <= names
    assert any(d["current"] for d in rows)


def test_minting_is_refused_off_the_hub_console(client):
    """Adding a device is a hub menu-bar action. TestClient is not the hub
    console, so the mint is refused."""
    r = client.post("/api/jremote/v1/devices", json={"name": "intruder"})
    assert r.status_code == 403


def test_minting_on_the_console_hands_the_token_out_exactly_once(client, store, on_console):
    r = client.post("/api/jremote/v1/devices", json={"name": "my-ipad"})
    assert r.status_code == 200
    token = r.json()["token"]
    assert devices.authenticate(token) == r.json()["device"]["id"]
    assert token not in str(store.list_devices())


def test_re_pairing_over_the_route_reuses_one_row(client, store, on_console):
    """The route carries the identity through to the mint: the same device
    pairing again gets its one row rotated, not a duplicate."""
    base = "/api/jremote/v1/devices"
    first = client.post(base, json={"name": "My Laptop", "identity": "uuid-x"}).json()
    second = client.post(base, json={"name": "my-laptop", "identity": "uuid-x"}).json()
    assert second["device"]["id"] == first["device"]["id"]
    assert second["device"]["identity"] == "uuid-x"
    assert devices.authenticate(first["token"]) is None            # rotated out
    assert devices.authenticate(second["token"]) == first["device"]["id"]


def test_a_malformed_identity_is_refused_by_the_route(client, on_console):
    """Identity is a Keychain UUID, never human-typed — a value too long or
    carrying characters outside the machine alphabet is a mangled paste or a
    probe, and it is refused before it reaches the column that keys the table."""
    base = "/api/jremote/v1/devices"
    assert client.post(base, json={"name": "x", "identity": "has space"}).status_code == 400
    assert client.post(base, json={"name": "x", "identity": "a;b"}).status_code == 400
    assert client.post(base, json={"name": "x", "identity": "z" * 129}).status_code == 400
    # A well-formed identity still mints, and the response carries the field.
    r = client.post(base, json={"name": "x", "identity": "Keychain-UUID_1.2:3"})
    assert r.status_code == 200
    assert r.json()["device"]["identity"] == "Keychain-UUID_1.2:3"
    # No identity at all is accepted, exactly as before the field existed.
    assert client.post(base, json={"name": "x"}).status_code == 200


def test_mint_gate_tells_lan_from_tunnel_and_garbage():
    assert devices.mint_allowed_from("127.0.0.1")
    assert devices.mint_allowed_from("192.168.1.20")
    assert not devices.mint_allowed_from("10.66.0.3")  # inside the wg tunnel
    assert not devices.mint_allowed_from("8.8.8.8")
    assert not devices.mint_allowed_from("testclient")


def test_mint_gate_fallback_still_refuses_the_mesh(monkeypatch):
    """A leaf host has no wg_peer.py to read, so the gate falls back to shape
    alone — and 10.66.0.x is `is_private`, so without the explicit mesh
    exclusion a tunnel caller could mint on any leaf."""
    import jstack_host.tunnel as tunnel

    def no_script(ip):
        raise RuntimeError("no wg script on this host")

    monkeypatch.setattr(tunnel, "is_lan_caller", no_script)
    assert devices.mint_allowed_from("127.0.0.1")
    assert devices.mint_allowed_from("192.168.1.20")
    assert not devices.mint_allowed_from("10.66.0.7")
    assert not devices.mint_allowed_from("8.8.8.8")
    assert not devices.mint_allowed_from("testclient")


def test_a_device_can_always_revoke_itself(store):
    """The remote's one device action, from anywhere: disconnect this device."""
    row, token = devices.mint("my-phone")
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    r = c.post(f"/api/jremote/v1/devices/{row['id']}/revoke")
    assert r.status_code == 200 and r.json()["self"] is True
    assert devices.authenticate(token) is None


def test_a_remote_cannot_revoke_another_device(client, store):
    """A remote must not revoke another device. Off the hub console
    a revoke of anything but the caller itself is refused, and the target's
    token keeps working."""
    row, token = devices.mint("my-old-phone")
    r = client.post(f"/api/jremote/v1/devices/{row['id']}/revoke")
    assert r.status_code == 403
    assert devices.authenticate(token) == row["id"]     # still alive


def test_the_hub_console_can_revoke_another_device(client, store, on_console):
    """The kill that a remote may not do is the hub console's to do."""
    row, token = devices.mint("my-old-phone")
    r = client.post(f"/api/jremote/v1/devices/{row['id']}/revoke")
    assert r.status_code == 200 and r.json()["self"] is False
    assert devices.authenticate(token) is None
    assert client.post(f"/api/jremote/v1/devices/{row['id']}/revoke").status_code == 404


def test_a_revoked_device_cannot_use_the_registry(client, store):
    """Its own row included — revocation ends the credential everywhere."""
    row, token = devices.mint("my-old-phone")
    devices.revoke(row["id"])
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    assert c.get("/api/jremote/v1/devices").status_code == 401


def test_a_device_cannot_administer_even_its_own_label(store):
    row, token = devices.mint("phone")
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    r = c.post(f"/api/jremote/v1/devices/{row['id']}/rename",
               json={"name": "My iPhone 17"})
    assert r.status_code == 403
    assert store.device(row["id"])["name"] == "phone"


def test_a_remote_cannot_rename_another_device(client, store):
    row, _ = devices.mint("phone")
    r = client.post(f"/api/jremote/v1/devices/{row['id']}/rename",
                    json={"name": "hijacked"})
    assert r.status_code == 403
    assert store.device(row["id"])["name"] == "phone"


def test_the_hub_console_renames_and_404s_the_unknown(client, store, on_console):
    row, _ = devices.mint("phone")
    r = client.post(f"/api/jremote/v1/devices/{row['id']}/rename",
                    json={"name": "My iPhone 17"})
    assert r.status_code == 200
    assert store.device(row["id"])["name"] == "My iPhone 17"
    assert client.post("/api/jremote/v1/devices/nope/rename",
                       json={"name": "x"}).status_code == 404


# ── why a 401 happened ───────────────────────────────────────────────────────
#
# The response body is deliberately one sentence for every failure — telling a
# caller which half of its credential was wrong is a probing oracle. That
# leaves the host's log as the only place the four causes are distinguishable,
# and a device that will not connect is diagnosed from there or by guessing.

def test_deny_reason_separates_the_four_ways_a_token_fails(store):
    row, token = devices.mint("my-ipad")
    device_id = row["id"]

    assert "no bearer token" in devices.deny_reason("")
    assert "malformed" in devices.deny_reason("jr1.")
    assert "malformed" in devices.deny_reason("jr1.only-two-parts")
    assert f"unknown device id {'0' * 12}" in devices.deny_reason(f"jr1.{'0' * 12}.x")
    assert f"wrong secret for {device_id}" in devices.deny_reason(f"jr1.{device_id}.wrong")

    devices.revoke(device_id)
    reason = devices.deny_reason(token)
    assert "revoked" in reason and device_id in reason


def test_deny_reason_names_the_trailing_newline_a_paste_carries(store):
    """The failure this exists for: a token copied out of a fenced code block
    is character-for-character right and still 401s. Without the shape note the
    log reads 'wrong secret' and sends the reader to re-mint a good token."""
    _row, token = devices.mint("my-ipad")
    assert devices.authenticate(token + "\n") is None
    assert "UNTRIMMED" in devices.deny_reason(token + "\n")
    assert "UNTRIMMED" not in devices.deny_reason(token[:-1])


def test_deny_reason_never_echoes_the_secret(store):
    """It goes to a log file. A reason that quoted what was presented would
    write every valid token of every device that ever mistyped a host URL."""
    _row, token = devices.mint("my-ipad")
    secret = token.split(".", 2)[2]
    for presented in (token + "\n", f"jr1.{_row['id']}.{secret}x", secret, "jr1.x.y"):
        assert secret not in devices.deny_reason(presented)


# ── "is this host set up" — one predicate, not a file check ──────────────────

def test_provisioned_is_the_table_not_the_file(store, tmp_path, monkeypatch):
    """The break this catches, live on the hub 2026-09-09: `jstack-host pair`
    refused with "this host has no token yet" on the machine that owns the
    mesh and had already minted nine device rows. It asked whether
    `Credentials/jremote-api-token` existed — the spent migration source, long
    since gone on a host whose table has decided — so the ONE Mac that mints
    codes for every other device could not mint one.
    """
    monkeypatch.setattr("jstack_host.hostenv.token_path",
                        lambda: tmp_path / "never-written")

    assert devices.provisioned() is False       # no rows, no file
    devices.mint("my-laptop")
    assert devices.provisioned() is True        # the table decides


def test_provisioned_still_true_on_a_pre_migration_host(legacy_file, store):
    """The other half: a host whose devices table is empty and whose file is
    the credential three phones are carrying is provisioned, and the first
    request grandfathers it."""
    assert store.count_devices() == 0
    assert devices.provisioned() is True


def test_pair_mints_on_a_host_that_has_only_device_rows(store, tmp_path,
                                                        monkeypatch, capsys):
    """End to end through the CLI gate, since that is where it bit."""
    from jstack_host import cli

    monkeypatch.setattr("jstack_host.hostenv.token_path",
                        lambda: tmp_path / "never-written")
    monkeypatch.setattr(cli, "_adopt", lambda args: None)
    devices.mint("my-iphone")

    args = argparse.Namespace(name="My laptop", ttl=600, open=False,
                              state_dir=None, label=None)
    assert cli._cmd_pair(args) == 0
    assert "My laptop" in capsys.readouterr().out


def test_internal_token_refuses_to_re_key_from_a_dir_no_host_serves(
        store, tmp_path, monkeypatch):
    """The second half of #45, and the one with a live credential behind it.

    `internal_token()` re-keys the `host-internal` row whenever the plaintext
    beside it is missing. A process that resolved the wrong state dir does that
    into a store the real host never reads — the read-compare-write race
    d9b432f fixed *within* one state dir, now available across two, with each
    side re-keying what the other just wrote.

    Reading is untouched: this refuses the mint, not the answer.
    """
    import json

    from jstack_host import hostenv

    served = tmp_path / "served"
    marker = tmp_path / "embedded.json"
    marker.write_text(json.dumps({"server": "the dashboard", "port": 9090,
                                  "state_dir": str(served)}))
    monkeypatch.setenv("JREMOTE_EMBED_MARKER", str(marker))

    with pytest.raises(hostenv.SecondIdentity) as caught:
        devices.internal_token()
    assert str(served) in str(caught.value)
    assert store.device(devices.INTERNAL_ID) is None, "it minted a row anyway"
    assert not (tmp_path / "internal-token").exists(), "it wrote a secret anyway"
