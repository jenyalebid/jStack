"""One-time enrolment codes — authorization that travels, so the device doesn't.

Both credential paths this host owns are gated on the caller's source address:
`POST /devices` mints a token only from the LAN, `POST /tunnel/pair` issues a
peer config only from the LAN. That gate is not a permission check — it is a
*reachability* check standing in for one. On the LAN, being there is the proof.

Which is fine until the machine that needs enrolling is never on the LAN. A
second Mac in another building has no way in: root on it dissolves nothing,
because the thing it lacks is an address the hub will accept. Carrying a
config over by hand is the manual step this whole channel exists to remove.

So the proof gets carried explicitly instead of inferred from where the packet
came from (docs/multi-host-access.md, P3):

> **Enrolment is authorized, not located.** The hub mints a one-time,
> short-lived enrolment code. It is typed once into the new machine. That
> machine redeems it for its own device token and, on a host that owns a mesh,
> its own WireGuard peer.

Four properties carry the safety the address gate used to:

  · **Single-use**, enforced by a conditional UPDATE in the store, so two
    machines racing the same code cannot both win.
  · **Short-lived** — ten minutes by default, an hour at the most.
  · **Minted by an authenticated party**, and the row records *which* device
    minted it. Enrolment is attributable, permanently.
  · **Rate-limited** through the same two-tier limiter as bearer auth, keyed
    per-address, because redemption is by definition unauthenticated.

WHAT THIS WIDENS, PLAINLY. Before this module a stolen device token could not
mint more credentials from outside the LAN; now it can mint a code, and the code
mints a device. That is the deliberate trade the design named, and two things
bound it: the `created_by` column makes every enrolment traceable to the
credential that authorized it, and a code whose creator has since been revoked
is refused at redemption — so revoking a lost phone also kills the codes it left
outstanding, rather than leaving them live for their whole TTL.

RE-PAIRING IS NOT A SECOND DEVICE. Redeeming a code used to mint unconditionally,
so every re-run of the installer left another live credential behind on the one
path we tell people to re-run — eleven pairings of one Mac, eight device rows,
measured on a clean VM. A device that already holds a row here presents that
credential alongside the code, and `devices.rekey` gives that row a new secret
instead of minting beside it: one row per device, and the superseded token is
dead the moment the new one is handed over. The credential is the anchor
because nothing else in the request can be one — the host cannot read a device
out of a code, and an id it merely believed would let any code-holder re-key
somebody else's row. Absent, stale, revoked or mistyped all fall through to a
plain mint, because by the time it is looked at the code is already spent.

The codes table never syncs, for the same reason `devices` never does. See the
schema comment in store.py.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time

from . import auth, devices, hostenv

# No 0/O/1/I. A code is read off one screen and typed into another machine, so
# the character pairs that look alike are the ones that turn a good code into a
# failed enrolment nobody can debug — and the failure is terminal either way,
# because the attempt that consumed it is the only one there was.
ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CODE_LEN = 8          # 32**8 = 2**40, against a ten-minute window and a limiter
GROUP = 4             # displayed XXXX-XXXX; grouping is display, never content

DEFAULT_TTL = 600     # ten minutes — the doc's number, and long enough to walk
MIN_TTL = 60
MAX_TTL = 3600
# A carried code is not typed within the hour: the file it rides in is walked
# to another building, often the next day. That file already holds the
# machine's permanent private key, so a longer-lived code inside it adds no
# exposure the file did not already carry. Only `adopt --offline` asks for it.
MAX_OFFLINE_TTL = 7 * 86400

# The limiter scope redemption counts under. Not a device id — there is no
# device yet, which is the whole point — so every redemption from one address
# shares a bucket, and guessing codes trips the same lockout and the same alert
# as guessing tokens.
LIMITER_SCOPE = "enrol"

# One refusal message for every cause. Unknown, expired, already used and
# mistyped must be indistinguishable in the response, or the endpoint becomes
# an oracle telling an attacker which of its guesses was structurally right.
REFUSED = "that enrolment code is not valid — ask for a new one"

# What a code enrols. A device gets a token and, where the host owns a mesh, a
# peer; a host gets those *and* a row in `hosts`, so every device mirroring this
# one learns the machine exists without anybody typing its address in.
#
# The kind is fixed at mint. It cannot be a field on the redemption request:
# whoever redeems is by definition unauthenticated, and one that could declare
# itself a host would write its own row into the grid the user reads to decide
# which machines to trust.
KIND_DEVICE = "device"
KIND_HOST = "host"
# Only the local installer issues this kind. It introduces the host's own
# app without inventing a separately managed leaf device.
KIND_LOCAL = "local"
KINDS = (KIND_DEVICE, KIND_HOST)

# A host key is the redeeming machine's own `/host` id — minted there, never
# here, because local-first routing compares it against what the host on
# loopback answers. Free-form (JREMOTE_HOST_ID overrides it), so this bounds the
# shape rather than the format.
HOST_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")

DEFAULT_PORT = 9090


class EnrolmentError(Exception):
    """The code was not one this host will honour."""


class HostKeyRefused(Exception):
    """The machine's declared key or port is not one we can record.

    Its own exception, and it is raised BEFORE any code is looked up. A refusal
    that came after the lookup would answer differently depending on whether the
    code existed, which is exactly the oracle `REFUSED` exists to deny — so this
    one may say what is wrong, because at the point it is raised this host has
    not yet learned anything about the code.
    """


class EnrolmentLockedOut(Exception):
    """Too many bad codes from this address. Carries the seconds remaining."""

    def __init__(self, seconds: int):
        super().__init__("too many failed enrolment attempts — locked out")
        self.seconds = seconds


def _store():
    """The store holding the codes. A seam, so tests point enrolment at their
    own file instead of this Mac's — same shape as devices._store."""
    from .store import get_store
    return get_store()


