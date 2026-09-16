"""Tunnel pairing for the jRemote app — a device pairs itself, over the LAN.

The phone pairs by scanning a QR off a LAN-only dashboard page. A Mac has no
camera pointed at its own screen, and asking the user to carry a .conf between
machines is the manual step this whole channel exists to remove. So a device
already talking to the host on the LAN can ask for its own WireGuard peer and
get the config back over the connection it is already authenticated on.

Two rules make that safe, and both are enforced here rather than trusted:

  · **Pairing is LAN-only.** A caller already inside the tunnel cannot mint more
    peers — otherwise one leaked device config becomes an unbounded supply of
    them. The check is the caller's own source address, which the client does
    not get to assert.
  · **A name that is already paired gets its existing config back**, never a
    fresh keypair. Re-pairing a machine must not silently strand the peer entry
    the host is still carrying for it.

`wg_peer.py` remains the only thing that writes wg0.conf; this module shells out
to it exactly as a human would, so pairing has one implementation.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
import sys
from pathlib import Path

from . import hostenv

#: The mesh tools live outside the package, not inside it — they are scripts a
#: person runs with `sudo`, and burying them in an importable package makes
#: them unfindable for the one job they have. *Which* copy is a profile answer
#: (`hostenv.peer_script`): a host that installed the tooling with the package
#: gets the one beside it, and a machine whose tunnel predates the package
#: keeps the copy its own daemons already drive.
PEER_SCRIPT = hostenv.peer_script()
#: The hub's tunnel state, resolved the *same way* `wg_peer.py` resolves it, so
#: the tool never writes one directory while this module reads another —
#: `WG_PEER_DIR` first, then the profile's answer. A profile answer and not a
#: path derived from this source for the same reason `PEER_SCRIPT` is one: the
#: two belong to the same mesh and have to resolve to the same tree, and this
#: Mac was the proof they could come apart (#42 — the hub reading
#: `<package>/Credentials/wireguard` while its daemons drove the copy under
#: Infrastructure, so `can_pair()` was False on the machine holding the peer
#: table). (The APNs key still lives under `credentials_dir()`: it is written in
#: the user's own install context, where that path is right.)
WG_DIR = hostenv.wireguard_dir()
CLIENTS_DIR = WG_DIR / "clients"
HUB_CONF = WG_DIR / "wg0.conf"

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


def rebind() -> None:
    """Re-resolve the four paths above against the environment as it is now.

    Module constants are bound at import, and one caller legitimately changes
    the answer afterwards: `install_host.adopt_installed_environment`, which
    exists precisely because a command typed into a shell has none of the
    installed agent's environment and must take it on before it looks at
    anything. Adopting `WG_PEER_DIR` a moment after this module resolved
    without it leaves the process reading the directory the shell implied and
    the tool writing the one the agent declared — the same split as #42,
    arrived at from the other side.

    Named and callable rather than left to whoever remembers to reassign three
    attributes: a rebind that misses `HUB_CONF` is a `can_pair()` that answers
    for a different mesh than `issue()` writes to.
    """
    global PEER_SCRIPT, WG_DIR, CLIENTS_DIR, HUB_CONF
    PEER_SCRIPT = hostenv.peer_script()
    WG_DIR = hostenv.wireguard_dir()
    CLIENTS_DIR = WG_DIR / "clients"
    HUB_CONF = WG_DIR / "wg0.conf"


class TunnelError(Exception):
    """Pairing could not be completed — distinct from being refused."""


class PairingRefused(Exception):
    """The caller is not allowed to pair (not on the LAN, bad name)."""


class PairingUnsupported(Exception):
    """This host does not run the tunnel, so it has no peers to mint."""


def can_pair() -> bool:
    """Whether this host is the one that owns the mesh.

    The test is `wg0.conf`, not the pairing script. Only a hub holds that file
    — it *is* the mesh, the list of peers the interface honours — while the
    script that edits it now ships to every host, because any Mac may be set
    up as a hub and the installer cannot know in advance which one will be.
    A leaf reached its hub by dialling out; its machine-level tunnel already
    routes the mesh and the app on it needs no peer of its own.

    Absence, not failure. Answering on the script's presence was right while
    only hubs carried it and became wrong the moment the payload did: every
    leaf would have offered a pairing button that could only fail, on a file
    it has no business owning. Before either, a leaf's Settings showed
    `cannot read the pairing script at …/wg_peer.py` — a missing-file error
    for a file that was never meant to be there.
    """
    return PEER_SCRIPT.is_file() and HUB_CONF.is_file()


def _peer_subnet() -> ipaddress.IPv4Network:
    """The peer subnet, read from wg_peer.py itself rather than restated here.

    A second copy of `10.66.0` in this file would keep passing after the real
    one moved, which is the failure the check exists to prevent.

    Read as text rather than imported: importing runs whatever the file does at
    module scope, and a subnet lookup is not a good reason to execute the tool
    that edits the live tunnel config.
    """
    try:
        source = PEER_SCRIPT.read_text()
    except OSError as exc:
        raise TunnelError(f"cannot read the pairing script at {PEER_SCRIPT}: {exc}")
    # Either the plain assignment or the env-override form wg_peer.py uses.
    match = re.search(
        r'^SUBNET_PREFIX\s*=\s*os\.environ\.get\([^,]+,\s*[\'"]([\d.]+)[\'"]',
        source, re.M) or re.search(
        r'^SUBNET_PREFIX\s*=\s*[\'"]([\d.]+)[\'"]', source, re.M)
    if not match:
        raise TunnelError(
            f"{PEER_SCRIPT.name} no longer declares SUBNET_PREFIX — the pairing "
            "gate cannot tell a tunnel caller from a LAN one")
    return ipaddress.ip_network(f"{match.group(1)}.0/24")


def is_lan_caller(client_ip: str) -> bool:
    """True when this caller reached us without going through the tunnel.

    Loopback and any private address count; an address inside the WireGuard
    subnet does not, and neither does anything routable from the internet.
    """
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    if addr in _peer_subnet():
        return False
    return addr.is_loopback or addr.is_private


#: One row of `wg_peer.py list` — `work-mac  10.66.0.7  added 2026-09-15`.
#: Matched rather than split on whitespace so that the empty table's own line,
#: "no devices paired", cannot be read as a peer named `no`.
PEER_LINE_RE = re.compile(r"^\s*([a-z0-9][a-z0-9-]*)\s+(\d{1,3}(?:\.\d{1,3}){3})\b")


def live_peers() -> set[str]:
    """Every name wg0.conf carries a peer entry for, right now.

    One listing for the whole table. A caller asking about more than one
    machine — the adopt dialog asks about all of them before it draws — would
    otherwise shell out once per name, against a file that answers them all.
    """
    listed = subprocess.run(
        [sys.executable, str(PEER_SCRIPT), "list"],
        capture_output=True, text=True, timeout=30,
    )
    if listed.returncode != 0:
        raise TunnelError(f"could not list peers: {listed.stderr.strip()}")
    return {m.group(1) for m in
            (PEER_LINE_RE.match(line) for line in listed.stdout.splitlines())
            if m}


def peer_is_live(device: str) -> bool:
    """Whether wg0.conf still carries a peer entry for this name.

    A stale artefact on disk with no live peer entry is not a pairing —
    re-issuing it would hand back a key the host has stopped accepting.

    Set membership and not a search. This ran `^\\s*{device}\\b`, and `-` is a
    word boundary, so `work` read as live off a table holding only `work-mac`.
    Every caller acts on the answer — the adopt steering refuses to write a
    carried file on it — and a name that is merely the prefix of a real peer
    must not be able to answer yes.
    """
    return device in live_peers()


def existing_config(device: str) -> str | None:
    """The config already issued to this device, if it is still paired."""
    path = CLIENTS_DIR / f"{device}.conf"
    if not path.exists():
        return None
    return path.read_text() if peer_is_live(device) else None


#: What `wg_peer.py add --leaf` writes into the bundle folder. Named here so a
#: file that stops being emitted is a loud absence rather than a bundle that
#: installs and then cannot start.
LEAF_FILES = ("jrleaf.conf", "leaf.env", "install_leaf.sh", "wg_up.sh",
              "wg_leaf_watch.sh", "README.md")


def leaf_bundle_dir(device: str) -> Path:
    return CLIENTS_DIR / f"{device}-leaf"


#: The three files in a bundle that belong to the hub rather than to the peer —
#: `wg_peer.py` copies them verbatim out of its own directory, and nothing in
#: them is per-machine. Everything else (the conf, leaf.env, the README) is the
#: peer's own issued state and must come back exactly as it was written.
HUB_OWNED = ("install_leaf.sh", "wg_up.sh", "wg_leaf_watch.sh")


def _read_bundle(device: str) -> dict[str, str]:
    """The bundle to hand a leaf — its own credentials, the hub's current code.

    A name that is already paired gets its existing config back, and for the
    keys and the address that is the whole point. The scripts are the opposite
    case: a machine re-attaching after the hub fixed one of them would receive
    the copy frozen into its client folder the first time it ever attached, and
    keep receiving it forever. That is how a leaf ends up running an installer
    whose bug was fixed on the hub weeks earlier, with nothing anywhere saying
    so. So the hub-owned three are read from where `wg_peer.py` copies them
    from, and only fall back to the issued copy on a hub whose script directory
    this package cannot see.
    """
    folder = leaf_bundle_dir(device)
    source = PEER_SCRIPT.parent
    files = {}
    for name in LEAF_FILES:
        path = folder / name
        if name in HUB_OWNED and (source / name).is_file():
            path = source / name
        if not path.exists():
            raise TunnelError(
                f"the leaf bundle for {device} is missing {name} — it would "
                "install a tunnel that cannot come up")
        files[name] = path.read_text()
    return files


def existing_bundle(device: str) -> dict[str, str] | None:
    """The leaf bundle already issued to this machine, if it is still paired."""
    if not leaf_bundle_dir(device).is_dir():
        return None
    return _read_bundle(device) if peer_is_live(device) else None


def _refuse_unless_pairable(device: str) -> None:
    """The two refusals that hold however the caller proved itself."""
    if not can_pair():
        raise PairingUnsupported(
            "this host does not run the tunnel — it reaches the mesh through "
            "the hub it dials out to, and needs no peer of its own")
    if not NAME_RE.match(device or ""):
        raise PairingRefused(
            f"bad device name {device!r} — lowercase letters, digits and dashes")


def pair(device: str, client_ip: str) -> dict:
    """Pair `device` and return its WireGuard config.

    Raises PairingRefused for a caller or name we will not serve, TunnelError
    when pairing was attempted and failed.
    """
    _refuse_unless_pairable(device)
    if not is_lan_caller(client_ip):
        raise PairingRefused(
            "pairing is only available on the host's own network — connect to "
            "the same Wi-Fi as the Mac and try again")
    return issue(device)


def issue(device: str, leaf: bool = False) -> dict:
    """Pair with NO source-address gate — for a caller authorized some other way.

    The LAN check in `pair()` is a stand-in for authorization: on the host's own
    network, being there is the proof. A redeemed enrolment code (enrolment.py)
    is that same proof carried explicitly, so it comes here instead — and only
    the *locational* rule is dropped. Everything else pairing enforces still
    holds, because none of it is about where the caller is: the host must own a
    mesh, the name must be one wg_peer will take, and an already-paired name
    gets its existing config back rather than a fresh keypair that would strand
    the entry the host is still carrying.

    `leaf=True` asks for the other artefact the same peer entry can carry. A
    *device* gets a wg-quick `.conf` its app loads; a *machine* joining the mesh
    permanently gets the install bundle, because it dials out under a
    LaunchDaemon rather than toggling a VPN — and the two are not interchangeable
    (`jrleaf.conf` is setconf-style and deliberately has no `Address` line).
    Handing a machine the client conf would look like a pairing and never come
    up, which is exactly the failure the enrolment path exists to remove.
    """
    _refuse_unless_pairable(device)
    already = existing_bundle(device) if leaf else existing_config(device)
    if already is not None:
        key = "bundle" if leaf else "config"
        return {"device": device, key: already, "created": False}

    add = ["add", "--leaf", device] if leaf else ["add", device]
    result = subprocess.run(
        [sys.executable, str(PEER_SCRIPT), *add],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise TunnelError(
            f"pairing {device} failed: {result.stderr.strip() or result.stdout.strip()}")

    if leaf:
        if not leaf_bundle_dir(device).is_dir():
            raise TunnelError(
                f"pairing {device} reported success but wrote no leaf bundle")
        return {"device": device, "bundle": _read_bundle(device), "created": True}

    path = CLIENTS_DIR / f"{device}.conf"
    if not path.exists():
        raise TunnelError(
            f"pairing {device} reported success but wrote no config at {path.name}")
    return {"device": device, "config": path.read_text(), "created": True}
