"""Per-device API tokens — one credential per device, each revocable alone.

The single shared bearer token made every device the same device: rotating the
file locked out the phone, the iPad and the laptop at once, and a lost phone
meant re-pairing everything. This module replaces that with the `devices` table
in the host's store (docs/multi-host-access.md, P2):

  token   =  jr1.<device_id>.<secret>
  stored  =  sha256(secret) — never the token itself

The device id travels inside the token, so auth is one primary-key lookup and
one digest compare. The secret is 256 bits from `secrets.token_urlsafe` —
machine-minted, never human-chosen — which is why the digest is a plain SHA-256
rather than a memory-hard KDF: there is no dictionary to run against a random
256-bit value, and auth runs on every request, where an argon2/scrypt's
deliberate ~100ms would tax every board poll. (Same call GitHub makes for its
API tokens.) What matters is what the spec demands: a stolen table yields no
usable credential.

**Grandfathering.** A host that has run before holds one token in a file and
three of the user's devices carry copies of it. On the first auth against an empty
table, that file token becomes row `legacy` — every installed device keeps
working through the upgrade, and from then on the table is the only authority:
rotating the file does nothing, revoking `legacy` is the new rotation. A fresh
install still mints the file (install_host.py) and the first request folds it
in, so provisioning a new host is unchanged until enrolment codes land (P3).

**Re-pairing re-keys; it does not accumulate.** A device that already holds a
row here presents that credential when it redeems an enrolment code, and
`rekey()` gives the row it proves a new secret in place. Without it every run of
the installer minted beside the last one — eleven pairings of one Mac left
eleven live credentials nobody would ever think to revoke, on exactly the path
we tell people to re-run.

**Revocation is immediate.** `revoke()` stamps the row and then wakes every
`wait_revoked()` watcher for that device — the PTY WebSocket holds one, so a
revoked phone's terminal drops mid-keystroke instead of typing until it next
reconnects. The SSE streams poll `is_revoked` per iteration and `revoke()`
pokes the board watcher, so they fall within a tick.

**The host is a device too.** spawn/showdoc/the health check call the API over
loopback and need a real credential — `internal_token()` mints row
`host-internal` with its plaintext in the state dir. Its one privilege is
re-keying itself when the plaintext file is lost (the host owns the table
anyway); a *revoked* host-internal row stays revoked and the callers degrade to
their no-token fallbacks.

This table NEVER syncs. See the schema comment in store.py.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import ipaddress
import os
import secrets
import threading
import time
import uuid
from pathlib import Path

from . import hostenv

TOKEN_PREFIX = "jr1"
LEGACY_ID = "legacy"
INTERNAL_ID = "host-internal"

# Rows a re-pairing must never re-key, however good the token proving them.
# Both are shared: `legacy` is the token file, which the command line and every
# device installed before enrolment may all be carrying at once, and
# `host-internal` is the host's own plumbing. Re-keying either because one app
# paired again would lock out every other holder, silently, from a request that
# carried no authority over them.
SHARED_IDS = (LEGACY_ID, INTERNAL_ID)

# What the shared token file is called in the registry when this install is the
# one that minted it. The id stays `legacy` — `parse()` returns it for any
# unstructured token and that is a wire fact — but the NAME is a display fact,
# and on a machine born after enrolment "legacy" describes nothing that ever
# happened here. See `adopt_master_token`.
MASTER_NAME = "This Mac — command line"

# The mesh subnet, mirrored from wg_peer.py's SUBNET_PREFIX. On the hub the
# mint gate asks tunnel.is_lan_caller, which reads the subnet out of that
# script; a leaf host has no script to read, and its fallback must still
# refuse tunnel sources — 10.66.0.x is `is_private` and would pass otherwise.
MESH_SUBNET = ipaddress.ip_network("10.66.0.0/24")

# last_seen_at is written at most this often per device — it is a "when was
# this phone last here" display fact, and a write per request would put the
# board's poll cadence into WAL for nothing.
_SEEN_EVERY = 60.0
_seen_at: dict[str, float] = {}
_seen_lock = threading.Lock()

# Live watchers per device: (loop, event) pairs registered by long-lived
# connections. revoke() may run in a threadpool (sync FastAPI route), so the
# wake crosses via call_soon_threadsafe.
_watchers: dict[str, set] = {}
_watch_lock = threading.Lock()


def _store():
    """The store holding the devices table. A seam, so tests point every auth
    at their own file instead of this Mac's — the conftest patches it."""
    from .store import get_store
    return get_store()


