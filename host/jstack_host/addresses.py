"""Where this host can be reached — the answer a pairing screen has to show.

Pairing a second machine means typing an address into it, and the address the
app already holds is the one that is guaranteed wrong: on the Mac that mints
the code, the app is talking to loopback. `http://127.0.0.1:9090` is true for
the machine saying it and false for every machine being told it, so a dialog
that echoed its own base URL would hand out an address that cannot work and
looks like it should. That failure is silent on the minting side and total on
the receiving one, which is the worst shape a setup step can have.

So the host answers for itself, off its own interfaces, in the order a reader
should try them:

  · **lan** — this network's private IPv4, on an interface that actually
    faces the network. True while both machines are on the same network,
    which is where pairing happens: the device being enrolled is, by
    definition, not on the mesh yet. First because it is the one address a
    new device can act on *now*.
  · **local** — the Bonjour name. Survives a DHCP move, which the numeric LAN
    address does not; second because `.local` resolution is flakier than a
    number on a guest network.
  · **mesh** — `10.66.0.x`. The address a device ends up *using* once its
    tunnel is up — and the one a device that is still pairing can never
    reach. Last, and only present on a host that has a tunnel at all: a
    pairing screen that led with it handed every new device its own
    unreachable first try, which is exactly what this module exists to stop.

**Loopback is deliberately absent.** It is never useful to a second machine,
and an address list whose first entry cannot work teaches the reader to
distrust the rest of it. The app pairing a host to ITSELF already has
loopback without being told (`LanProbe.localHost`), and that path does not
come through here.

**So are host-only interfaces**, for the same reason. A Mac running VMs holds
something like `192.168.64.1` on `bridge100`: private, routable, and visible
to nothing but the guests on that bridge. Reading the address alone cannot
tell it apart from the real LAN — both are RFC1918 — so the address alone is
not enough to classify on, and a list built that way hands a phone an entry
captioned "works while both machines are on this network" that no phone can
ever reach. The interface it sits on is the signal that separates them.

Nothing here claims reachability. These are addresses this machine *holds* —
whether a packet from somewhere else arrives on one is a fact about the
network, and this module would be lying if it implied otherwise. The pairing
screen says "try these", never "these work".
"""

from __future__ import annotations

import ipaddress
import re
import socket
import subprocess

#: The mesh subnet, mirrored from `wg_peer.py`'s SUBNET_PREFIX the same way
#: `devices.MESH_SUBNET` mirrors it. A host with no tunnel simply never has an
#: address inside it, so this needs no guard for the leaf case.
MESH_SUBNET = ipaddress.ip_network("10.66.0.0/24")

#: `inet 192.168.0.106 netmask 0xffffff00` — BSD `ifconfig`, which is what
#: every machine this package installs on runs. A parse that finds nothing
#: degrades to an empty list, and the screen above says so rather than
#: inventing an address.
_INET_RE = re.compile(r"^\s*inet\s+(\d+\.\d+\.\d+\.\d+)", re.M)

#: `ifconfig` starts an interface's block at column zero: `en1: flags=...`.
_IFACE_RE = re.compile(r"^(\w+):", re.M)

#: Interface families that hold a private address nothing off this Mac can
#: route to. `bridge` is macOS virtualisation (the VM host-only network),
#: `vmenet`/`vnic` the guest-side members of it, `awdl`/`llw` Apple's
#: peer-to-peer radios. Matched as prefixes because the kernel numbers them —
#: `bridge100`, `bridge101` — and a new number must not quietly reopen this.
#:
#: `utun` is deliberately NOT here. The mesh lives on one, and it is excluded
#: from `lan` by subnet already, then re-added as its own `mesh` kind. Listing
#: it would delete the mesh address instead of reclassifying it.
_HOST_ONLY_IFACES = ("bridge", "vmenet", "vnic", "awdl", "llw")

DEFAULT_PORT = 9090


def _inet_addrs() -> list[str]:
    """Every IPv4 this machine holds. A seam, so the tests drive the
    classifier against fixed interface sets instead of the host's own.

    Deliberately unfiltered: `mode` reads this to ask whether this machine
    holds the mesh gateway, and that question is about every interface, not
    the ones a pairing screen should advertise.
    """
    return list(_inet_ifaces())


def _inet_ifaces() -> dict[str, str]:
    """Each IPv4 this machine holds, mapped to the interface holding it.

    A second seam rather than a change to `_inet_addrs`, because its callers
    want different things: `mode` wants the addresses, this module wants to
    know which ones face the network. Unparseable output degrades to `{}`,
    and `classify` treats an address it has no interface for as a real LAN
    address — the pre-existing behaviour, so a parse failure narrows what we
    can exclude without silently emptying the list.
    """
    try:
        out = subprocess.run(["/sbin/ifconfig"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001 — no interfaces readable is an empty list
        return {}
    found: dict[str, str] = {}
    iface = ""
    for line in out.splitlines():
        head = _IFACE_RE.match(line)
        if head:
            iface = head.group(1)
            continue
        addr = _INET_RE.match(line)
        if addr:
            found[addr.group(1)] = iface
    return found


def _hostname() -> str:
    """A seam for the same reason `_inet_addrs` is one."""
    try:
        return socket.gethostname()
    except Exception:  # noqa: BLE001
        return ""


def _is_host_only(iface: str) -> bool:
    """Whether an interface faces only this Mac and its guests."""
    return iface.startswith(_HOST_ONLY_IFACES)


def classify(inets: list[str], hostname: str, port: int,
             ifaces: dict[str, str] | None = None) -> list[dict]:
    """The address list, ordered lan → local → mesh. Pure, so the ordering
    and the exclusions are what the tests actually pin.

    Every entry here is reachable only from this LAN or from something already
    on this mesh. Nothing in this list gets a machine that is neither — see
    `adopt`, which has to say so rather than offer three addresses that will
    all time out.

    `ifaces` maps address → the interface holding it, and is what lets a VM
    bridge be told apart from the real LAN. Omitted, every address is treated
    as network-facing: an interface map is extra evidence for dropping an
    entry, never a precondition for keeping one.
    """
    out: list[dict] = []
    mesh, lan = [], []
    for raw in inets:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or not ip.is_private:
            continue
        if ip in MESH_SUBNET:
            mesh.append(str(ip))
        elif not _is_host_only((ifaces or {}).get(raw, "")):
            lan.append(str(ip))

    for addr in lan:
        out.append({"kind": "lan", "host": addr,
                    "url": f"http://{addr}:{port}",
                    "note": "works while both machines are on this network"})

    name = (hostname or "").strip().rstrip(".")
    if name and name.lower() != "localhost":
        if not name.endswith(".local"):
            name = name.split(".")[0] + ".local"
        out.append({"kind": "local", "host": name,
                    "url": f"http://{name}:{port}",
                    "note": "survives this Mac changing address"})

    for addr in mesh:
        out.append({"kind": "mesh", "host": addr,
                    "url": f"http://{addr}:{port}",
                    "note": "for a device already paired onto this Mac's "
                            "tunnel"})
    return out


def reachable(port: int = DEFAULT_PORT) -> list[dict]:
    """Where a second machine could try to reach this one."""
    held = _inet_ifaces()
    return classify(list(held), _hostname(), port, held)
