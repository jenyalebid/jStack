"""Bearer auth for the jRemote API — per-device tokens, rate-limited failures.

Independent of the dashboard's cookie auth: the app sends
`Authorization: Bearer <token>`. Since P2 (docs/multi-host-access.md) the
authority is the `devices` table in the host's store — each device carries its
own token, revocable alone — not the single shared file. The file still exists
as the `legacy` device's plaintext (three installed devices carry copies), and
`devices._grandfather` folds it into the table on first use.

The dashboard's AuthMiddleware exempts `/api/jremote` from the cookie check;
this dependency is the real gate. Fail-closed: no live device rows and no
token file means every request is rejected.

Failures are rate-limited in two tiers (Layer 3 of
docs/remote-access-security.md). The tight one is per *credential*: 5 bad
secrets for one device id, from one address, inside a minute lock that pairing
out for 15 minutes. The loose one is per address: `_SPRAY_MAX` bad tokens from
one address inside a minute lock the whole address out. Both alert through
`hostenv.security_alert`. Loopback is exempt from the *lockout* (never from
auth): the health probe deliberately sends bad tokens from loopback, the Mac
app lives there too, and a process on this machine is past the point where a
lockout is the defense.

WHY TWO TIERS. Locking the address alone is what a single-tier limiter does,
and it punishes the wrong thing. A device holds one row per host, and tokens are
not interchangeable between hosts — so pointing one row at the wrong host is an
ordinary mistake that produces a burst of rejects. Under a per-address lockout
that burst also takes down the device's *correct* rows, for fifteen minutes,
with a 429 that names neither the row at fault nor the reason. That happened
live on 2026-09-03: a phone whose work-Mac row pointed at the home Mac lost its
working home-Mac session too.

Per-credential keying keeps every bit of the protection that matters. Guessing a
secret means hammering one device id, which is exactly what the tight tier
counts. Spraying random ids never yields a lockout of a real credential, so the
loose tier catches that instead — at a threshold no misconfigured client reaches,
because a client only ever presents the handful of tokens it was given.

UNKNOWN IDS METER ONCE (jStack#50). A token whose id matches no row at all is
the other thing a stale client holds — a store wiped and re-provisioned, a
keychain entry outliving its host. No secret exists behind it, so its retries
feed no credential lock; each distinct id ticks the address meter once per
window. A scan is many ids, so the address wall stands; a client hammering its
one dead credential meters as one tick and never locks anything — including
the live credential it presents right beside it.
"""

import ipaddress
import threading
import time
from pathlib import Path

from fastapi import Header, HTTPException, Request

from . import hostenv

# ── the legacy token file ──
# Read only by devices._grandfather (one-time migration of an already-installed
# host) and install_host (fresh provisioning). Cached by (path, mtime) so a
# token written after startup is seen without a restart.
_cache: tuple[Path, float, str] | None = None


def _expected_token() -> str:
    global _cache
    path = hostenv.token_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _cache = None
        return ""
    if _cache is None or _cache[0] != path or _cache[1] != mtime:
        _cache = (path, mtime, path.read_text().strip())
    return _cache[2]


# ── failure rate limiting ──

_FAIL_WINDOW = 60.0
_FAIL_MAX = 5
_SPRAY_MAX = 50
_LOCKOUT_SECS = 15 * 60.0
_MAX_TRACKED = 4096

_limiter_lock = threading.Lock()
# keyed by scope ("<ip>|<device id>") — one guessed credential
_failures: dict[str, list[float]] = {}
# keyed by bare ip — every reject from that address, whatever it named
_spray: dict[str, list[float]] = {}
# keyed by either: a scope locks one credential, a bare ip locks the address
_locked_until: dict[str, float] = {}
# keyed by scope — unknown device ids already metered this window, so a stale
# credential retrying forever is one spray tick, not a stream (jStack#50)
_unknown_seen: dict[str, float] = {}