def provisioned() -> bool:
    """Can anything authenticate against this host — a device row, or a token
    file the first request will grandfather into one.

    The question is never "is the token file present". On a host whose table
    has already decided, that file is a spent migration source and may well be
    gone (this module's own rule: rotating it does nothing, revoking `legacy`
    is the rotation). Every gate that means "is this host set up" asks HERE,
    so a hub carrying a dozen device rows is never told it has no token.
    """
    if hostenv.token_path().exists():
        return True
    try:
        return _store().count_devices() > 0
    except Exception:  # noqa: BLE001 — a broken store answers 401 anyway
        return False


def _hash(secret: str) -> str:
    return "sha256:" + hashlib.sha256(secret.encode()).hexdigest()


def parse(presented: str) -> tuple[str, str]:
    """A presented bearer → (device_id, secret).

    A `jr1.` token that does not split into three non-empty parts is malformed
    and answers ("", "") — it must never fall through to the legacy compare,
    where a mangled new-style token would be judged against the old shared
    secret. Anything else is a legacy candidate: the shared token predates
    structure, and `token_urlsafe` never emits a dot, so the prefix cannot
    collide with one.
    """
    if presented.startswith(TOKEN_PREFIX + "."):
        parts = presented.split(".", 2)
        if len(parts) == 3 and parts[1] and parts[2]:
            return parts[1], parts[2]
        return "", ""
    return LEGACY_ID, presented


def deny_reason(presented: str) -> str:
    """Why this token was refused — for the host's own log, never a response
    body and never the secret itself.

    From the device's end every failure reads the same: "invalid token". The
    four causes need four different fixes — an unknown device id means the
    wrong string entirely, a revoked row means re-mint, a wrong secret means
    the id survived a mangled paste, and untrimmed whitespace means the paste
    carried a newline the field did not strip. A 401 that cannot say which
    leaves the only person who can fix it guessing from the wrong end.
    """
    if not presented:
        return "no bearer token presented"
    device_id, _secret = parse(presented)
    shape = f"len={len(presented)}"
    if presented != presented.strip():
        shape += ", UNTRIMMED — leading/trailing whitespace or newline"
    if not device_id:
        return f"malformed {TOKEN_PREFIX}. token ({shape})"
    row = _store().device(device_id)
    if row is None:
        if device_id == LEGACY_ID:
            return f"legacy token, and no legacy row to match it ({shape})"
        return f"unknown device id {device_id} ({shape})"
    if row["revoked_at"] is not None:
        return f"revoked device {device_id} ({row['name']})"
    return f"wrong secret for {device_id} ({row['name']}) ({shape})"


def cancelled(presented: str) -> bool:
    """True when this token is a real mint whose only fault is a revoked row.

    The distinction the rate limiter needs. A correct secret for a known row
    cannot be arrived at by guessing — presenting it proves the holder was
    minted this token and has not noticed it was cancelled, which is a stale
    client, not an attacker. Counting it as a failed guess is what let a
    re-adopted Mac's app arm a fifteen-minute lockout against itself two
    seconds at a time.

    Deliberately narrower than `deny_reason`'s revoked case: that one answers
    on the device id alone, and a revoked id would then be a name anyone could
    borrow to guess the secret for free. The secret must check out.
    """
    if not presented:
        return False
    device_id, secret = parse(presented)
    if not device_id:
        return False
    row = _store().device(device_id)
    if row is None or row["revoked_at"] is None:
        return False
    return hmac.compare_digest(row["token_hash"], _hash(secret))