def _hash(code: str) -> str:
    return "sha256:" + hashlib.sha256(code.encode()).hexdigest()


def normalize(raw: str) -> str:
    """A typed code → its canonical form, or "" when it cannot be one.

    Case and grouping are presentation: `mfq4-7k2p`, `MFQ47K2P` and `mfq4 7k2p`
    are one code. Characters outside the alphabet are dropped rather than
    mapped — they are excluded precisely *because* they are ambiguous, so a
    typed `O` has no single right answer, and dropping it yields the wrong
    length, which fails cleanly instead of silently redeeming something else.
    """
    cleaned = "".join(c for c in raw.upper() if c in ALPHABET)
    return cleaned if len(cleaned) == CODE_LEN else ""


def display(code: str) -> str:
    return f"{code[:GROUP]}-{code[GROUP:]}"


# ── minting ──

def mint_code(name: str, created_by: str, ttl: int = DEFAULT_TTL,
              kind: str = KIND_DEVICE, max_ttl: int | None = None) -> dict:
    """A new code for a device to be called `name`. Returned once, never stored.

    The name travels with the code rather than being chosen at redemption: the
    person minting it knows which machine it is for, and a redeemer that got to
    name itself could enrol under the name of a device the user already trusts,
    in the very registry they read to decide what to revoke. `kind` travels for
    the same reason, and a harder one — see the constants above. `max_ttl`
    lifts the clamp for a carried code (`MAX_OFFLINE_TTL`); left None, the
    typed-in window `MAX_TTL` applies.
    """
    if kind not in (*KINDS, KIND_LOCAL):
        raise EnrolmentError(f"unknown enrolment kind {kind!r}")
    try:
        ttl = int(ttl)
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL
    ttl = max(MIN_TTL, min(MAX_TTL if max_ttl is None else int(max_ttl), ttl))
    now = int(time.time())
    store = _store()
    store.sweep_enrolment_codes(now)
    for _ in range(3):
        code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
        if store.add_enrolment_code(_hash(code), name, now + ttl,
                                    created_by, kind):
            return {"code": display(code), "name": name, "kind": kind,
                    "expires_at": now + ttl, "expires_in": ttl}
    raise RuntimeError("could not mint an enrolment code")  # 3 collisions in 2**40


# ── redemption ──

def _creator_revoked(row: dict) -> bool:
    """Was the code minted by a credential that has since been revoked?

    Checked before the code is consumed, so a revoked phone's outstanding codes
    die with it instead of staying live for the rest of their TTL. A creator
    this host has no row for is not treated as revoked — codes minted by the
    host's own tooling carry no device id, and refusing those would break
    provisioning to close nothing.
    """
    creator = (row.get("created_by") or "").strip()
    if not creator:
        return False
    device_row = _store().device(creator)
    return device_row is not None and device_row["revoked_at"] is not None


