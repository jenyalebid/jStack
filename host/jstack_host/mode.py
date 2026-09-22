"""Which of the three shapes this host is, keyed on how an off-LAN device reaches it.

  · **local**   — nothing off the LAN can reach it. Dials out on no tunnel,
    publishes no way in. Where a fresh install lands.
  · **open**    — this Mac itself publishes a WireGuard endpoint the outside
    can dial.
  · **managed** — dials OUT to a parent hub and rides its mesh, so devices
    paired to that parent reach here with no setup of their own. A leaf.

Read from what the machine *is*, never from a setting it was told to believe:
managed when a leaf tunnel is installed (and this is not itself a hub) or when
it sits on the mesh without owning it; open when it owns the mesh AND publishes
an endpoint; local otherwise.

Mesh ownership is two independent facts, either sufficient: `tunnel.can_pair()`
and holding the mesh gateway `10.66.0.1` on an interface (leaves get `.2` up).
can_pair alone was jStack#42 — it answers no whenever `wg0.conf` is outside
this package's tree, which demoted a Mac that owned the mesh to a leaf.

Two limits are deliberate, both the difference between a check and a claim.
A leaf whose tunnel is momentarily down stays *managed* — attachment is a fact
on disk; the separate `live` flag carries whether the mesh address is up now.
And an open host publishes an endpoint, which is all this Mac can see by
itself; `verified` carries whether an off-network handshake was actually
observed (`open_mode.verify`).

`classify` is pure so the tests pin the rules against fixed facts; `current` is
the one place that reads this machine's own, through `addresses`, `tunnel` and
the same endpoint locations `wg_peer.py` honours — so a host that moved
`WG_DIR` is judged by what the tunnel tool would do, not by a guess.
"""

from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path

from . import addresses, hostenv, tunnel

#: The LaunchDaemon `install_leaf.sh` drops on a machine it turns into a leaf
#: (`/Library/LaunchDaemons/com.jremote.leaf.plist`). Presence is the durable
#: "this Mac is attached to a parent" record — it outlives the interface being
#: up, which is exactly the property the mode needs and the mesh address lacks.
#: A constant so a test can point it at a tmp file instead of the real root.
LEAF_PLIST = Path("/Library/LaunchDaemons/com.jremote.leaf.plist")


def _leaf_installed() -> bool:
    """Whether a leaf tunnel is installed on this machine — the durable record,
    read without caring whether the daemon is loaded this second."""
    return LEAF_PLIST.is_file()


def _parent_url() -> str:
    """Which hub this machine is attached to, or "".

    Read off `parent.json` — the record `attach` writes and `detach` drops, so
    it is the same file that decides whether this machine is attached at all.
    Only the URL is taken: that record also holds the device token this Mac
    holds on its parent, and the mode is served to every device that can read
    `/host`.

    Worth carrying because "managed" without a parent is a mode with no object.
    A menu that says a Mac is managed and cannot say by what leaves the one
    question it raised unanswered, and the answer is already on disk.
    """
    try:
        raw = json.loads((hostenv.state_dir() / "parent.json").read_text())
    except (OSError, ValueError):
        return ""
    url = raw.get("parent_url") if isinstance(raw, dict) else ""
    return url if isinstance(url, str) else ""


def _on_mesh(inets: list[str]) -> bool:
    """Whether one of this machine's own interfaces holds a mesh address right
    now — the liveness half of the leaf story, and what tells a hub from a
    machine that merely has the tooling."""
    for raw in inets:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if ip in addresses.MESH_SUBNET:
            return True
    return False


def _mesh_gateway() -> ipaddress.IPv4Address:
    """The mesh subnet's gateway — the address the hub itself holds.

    Derived from `addresses.MESH_SUBNET` rather than written out, so moving the
    subnet moves this with it. A second literal `10.66.0.1` in this file would
    keep passing after the real one moved, which is the failure the check
    exists to prevent."""
    return next(addresses.MESH_SUBNET.hosts())


def _owns_mesh_gateway(inets: list[str]) -> bool:
    """Whether this machine holds the mesh gateway address — proof it *is* the
    hub, not a machine dialled into one.

    The interface half of the hub question. `tunnel.can_pair()` is the other
    half and answers a narrower one: whether this package can find the peer
    list well enough to edit it. A hub whose wireguard state lives outside the
    package tree fails that and still owns the mesh."""
    gateway = _mesh_gateway()
    for raw in inets:
        try:
            if ipaddress.ip_address(raw) == gateway:
                return True
        except ValueError:
            continue
    return False