def orphaned(presented: str) -> bool:
    """True when this token is well-formed but names a device id no row
    matches — the other stale credential (jStack#50).

    `cancelled` covers the row that was revoked; this covers the row that is
    GONE — a store wiped and re-provisioned, a row deleted under a keychain
    entry, a credential minted for some other host entirely. Nothing exists
    behind the id for a guess to converge on, so the limiter protects nothing
    by counting these; what counting bought was a phone arming a
    fifteen-minute relock against itself with a credential this host will
    never take.

    No secret check, because there is nothing to check against — and unlike
    `cancelled`'s carve-out, this one gives an attacker nothing: a made-up id
    draws the same uniform 401 either way. The one id that can go from
    unknown to known, `legacy`, is materialized by `_grandfather` inside
    `authenticate` BEFORE the denial is noted — so only its very first
    presentation reads as orphaned, and every wrong secret after that counts
    against a row that now exists.
    """
    if not presented:
        return False
    device_id, _secret = parse(presented)
    if not device_id:
        return False
    return _store().device(device_id) is None


def authenticate(presented: str) -> str | None:
    """The device id this token proves, or None. The one auth answer.

    Fail-closed is inherited from the shape: no rows means no row matches.
    The single carve-out is the empty-table grandfather below, which requires
    the exact token a previous install minted — an attacker who has that
    token was already in before this module existed.
    """
    if not presented:
        return None
    device_id, secret = parse(presented)
    if not device_id:
        return None
    store = _store()
    row = store.device(device_id)
    if row is None and device_id == LEGACY_ID:
        row = _grandfather(store)
    if row is None or row["revoked_at"] is not None:
        return None
    if not hmac.compare_digest(row["token_hash"], _hash(secret)):
        return None
    _note_seen(device_id)
    return device_id


def _grandfather(store) -> dict | None:
    """First auth on a never-migrated host: fold the token file into row
    `legacy`, once. Only a table with no DEVICE rows migrates — a table that
    has decided (any row, even all revoked) never re-reads the file, or
    "write a new file" becomes a back door around revocation.

    `host-internal` does not count as deciding: it is self-minted plumbing,
    and on a restarted host the spawn-routing call races the user's phone for
    first request — if its row blocked migration, whichever machine call came
    first would lock every installed device out for good."""
    if any(r["id"] != INTERNAL_ID for r in store.list_devices()):
        return None
    from .auth import _expected_token
    file_token = _expected_token()
    if not file_token:
        return None
    store.add_device(LEGACY_ID, "legacy", _hash(file_token))
    return store.device(LEGACY_ID)


def adopt_master_token(token: str) -> bool:
    """Register a just-minted token file as this Mac's own row, at install.

    Without this the first authenticated call does it instead, through
    `_grandfather`, which files the row under `legacy` — so a Mac that is
    minutes old opens its device list on a credential named after an era it
    never lived through, "last seen 1 second ago", and the person reading it
    has every reason to think somebody else is already in.

    The installer is the one caller that knows the difference, because
    `mint_token` tells it whether the file was written now or was already
    there. A file already there is a real legacy candidate and is left alone —
    grandfathering stays exactly as it was, and no install can lock a host out
    by claiming a history it does not have.

    Gated on the same empty table `_grandfather` requires, for the same
    reason: a table that has decided is never re-written from a file. False
    when there was nothing to do.
    """
    if not token:
        return False
    store = _store()
    if any(r["id"] != INTERNAL_ID for r in store.list_devices()):
        return False
    return store.add_device(LEGACY_ID, MASTER_NAME, _hash(token))


def _note_seen(device_id: str) -> None:
    now = time.monotonic()
    with _seen_lock:
        last = _seen_at.get(device_id, 0.0)
        if now - last < _SEEN_EVERY:
            return
        _seen_at[device_id] = now
    _store().touch_device(device_id)