def _limiter_exempt(client_ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return True  # not a network caller (test harness) — nothing to lock
    return addr.is_loopback


def _scope(client_ip: str, presented: str) -> str:
    """The credential this attempt is guessing at, as a lockout key.

    A token names its own device id in the clear, so the id is knowable without
    authenticating — which is what lets the counter be per-credential. Anything
    with no id to read (absent, malformed) shares the `-` bucket: those are
    indistinguishable from each other, and pooling them is the conservative
    reading, not the lenient one.
    """
    from . import devices
    device_id, _secret = devices.parse(presented) if presented else ("", "")
    return f"{client_ip}|{device_id or '-'}"


def _locked_out(client_ip: str, scope: str) -> float:
    """Seconds of lockout remaining for this attempt, 0 when clear.

    Either tier can be holding it — the credential's own lock or the address's.
    The longer of the two wins, so `Retry-After` never under-promises.
    """
    if _limiter_exempt(client_ip):
        return 0.0
    now = time.time()
    longest = 0.0
    with _limiter_lock:
        for key in (scope, client_ip):
            remaining = _locked_until.get(key, 0.0) - now
            if remaining <= 0:
                _locked_until.pop(key, None)
            else:
                longest = max(longest, remaining)
    return longest


def _prune(tracked: dict[str, list[float]], now: float) -> None:
    """Bound the tables — a scanner must not grow them forever."""
    if len(tracked) <= _MAX_TRACKED:
        return
    for key in [k for k, ts in tracked.items()
                if not ts or now - ts[-1] >= _FAIL_WINDOW]:
        del tracked[key]


def _record_failure(client_ip: str, scope: str | None) -> None:
    """Count one failure — against the credential and the address, or with
    `scope=None` against the address alone (an unknown id has no credential
    tier: there is no row behind it for a guess to converge on)."""
    if _limiter_exempt(client_ip):
        return
    now = time.time()
    tiers: list[tuple[dict[str, list[float]], str, int, str]] = [
        (_spray, client_ip, _SPRAY_MAX, "address")]
    if scope is not None:
        tiers.insert(0, (_failures, scope, _FAIL_MAX, "credential"))
    tripped: list[tuple[str, str, int]] = []
    with _limiter_lock:
        for table, key, ceiling, what in tiers:
            recent = [t for t in table.get(key, ()) if now - t < _FAIL_WINDOW]
            recent.append(now)
            table[key] = recent
            _prune(table, now)
            if len(recent) >= ceiling and _locked_until.get(key, 0.0) <= now:
                _locked_until[key] = now + _LOCKOUT_SECS
                table[key] = []
                tripped.append((what, key, ceiling))
    for what, key, ceiling in tripped:
        subject = (f"device id {key.split('|', 1)[1]} from {key.split('|', 1)[0]}"
                   if what == "credential" else f"address {key}")
        body = (f"jRemote auth lockout: {ceiling} bad tokens inside "
                f"{_FAIL_WINDOW:.0f}s against {subject} on {hostenv.host_name()} "
                f"— locked out for {_LOCKOUT_SECS / 60:.0f} min.")
        # Threaded: an alert send (Telegram on this Mac) must never stall the
        # request that tripped it.
        threading.Thread(target=hostenv.security_alert, args=(body,),
                         daemon=True).start()


def _note_denial(client_ip: str, scope: str, presented: str) -> None:
    """Count one rejection toward the limiter — unless it was a cancelled
    credential, which is denied but never counted.

    The limiter exists to stop a caller guessing its way to a token. Someone
    holding a token we minted and later revoked is not guessing: they already
    have the secret, and no number of retries moves them closer to a live one.
    Locking them out denies nothing the 401 had not already denied, and costs
    the one thing a lockout can cost — the machine's own way back, for fifteen
    minutes after the row is put right.

    An id with no row at all (jStack#50) is the other non-guess: `cancelled`
    spared the revoked row's own token, but a row that is *gone* — a store
    wiped and re-provisioned, a keychain entry outliving its host — left the
    retrying client branded an attacker, relocking itself every window. Those
    are metered through `_note_orphan` instead: never the credential tier,
    one address tick per distinct id per window.
    """
    from . import devices
    if devices.cancelled(presented):
        return
    if devices.orphaned(presented):
        _note_orphan(client_ip, presented)
        return
    _record_failure(client_ip, scope)


def _note_orphan(client_ip: str, presented: str) -> None:
    """An unknown device id: one spray tick per distinct id per window, and
    never a credential-tier count.

    There is no row, so there is no secret a retry converges on — counting
    every attempt protected nothing, and it is what armed the fifteen-minute
    relock loop a phone ran against itself with one stale credential
    (jStack#50, the three-day "off-network just stops working"). But the
    id-space scan the address tier exists for is many DISTINCT ids, not one
    id many times — so a first sighting still ticks the address meter, and
    only the repeats are free. Fifty fresh ids in a window still lock the
    address; one dead id hammered forever never locks anything.
    """
    if _limiter_exempt(client_ip):
        return
    from . import devices
    device_id, _secret = devices.parse(presented)
    now = time.time()
    key = f"{client_ip}|{device_id}"
    with _limiter_lock:
        if now - _unknown_seen.get(key, 0.0) < _FAIL_WINDOW:
            return
        _unknown_seen[key] = now
        if len(_unknown_seen) > _MAX_TRACKED:
            for k in [k for k, t in _unknown_seen.items()
                      if now - t >= _FAIL_WINDOW]:
                del _unknown_seen[k]
    _record_failure(client_ip, None)


def lockout_remaining(client_ip: str, scope: str) -> float:
    """Seconds this attempt is locked out for, 0 when clear — the limiter as a
    facility, for gates that are not bearer auth.

    Enrolment redemption is the first of those: it is unauthenticated by
    design, so it has no credential to key on and no device to blame, and
    without this it would be the one unmetered guessing surface on the host.
    Given a scope of its own it inherits both tiers, the same lockout, and the
    same alert — rather than growing a second limiter that would have to be
    kept honest separately.
    """
    return _locked_out(client_ip, scope)


def note_failure(client_ip: str, scope: str) -> None:
    """Count one failed attempt against `scope` and this address."""
    _record_failure(client_ip, scope)


def reset_limiter() -> None:
    """Drop all failure/lockout state — test isolation only."""
    with _limiter_lock:
        _failures.clear()
        _spray.clear()
        _locked_until.clear()
        _unknown_seen.clear()


# ── the gate ──

def _client_ip(request: Request | None) -> str:
    if request is not None and request.client:
        return request.client.host or ""
    return ""


def _deny_log(client_ip: str, presented: str) -> None:
    """Say why a 401 happened, in the host's log, before raising it.

    The response can only ever say "invalid or missing bearer token" — telling
    a caller *which* part of its credential was wrong is a probing oracle. But
    the host's own operator has no other window: a phone that will not connect
    shows one message for four different faults, and without this line the only
    way to tell a mangled paste from a revoked row is to guess and re-mint.
    """
    from . import devices
    print(f"jremote auth: 401 from {client_ip or 'local'} — "
          f"{devices.deny_reason(presented)}", flush=True)


def _gate(client_ip: str, authorization: str) -> str:
    """Auth one request: the device id on success, HTTPException otherwise.

    The token is read before the lockout is checked, because which lockout
    applies depends on which credential is being presented — a device whose
    other row is locked out must still be able to use the row that works.
    """
    prefix = "Bearer "
    presented = authorization[len(prefix):] if authorization.startswith(prefix) else ""
    scope = _scope(client_ip, presented)
    remaining = _locked_out(client_ip, scope)
    if remaining:
        print(f"jremote auth: 429 from {client_ip} — {scope} locked out, "
              f"{int(remaining)}s remaining", flush=True)
        raise HTTPException(
            status_code=429,
            detail="too many failed auth attempts — locked out",
            headers={"Retry-After": str(int(remaining) + 1)})
    from . import devices
    device_id = devices.authenticate(presented)
    if device_id is None:
        _deny_log(client_ip, presented)
        _note_denial(client_ip, scope, presented)
        raise HTTPException(status_code=401,
                            detail="invalid or missing bearer token")
    return device_id


BUILD_HEADER = "x-jremote-build"


def _note_build(device_id: str, build: str) -> None:
    """Record what build the authenticated caller says it is. Best-effort by
    law: version bookkeeping must never cost a request that already proved
    itself."""
    if not build:
        return
    try:
        from . import devices
        devices.note_build(device_id, build)
    except Exception as e:  # noqa: BLE001
        print(f"jremote auth: build note failed ({type(e).__name__}: {e})",
              flush=True)


async def require_token(request: Request,
                        authorization: str = Header(default="")) -> None:
    """FastAPI dependency. Raises 401 unless a live device's token is present."""
    device_id = _gate(_client_ip(request), authorization)
    _note_build(device_id, request.headers.get(BUILD_HEADER, ""))


async def current_device(request: Request,
                         authorization: str = Header(default="")) -> str:
    """The authenticated caller's device id — for routes that need to know
    *which* device this is (the device list, the streams' revocation checks)."""
    device_id = _gate(_client_ip(request), authorization)
    _note_build(device_id, request.headers.get(BUILD_HEADER, ""))
    return device_id


def authenticate_ws(ws) -> str:
    """WebSocket auth (accept → verify → close has no HTTPException path).
    Returns the device id, or "" — same gate, same limiter, same posture."""
    client_ip = ws.client.host if ws.client else ""
    auth_header = ws.headers.get("authorization", "")
    presented = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    scope = _scope(client_ip, presented)
    if _locked_out(client_ip, scope):
        return ""
    from . import devices
    device_id = devices.authenticate(presented)
    if device_id is None:
        _deny_log(client_ip, presented)
        _note_denial(client_ip, scope, presented)
        return ""
    _note_build(device_id, ws.headers.get(BUILD_HEADER, ""))
    return device_id