PEER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


def peer_name(name: str) -> str:
    """A device name → a name `wg_peer.py` will accept, or "".

    Device names are what the user typed ("My Laptop"); peer names are a
    filename and a config stanza key. Deriving one from the other keeps the
    enrolment to a single typed code — the alternative is asking whoever mints
    the code for a second, syntactically-constrained name they would have to
    know the rules for.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:31]
    slug = slug.rstrip("-")
    return slug if PEER_NAME_RE.match(slug) else ""


def _tunnel_for(name: str, leaf: bool = False) -> tuple[dict | None, str]:
    """This device's peer config, or None and the reason there isn't one.

    Never raises. By the time this runs the code is already burned and the
    token already minted, so an exception here would cost the caller a
    credential it can never ask for again — over a tunnel that half of the
    hosts running this package do not even have. A leaf reaching this answers
    "no mesh to join", which is a fact about the host, not a failure.

    `leaf` follows the code's kind, not the caller's word for itself: a machine
    joining the mesh for good needs the install bundle, and a device toggling a
    VPN needs the client conf. They come off the same peer entry and are not
    interchangeable.
    """
    peer = peer_name(name)
    if not peer:
        return None, f"no WireGuard peer name can be made from {name!r}"
    from . import tunnel
    try:
        return tunnel.issue(peer, leaf=leaf), ""
    except tunnel.PairingUnsupported as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 — see the docstring: never lose the token
        return None, f"pairing {peer} failed: {type(exc).__name__}: {exc}"


# A client conf carries `Address = 10.66.0.7/32`; a leaf bundle carries the
# same fact as `WG_ADDR=10.66.0.7/32` in leaf.env, because `wg setconf` rejects
# an Address line as wg-quick syntax. One pattern for both, so the registry
# cannot end up with an address for one kind of peer and a blank for the other.
MESH_ADDRESS_RE = re.compile(r"^\s*(?:Address\s*=|WG_ADDR=)\s*([0-9.]+)", re.M)


def mesh_address(peer: dict | None) -> str:
    """The address `wg_peer.py` just handed this peer, read off what it wrote.

    The registry row has to carry an address a device can dial, and the only
    thing that knows it is the artefact the tunnel wrote a moment ago — asking
    `wg_peer.py` again would be a second answer that could disagree with the
    one the machine was actually given.
    """
    if not peer:
        return ""
    text = peer.get("config") or (peer.get("bundle") or {}).get("leaf.env", "")
    match = MESH_ADDRESS_RE.search(text or "")
    return match.group(1) if match else ""


def _own_mesh_address() -> str:
    """This host's own address on the mesh — the one a leaf reaches it at from
    inside the tunnel, and the address a leaf records so its devices can mint on
    this host in reverse (grants.py, the #62 direction).

    Read off the live interface rather than assumed: a hub owns the gateway, so
    the answer is almost always `10.66.0.1`, but taking it from what is actually
    configured keeps a non-gateway topology honest. The gateway is the fallback
    for the window between owning a mesh and the interface being up — better a
    stable guess than an empty address that mints nothing.
    """
    import ipaddress
    from . import addresses
    for raw in addresses._inet_addrs():
        try:
            if ipaddress.ip_address(raw) in addresses.MESH_SUBNET:
                return raw
        except ValueError:
            continue
    return str(next(addresses.MESH_SUBNET.hosts()))


def _check_host_claim(host_key: str, port: int) -> int:
    """Validate what a machine says about itself, or refuse loudly.

    Runs before the code is even looked up, so its detailed refusals cannot
    become an oracle. Returns the port to record.
    """
    if not HOST_KEY_RE.match(host_key or ""):
        raise HostKeyRefused(
            "that is not a usable host key — it is the machine's own id from "
            "its /host response, 8 to 128 characters of letters, digits, dot, "
            "dash, underscore or colon")
    # Absent falls back; zero does not. A caller that sent a port is telling us
    # something, and quietly substituting 9090 for it would put an address in
    # the registry that nothing is listening on.
    if port in (None, ""):
        port = DEFAULT_PORT
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise HostKeyRefused("that is not a usable port")
    if not 1 <= port <= 65535:
        raise HostKeyRefused("that is not a usable port")
    return port


def redeem(raw_code: str, client_ip: str, host_key: str = "",
           port: int = DEFAULT_PORT, device_token: str = "",
           grant_token: str = "", identity: str = "", *,
           parent_port: int = DEFAULT_PORT,
           ssh_pubkey: str = "", ssh_user: str = "") -> dict:
    """Spend a code: a device token, and a peer config where one applies.

    `device_token` is the credential the redeemer already holds on this host,
    when it has one. It re-keys that row rather than adding a second — see
    RE-PAIRING above — and it is read only after the consume, with everything
    else in this function unchanged whether it was sent or not.

    `grant_token` is the reciprocal: a credential the joining MACHINE minted on
    itself and is handing to this host, so this host can later mint device
    tokens there on behalf of devices it already trusts (grants.py). It is kept
    only for a host code with a valid key — a device sending one is sending
    something this host has no machine to attach it to — and it is never
    required: a leaf running an older build sends none and simply joins the mesh
    without delegating, which is a machine that works and a tile that asks for a
    code, not a failure.

    Order is deliberate. A declared host key is checked first, before this host
    has looked at the code at all — that is what lets its refusal be specific.
    The limiter is next, because a locked-out address must not get to burn a
    code it guessed. The creator check comes before the consume, because
    refusing a revoked minter's code should leave the code claimable by nobody
    rather than mark it used. Then the kind check, still before the consume: a
    host code redeemed without a key must stay claimable by the machine that was
    actually meant to have it. The consume is atomic, and everything after it
    has to succeed or degrade, never raise.
    """
    if host_key:
        port = _check_host_claim(host_key, port)

    scope = f"{client_ip}|{LIMITER_SCOPE}"
    remaining = auth.lockout_remaining(client_ip, scope)
    if remaining:
        raise EnrolmentLockedOut(int(remaining) + 1)

    code = normalize(raw_code)
    code_hash = _hash(code) if code else ""
    store = _store()
    now = int(time.time())

    row = store.enrolment_code(code_hash) if code_hash else None
    if row is None or _creator_revoked(row):
        auth.note_failure(client_ip, scope)
        raise EnrolmentError(REFUSED)
    kind = row.get("kind") or KIND_DEVICE
    from . import managed_access
    import ipaddress
    try:
        local = ipaddress.ip_address(client_ip).is_loopback
    except ValueError:
        local = False
    if (kind == KIND_LOCAL and not local) or (managed_access.is_leaf() and kind != KIND_LOCAL):
        raise EnrolmentError(REFUSED)
    if kind == KIND_HOST and not host_key:
        # The same blank refusal as an unknown code, deliberately: naming this
        # cause would tell a guesser their code was real. Unconsumed, so the
        # machine this was minted for can still spend it.
        auth.note_failure(client_ip, scope)
        raise EnrolmentError(REFUSED)
    if not store.consume_enrolment_code(code_hash, f"from {client_ip}", now):
        auth.note_failure(client_ip, scope)   # used, or expired: still a miss
        raise EnrolmentError(REFUSED)

    # The row this device already had here, re-keyed — or a new one. The code's
    # name is spent on the mint and NOT on the re-key: a row that exists has a
    # name the user may have chosen in the registry, and a re-pairing is not a
    # rename. It is also the truer answer — the device redeeming is the device
    # it always was, whoever the code was minted for.
    rekeyed = devices.rekey(device_token) if device_token and kind != KIND_LOCAL else None
    if kind == KIND_LOCAL:
        token = devices.internal_token()
        device_row = devices.row(devices.authenticate(token))
    else:
        device_row, token = rekeyed or devices.mint(row["name"], identity or None)
    device_row["revoked"] = False
    store.note_enrolment_device(code_hash, f"{device_row['id']} from {client_ip}")

    # Keep the control connection independently of Home visibility. Revoking
    # it to hide Home would also prevent sibling discovery and hub policy
    # checks. Ordinary API access is checked against the two per-leaf flags.
    reachback = True
    if kind == KIND_HOST:
        previous = store.host_row(host_key)
        reachback = bool(previous["sees_home"]) if previous else True

    peer, note = (None, "") if kind == KIND_LOCAL else _tunnel_for(row["name"], leaf=kind == KIND_HOST)
    host_row = None
    if kind == KIND_HOST:
        # After the token, and never conditional on the tunnel: a machine with
        # no route yet is still a machine this host let in, and a row with an
        # empty address says exactly that. Dropping it would lose the only
        # record of an enrolment whose code is already spent.
        host_row = _register_host(row["name"], host_key,
                                  mesh_address(peer), port)
        store.bind_host_device(host_key, device_row["id"])
        # Policy changes do not destroy the machine's control credential.
        # It remains usable only for managed discovery/authorization when
        # home visibility is off; the ordinary API enforces the restriction.
        if previous is None:
            store.set_host_visibility(host_key, sees_home=reachback, sees_leaves=True)
        # After the row, because the grant is keyed by the machine and a grant
        # held for a machine that is not in the registry is a credential no
        # surface can ever reach. Degrades like everything else past the
        # consume: a store that refuses this write costs delegated access, not
        # the enrolment the caller already paid its code for.
        if grant_token:
            from . import grants
            try:
                grants.remember(host_key, grant_token, f"{client_ip}")
            except Exception as exc:  # noqa: BLE001 — never lose the token
                note = (note + "; " if note else "") + (
                    f"the machine's delegation grant could not be stored "
                    f"({type(exc).__name__}) — devices will have to pair with "
                    f"it directly")
        else:
            # No grant at all, which is not a choice anyone made: every build
            # that knows to send one sends one. The machine is running a
            # `jstack-host` older than delegated minting, and it just completed
            # an adoption that looks identical to a working one from both ends.
            #
            # Said here because this is the only moment anything knows. The
            # machine prints what it is told and moves on; the hub's menu shows
            # the *result* as "pair-by-hand", which reads as a setting rather
            # than a Mac that silently arrived half-attached. Without this the
            # next surface to mention it is `mint_on` refusing, weeks later,
            # with an error about a grant nobody knew was missing.
            note = (note + "; " if note else "") + (
                "this machine sent no delegation grant — its jstack-host "
                "predates delegated minting, so devices will have to pair "
                "with it by hand. Upgrade the host there and re-attach to "
                "fix it")

    # The direction #62 was missing: a grant THIS host issues, so the leaf can
    # later hand its own devices access to THIS host the same way a hub hands
    # its devices access to a leaf — `grants.mint_on` against `/delegate/mint`,
    # just called from the other side. Symmetric with the block above (which
    # stores the grant the LEAF issued) and unconditional for the same reason
    # that one is: every build that knows to ask for it gets one, and a leaf
    # that predates this simply never presents it to anyone.
    leaf_grant = ""
    parent_identity: dict = {}
    if kind == KIND_HOST:
        from . import grants
        leaf_grant = grants.issue(host_key)
        parent_identity = {"key": hostenv.host_id(), "name": hostenv.host_name(),
                           "address": _own_mesh_address(), "port": parent_port}

    # The shell half of the handshake (#131): the machine's public key is
    # stored on its row, and the answer carries `shell_grants.leaf_shell` —
    # the same compute a live pull gets, so adoption and a flip can never
    # disagree. A machine sending no key (or one that fails `valid_pubkey`,
    # which would smuggle options into a sibling's authorized_keys) gets `{}`
    # and joins exactly as before. Degrades past the consume like everything
    # else here.
    shell: dict = {}
    if kind == KIND_HOST and ssh_pubkey:
        from . import shell_access, shell_grants
        try:
            if shell_access.valid_pubkey(ssh_pubkey):
                user = ssh_user if shell_access.valid_user(ssh_user) else ""
                store.set_host_shell(host_key, ssh_pubkey, user)
                shell = shell_grants.leaf_shell(host_key)
                # The hub can now `ssh` the machine it just adopted.
                shell_grants.refresh_hub_config()
        except Exception as exc:  # noqa: BLE001 — never lose the enrolment
            shell = {}
            note = (note + "; " if note else "") + (
                f"shell access could not be set up ({type(exc).__name__}) — "
                "re-run the joiner to retry")

    _announce(row, device_row, client_ip, kind, rekeyed is not None)
    return {"device": device_row, "token": token,
            "tunnel": peer, "tunnel_note": note,
            "kind": kind, "host": host_row,
            # Said out loud, because the machine reading this just received a
            # token it cannot use and would otherwise discover that as an
            # authentication failure days later, against a hub that looks up.
            "reachback": reachback,
            # Whether this host can now hand devices access to that machine
            # without anybody typing a second code. The attaching machine prints
            # it, because "you are on the mesh" and "your devices get in by
            # themselves" are two different outcomes and it just chose one.
            "delegated": bool(grant_token) and kind == KIND_HOST,
            # This host's own grant, and this host's own identity — the leaf's
            # half of delegated minting in reverse. Empty/absent for a device
            # code, which has no machine to carry either one back to.
            "leaf_grant": leaf_grant,
            "parent_identity": parent_identity,
            # What the joiner authorizes and reaches — empty for a device
            # code, or a machine that presented no usable key.
            "shell": shell,
            # Which of the two happened, said out loud. The app can tell from
            # the id, but only if it kept one; a caller pairing by hand cannot
            # tell a fresh credential from a replaced one at all, and "your old
            # token just stopped working" is not something to leave implicit.
            "superseded": rekeyed is not None}


def _register_host(name: str, key: str, address: str, port: int) -> dict:
    row = _store().upsert_host(key, name, address, port)
    row["deleted"] = bool(row["deleted"])
    return row


def _announce(code_row: dict, device_row: dict, client_ip: str,
              kind: str = KIND_DEVICE, superseded: bool = False) -> None:
    """Say out loud that a device just joined this host, or re-keyed on it.

    A new credential minted from off the LAN is the exact event the address
    gate used to make impossible, so it must not be silent. Threaded for the
    same reason the limiter's alerts are: a Telegram send must never stall the
    request that earned it.

    The two outcomes read differently because they mean different things: a
    device that was not here before is the alarming one, and an alert that
    called a re-key by the same words would spend the alarm on the routine
    event until nobody read either.
    """
    what = "machine" if kind == KIND_HOST else "device"
    did = "re-paired with" if superseded else "joined"
    body = (f"jRemote enrolment: {what} {device_row['name']} "
            f"({device_row['id']}) {did} {hostenv.host_name()} from "
            f"{client_ip or 'local'}, on a code minted by "
            f"{code_row.get('created_by') or 'the host'}."
            + (" The token it held before this no longer works."
               if superseded else ""))
    threading.Thread(target=hostenv.security_alert, args=(body,),
                     daemon=True).start()


# ── the registry surface ──

def list_codes() -> list[dict]:
    """Every code this host is still carrying, expired-but-unused ones swept.

    `code_hash` never leaves this function. The code is 40 bits; a published
    digest of it is not a one-way door, it is the code with an afternoon of
    compute in front of it — and unlike a device token there is no second
    factor behind it.
    """
    now = int(time.time())
    store = _store()
    store.sweep_enrolment_codes(now)
    out = []
    for row in store.list_enrolment_codes():
        row.pop("code_hash", None)
        row["state"] = ("used" if row["used_at"]
                        else "expired" if row["expires_at"] <= now
                        else "live")
        out.append(row)
    return out


def state(raw_code: str) -> str:
    """Where one code stands — "live", "used", "expired", or "unknown".

    By the code itself, because that is the only handle there is: `list_codes`
    deliberately never returns a hash, so a row cannot be picked out of that
    list by anything but its name, and two machines pairing under the same name
    are two rows. Holding the code is also what authorizes spending it, so this
    tells a caller nothing it could not already have found out by redeeming.

    Read-only and off the redemption path. `jstack-host pair --open` is the
    caller: firing a `jremote://pair` link proves only that Launch Services
    accepted a URL, and the used bit here is the one place the truth of whether
    an app actually took it is written down.
    """
    code = normalize(raw_code)
    if not code:
        return "unknown"
    row = _store().enrolment_code(_hash(code))
    if row is None:
        return "unknown"
    if row["used_at"]:
        return "used"
    return "expired" if row["expires_at"] <= int(time.time()) else "live"


def revoke(raw_code: str) -> bool:
    """Withdraw an unused code, by the code itself.

    By the code and not by an id, because the only person who can revoke one is
    the person holding it, and a ten-minute credential does not need a naming
    scheme to outlive it. A used row is never deleted — it is the record of
    which device this host let in.
    """
    code = normalize(raw_code)
    return bool(code) and _store().revoke_enrolment_code(_hash(code))


def sweep() -> int:
    """Drop expired, unused codes. Housekeeping — expiry is enforced at
    redemption, so a code this misses is already dead."""
    return _store().sweep_enrolment_codes(int(time.time()))