# client_build is written on change, not on request — it moves once per app
# update. The cache seeds lazily from the row so a restart doesn't buy one
# redundant write per device.
_build_seen: dict[str, str] = {}
_build_lock = threading.Lock()


def note_build(device_id: str, build: str) -> None:
    """Record the build string an authenticated request carried.

    This column is why "is the fix on the phone" is answerable from the host:
    before it, a build's arrival could only be inferred from behaviour, and a
    fix whose failure mode is silence is indistinguishable from a fix that
    never installed.
    """
    build = build.strip()[:64]
    if not build:
        return
    with _build_lock:
        if _build_seen.get(device_id) == build:
            return
    row = _store().device(device_id)
    if row is None:
        return
    if row.get("client_build") != build:
        # If this throws it propagates (the auth caller swallows it) and the
        # cache below is never reached — so a failed write is retried next
        # request instead of being stranded as "done" until the build changes.
        _store().note_device_build(device_id, build)
    with _build_lock:
        _build_seen[device_id] = build


# ── minting ──

def mint(name: str, identity: str | None = None) -> tuple[dict, str]:
    """A new device: returns (row, token). The token exists only in this
    return value — hand it to exactly one device.

    `identity` is the app-generated, Keychain-persisted id of the PHYSICAL
    device, presented at pair time (optional — an app that predates it sends
    none). When present, re-pairing the same device ROTATES its one row in
    place — same id, new secret, name refreshed, revocation cleared — instead
    of minting a second credential beside the first; that accumulation (one
    physical device showing up as two rows) is what this keying exists to stop.
    Absent, every call mints a fresh random row under a NULL identity, exactly
    as before — the backward-compatible path."""
    if identity:
        for _ in range(3):
            device_id = uuid.uuid4().hex[:12]
            secret = secrets.token_urlsafe(32)
            settled = _store().upsert_device_identity(
                device_id, name, _hash(secret), identity)
            if settled is not None:
                return (_store().device(settled),
                        f"{TOKEN_PREFIX}.{settled}.{secret}")
        raise RuntimeError("could not mint a device id")  # 3 uuid collisions
    for _ in range(3):
        device_id = uuid.uuid4().hex[:12]
        secret = secrets.token_urlsafe(32)
        if _store().add_device(device_id, name, _hash(secret)):
            return (_store().device(device_id),
                    f"{TOKEN_PREFIX}.{device_id}.{secret}")
    raise RuntimeError("could not mint a device id")  # 3 uuid collisions


def rekey(presented: str) -> tuple[dict, str] | None:
    """A device presenting the credential it already holds → that row, re-keyed.

    Returns (row, new token) in the shape `mint` does, so a caller can use one
    or the other without knowing which happened; None when the token proves
    nothing this host will re-key, and then the caller mints instead.

    **Possession is the authority.** A device id in the request would let
    anyone re-key anyone's row — and the point of the re-key is that the old
    secret dies, so that is a revocation handed to whoever can type an id.
    Holding the token grants nothing new: everything the re-key opens was
    already open to whoever presented it.

    **Deliberately not `authenticate()`.** That call carries the empty-table
    grandfather, which mints row `legacy` out of the token file on first sight.
    The one caller here is enrolment, which is unauthenticated by definition —
    routing it through the grandfather would let an unauthenticated request
    that presented junk write a device row, which is the exact accumulation
    this function exists to stop. It also never touches `last_seen_at`: this is
    a pairing, and dating the row from it would say the old credential was in
    use at the moment it stopped existing.

    Never resurrects: `set_device_hash` refuses a revoked row, so a revoked
    device that still has its token gets None and a plain mint — a new row the
    user can see and revoke, not the old one quietly walking back in.
    """
    device_id, secret = parse(presented)
    if not device_id or device_id in SHARED_IDS:
        return None
    row = _store().device(device_id)
    if row is None or row["revoked_at"] is not None:
        return None
    if not hmac.compare_digest(row["token_hash"], _hash(secret)):
        return None
    new_secret = secrets.token_urlsafe(32)
    if not _store().set_device_hash(device_id, _hash(new_secret)):
        return None      # revoked between the compare and the write
    return (_store().device(device_id),
            f"{TOKEN_PREFIX}.{device_id}.{new_secret}")