def _endpoint_declared() -> bool:
    """Whether this host publishes a dial-in endpoint, read the same two places
    `wg_peer.py._endpoint()` reads — the env first, then `<WG_DIR>/endpoint`.

    Mirrored rather than imported for the reason the rest of this package
    mirrors that file: importing it runs a tool that edits the live tunnel, and
    a mode readout is no reason to do that."""
    if os.environ.get("WG_ENDPOINT"):
        return True
    return (tunnel.WG_DIR / "endpoint").is_file()


def classify(*, leaf_installed: bool, on_mesh: bool,
             is_hub: bool, endpoint: bool, off_net_verified: bool = False,
             owns_mesh_gateway: bool = False, parent: str = "") -> dict:
    """The taxonomy, from a handful of facts and nothing else. Pure on purpose.

    `off_net_verified` is the one that turns open mode's declaration into a
    claim: it is True only when an off-network handshake has actually been
    observed (open_mode.verify). An open host without it publishes an endpoint
    but has not been shown to be reachable through it, and says exactly that.

    `owns_mesh_gateway` is the second, independent proof of a hub — holding
    `10.66.0.1` on an interface. Either it or `is_hub` settles the question, so
    a hub is never demoted to a leaf of itself because this package could not
    find the peer list it does not administer.

    `parent` rides along on the managed answers and is empty everywhere else —
    it is the hub this machine dialled out to, and it says nothing about a
    machine that dialled out to nobody.
    """
    is_hub = is_hub or owns_mesh_gateway
    named = f" ({parent})" if parent else ""
    if leaf_installed and not is_hub:
        return {
            "mode": "managed",
            "live": on_mesh,
            "parent": parent,
            "note": (f"attached to a parent hub{named}; the mesh tunnel is up"
                     if on_mesh else
                     f"attached to a parent hub{named}, but the mesh tunnel is "
                     "DOWN — not reachable through the parent right now"),
        }
    if on_mesh and not is_hub:
        return {
            "mode": "managed",
            "live": True,
            "parent": parent,
            "note": "on a parent hub's mesh (no local leaf install record)",
        }
    if is_hub and endpoint:
        return {
            "mode": "open",
            "live": True,
            "verified": off_net_verified,
            "note": ("publishes a WireGuard endpoint and an off-network device "
                     "has reached it — reachable off-network, verified"
                     if off_net_verified else
                     "publishes a WireGuard endpoint for off-network access — "
                     "reachability is declared here, not yet verified (run "
                     "`jstack-host open` to prove it)"),
        }
    return {
        "mode": "local",
        "live": True,
        "note": "reachable only on this network",
    }


def is_hub() -> bool:
    """Mesh ownership, independent of which UI asks the question.

    A legacy installation may own the gateway without its peer administration
    being visible to this process. Neither a private IP nor a leaf's loopback
    proves that ownership. The request-level gate also refuses attached leaves.
    """
    inets = addresses._inet_addrs()
    return tunnel.can_pair() or _owns_mesh_gateway(inets)


def is_managed() -> bool:
    """The authority boundary without running an off-network reachability probe."""
    if tunnel.can_pair():
        return False
    inets = addresses._inet_addrs()
    return not _owns_mesh_gateway(inets) and (_leaf_installed() or _on_mesh(inets))


def current() -> dict:
    """This machine's mode, read off its own interfaces and tunnel state.

    The off-network verification is only consulted for a host that could be open
    — a hub with an endpoint — because reading it shells out to `wg`, and a
    local or managed host has no open claim to verify."""
    inets = addresses._inet_addrs()
    owns_gateway = _owns_mesh_gateway(inets)
    is_hub = tunnel.can_pair() or owns_gateway
    endpoint = _endpoint_declared()
    verified = False
    if is_hub and endpoint:
        from . import open_mode
        verified = open_mode.verify()["verified"]
    return classify(
        leaf_installed=_leaf_installed(),
        on_mesh=_on_mesh(inets),
        is_hub=is_hub,
        endpoint=endpoint,
        off_net_verified=verified,
        owns_mesh_gateway=owns_gateway,
        parent=_parent_url(),
    )