def mint_allowed_from(client_ip: str) -> bool:
    """Minting is LAN/loopback-only — the same law as tunnel pairing: a caller
    already inside the tunnel must not be able to turn one credential into an
    unbounded supply of them. On a host with no pairing script to consult
    (a leaf), the answer is loopback + private minus the mesh subnet.

    P1 NOTE: the optional netstack-sidecar leaf variant dials the API on real
    loopback, which would present tunnel callers AS loopback here. It must
    carry peer identity (or bind per-peer addresses) before this gate can
    trust loopback on such a host — flagged in docs/multi-host-access.md. The
    utun leaf path has no such problem: mesh callers arrive as 10.66.0.x.
    """
    try:
        from . import tunnel
        return tunnel.is_lan_caller(client_ip)
    except Exception:  # noqa: BLE001 — no wg script on this host: gate on shape alone
        try:
            addr = ipaddress.ip_address(client_ip)
        except ValueError:
            return False
        if addr in MESH_SUBNET:
            return False
        return addr.is_loopback or addr.is_private


# ── registry surface ──

def row(device_id: str) -> dict | None:
    """One device's row, or None.

    Read through *this* module's store, not `get_store()` directly, so a caller
    asking "who is making this request" reads the same table `authenticate`
    just decided against. The two resolve to one store on a real host and to two
    under the test fixtures, and a lookup that silently misses is how a route
    ends up naming every device "a device".
    """
    return _store().device(device_id)


def list_all() -> list[dict]:
    rows = _store().list_devices()
    for r in rows:
        r["revoked"] = r["revoked_at"] is not None
    return rows


def rename(device_id: str, name: str) -> bool:
    return _store().rename_device(device_id, name)


def is_revoked(device_id: str) -> bool:
    """Gone counts as revoked: a long-lived connection re-checking its device
    must drop when the row disappears, not linger on a KeyError."""
    row = _store().device(device_id)
    return row is None or row["revoked_at"] is not None


def revoke(device_id: str) -> bool:
    """Stamp the row and cut its live connections. False = nothing to do."""
    if not _store().revoke_device(device_id):
        return False
    with _watch_lock:
        waiting = list(_watchers.get(device_id, ()))
    for loop, event in waiting:
        loop.call_soon_threadsafe(event.set)
    try:
        from . import board_watch
        board_watch.poke()  # wakes the board SSE loops so they re-check now
    except Exception:  # noqa: BLE001 — a stream lingering a tick must not fail the revoke
        pass
    return True


async def wait_revoked(device_id: str) -> None:
    """Resolves when the device is revoked — the PTY holds one of these in its
    FIRST_COMPLETED set, so a revoke lands as a close, not a fact the next
    reconnect discovers. Checks after registering: a revoke that raced in
    between auth and here must still resolve."""
    loop = asyncio.get_running_loop()
    event = asyncio.Event()
    key = (loop, event)
    with _watch_lock:
        _watchers.setdefault(device_id, set()).add(key)
    try:
        if is_revoked(device_id):
            return
        # An active stream must obey the same hub policy as a fresh request.
        # Network authorization runs off the event loop. Ordinary local
        # credentials keep the existing event-driven revocation path.
        from . import managed_access
        while True:
            try:
                await asyncio.wait_for(event.wait(), timeout=2.0)
                return
            except asyncio.TimeoutError:
                if not await asyncio.to_thread(managed_access.stream_allowed, device_id):
                    return
    finally:
        with _watch_lock:
            waiting = _watchers.get(device_id)
            if waiting is not None:
                waiting.discard(key)
                if not waiting:
                    _watchers.pop(device_id, None)


# ── the host's own credential ──

def _credential_dir() -> Path:
    """Where the host's own plaintext lives — *beside the table that validates
    it*, not wherever `state_dir()` happens to point.

    The two are the same directory on a real host (the store is written into
    the state dir), so this changes nothing in production. It changes
    everything under test. `_internal_token_locked()` is a read-compare-write
    across both halves: it sets the row's hash through `_store()` and writes the
    plaintext to a path. Resolve those independently and a caller that has
    redirected one but not the other splits the credential in half — the hash
    lands in one world, the token in another, and nothing afterwards can
    reconcile them.

    That is not hypothetical. Infrastructure's `_isolate_jremote_devices` is an
    autouse fixture that patches `_store()` to a throwaway sqlite precisely so a
    test token is never folded into the real registry — and it had no reason to
    know this function also wrote a file. So every test run that reached
    `internal_token()` (showdoc routing does, on any `_route` call) minted a
    fresh secret, recorded its hash in a database deleted with `tmp_path`, and
    overwrote *this Mac's live credential* with a token nothing would ever
    accept again. The hub then answered 401 to its own health probe, its own
    spawn routing and its own menu bar, until someone re-keyed it by hand.
    Running the test suite took the hub down, silently, every time.

    Deriving the path from the store makes that unrepresentable: redirect the
    table and the plaintext follows it. The same lesson as #42 — two halves of
    one credential must not have two ways of being found.
    """
    db = getattr(_store(), "db_path", None)
    return Path(db).parent if db else hostenv.state_dir()


def internal_token() -> str:
    """The token the host uses to call its own API (spawn routing, showdoc
    routing, the health probe). Minted on demand as device `host-internal`,
    plaintext kept in the state dir, 0600.

    "" when the row is revoked — the user saying no to the host's own row is
    honored, and every caller already has a no-token fallback. A live row
    whose plaintext file was lost is re-keyed in place; that is the host
    re-minting itself, not a resurrection.

    **Serialized, because the re-key is read-compare-write and the losers of
    that race corrupt each other.** Two callers re-keying at once interleave as
    `set_hash(H1) · set_hash(H2) · write(T2) · write(T1)`, which leaves the row
    on H2 and the file on T1 — a mismatch no subsequent read can resolve, and
    every later caller sees that mismatch and re-keys again, restarting the
    race. Observed live on 2026-09-10: the host's own credential invalidated
    itself repeatedly, each /host call answering 401 minutes after a verified
    200. The flock makes the whole sequence one writer at a time, and the
    re-read inside it means a caller that arrives second takes the winner's
    token instead of minting a third.
    """
    # The lock belongs with the pair it serializes: a lock taken in the live
    # state dir would order writers that are editing two different stores.
    folder = _credential_dir()
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder / ".internal-token.lock"
    with open(lock, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            return _internal_token_locked()
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def _internal_token_locked() -> str:
    """`internal_token()`'s body, with the state-dir lock already held."""
    store = _store()
    row = store.device(INTERNAL_ID)
    if row is not None and row["revoked_at"] is not None:
        return ""
    path = _credential_dir() / "internal-token"
    try:
        token = path.read_text().strip()
    except OSError:
        token = ""
    if token and row is not None:
        _, secret = parse(token)
        if secret and hmac.compare_digest(row["token_hash"], _hash(secret)):
            return token
    secret = secrets.token_urlsafe(32)
    if row is None:
        if not store.add_device(INTERNAL_ID, INTERNAL_ID, _hash(secret)):
            # Raced another minter — theirs won; read the file they wrote.
            try:
                return path.read_text().strip()
            except OSError:
                return ""
    elif not store.set_device_hash(INTERNAL_ID, _hash(secret)):
        return ""  # revoked between the check and the re-key
    token = f"{TOKEN_PREFIX}.{INTERNAL_ID}.{secret}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token)
    os.chmod(path, 0o600)
    return token


def reset_for_tests() -> None:
    """Forget in-memory throttles and watchers — test isolation only."""
    with _seen_lock:
        _seen_at.clear()
    with _watch_lock:
        _watchers.clear()
    with _build_lock:
        _build_seen.clear()
