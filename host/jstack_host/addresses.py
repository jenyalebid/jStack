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
  · **local** — the names. This host's configured domain first if it has one
    (`hub_domain`), then the Bonjour name. Both survive a DHCP move, which the
    numeric LAN address does not; they come second because name resolution is
    flakier than a number on a guest network, and a device standing in front of
    the Mac should not wait on DNS to pair. A device that has already left is
    the one they exist for — see `hub_domain`.
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

import concurrent.futures
import ipaddress
import os
import re
import socket
import subprocess
import threading
import time

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

#: This host's own stable name, if its owner gave it one — `JSTACK_HUB_DOMAIN`,
#: else a single line in `<state_dir>/hub-domain`. Empty on a host that has
#: none, which is every host by default: the package ships no domain, the
#: machine supplies one.
_DOMAIN_ENV = "JSTACK_HUB_DOMAIN"
_DOMAIN_FILE = "hub-domain"


def hub_domain() -> str:
    """A name that resolves to this host's LAN address, kept true by DNS.

    This is the fix for the failure that outlived every other one here: a
    device pairs at home, stores the Mac's LAN *number*, the Mac's address then
    moves — three times in three days, on this machine — and the device is left
    dialing a host that is no longer there. It cannot relearn, because
    relearning means reaching the hub, and reaching the hub is what broke. A
    number frozen inside twenty-three devices is unfixable by definition.

    A name is fixable. The number lives in one DNS record the hub itself
    rewrites when it moves, and every device re-resolves on its own with no
    contact, no re-pairing and no new build.

    It is published as `local` rather than a kind of its own, and that is not
    an accident: `local` in this protocol means *a name, not a number* — the
    kind whose note is already "survives this Mac changing address", which a
    domain does more completely than Bonjour ever did. Clients in the field
    filter the address list to `lan` and `local`, so a new kind would be
    dropped by every app already installed and deliver nothing until a build
    shipped. This one is picked up on the next `/host` fetch by devices paired
    months ago.
    """
    env = os.environ.get(_DOMAIN_ENV, "").strip()
    if env:
        return env
    try:
        from . import hostenv
        return (hostenv.state_dir() / _DOMAIN_FILE).read_text().strip()
    except Exception:  # noqa: BLE001 — no file, no domain, no claim
        return ""


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


#: How long one request may wait on the Bonjour lookup. mDNS answers in
#: milliseconds when it answers at all; an in-process lookup from the sealed
#: Hub waited ~35s on a Local Network prompt nobody answered (see `_mdns_lookup`),
#: and `/host` — the first call every client makes, and the one the installer's
#: health check waits on — hung for exactly that long behind it, so every fresh
#: install read "did not become healthy" (26.9.3 proof). No lookup blocks a
#: request past this.
RESOLVE_DEADLINE = 1.0
#: The subprocess bound on one `dscacheutil` lookup.
MDNS_TIMEOUT = 5.0
#: How long an answer stands before it is looked up again.
RESOLVE_TTL = 300.0

_RESOLVE_LOCK = threading.Lock()
_RESOLVED: dict[str, tuple[float, str | None]] = {}
_PENDING: dict[str, concurrent.futures.Future] = {}


def _mdns_lookup(name: str) -> str:
    """The first IPv4 `name` resolves to, asked of `dscacheutil`, not of this process.

    Resolving a `.local` name is a Local Network access, and macOS bills it to
    the asking process's bundle. Inside the sealed Hub that is `live.jstack.hub`,
    whose Info.plist declares `NSLocalNetworkUsageDescription`: mDNSResponder
    answers "Local network access to query policy 'denied' for
    (live.jstack.hub)" and `gethostbyname` fails with Errno 8 — after ~35s the
    first time, while the permission prompt waits — so the jStack#41 check read
    "no evidence" forever on every sealed hub (jStack#226, proven in a 26.6.2
    guest). `dscacheutil` is a platform binary: the same query through it is
    not policed, answers in milliseconds, and does not raise a Local Network
    prompt for a lookup of this Mac's own name."""
    try:
        out = subprocess.run(["/usr/bin/dscacheutil", "-q", "host", "-a", "name", name],
                             capture_output=True, text=True, timeout=MDNS_TIMEOUT).stdout
    except subprocess.SubprocessError as exc:
        raise OSError(str(exc)) from exc
    for line in out.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "ip_address" and value.strip():
            return value.strip()
    raise OSError(f"{name} did not resolve")


def _lookup(name: str, resolver) -> str | None:
    try:
        return resolver(name)
    except OSError:
        return None


def _start_lookup(name: str, resolver) -> concurrent.futures.Future:
    """The lookup on a thread of its own — a daemon, and that is the point.

    An executor's workers are joined at interpreter exit, so a one-shot
    command that asked once (`pair --json`, which the menu bar runs and waits
    on) sat at its exit for the resolver's whole 35s failure after it had
    printed its answer in one second — Get a Code showed no dialog for 35s
    and full/pair read no code off it (26.9.4 proof). A daemon thread dies
    with the process; the answer a still-running lookup would have landed is
    one nobody was left to read."""
    future: concurrent.futures.Future = concurrent.futures.Future()

    def work() -> None:
        future.set_result(_lookup(name, resolver))
    threading.Thread(target=work, name=f"mdns:{name}", daemon=True).start()
    return future


def _resolve_local(name: str, *, resolver=_mdns_lookup,
                   deadline: float = RESOLVE_DEADLINE) -> str | None:
    """What `name` resolves to right now, or None on any failure — bounded.

    A seam, like `_hostname`/`_inet_ifaces`: real mDNS resolution is network
    IO the classifier below stays free of. None is "no evidence either way",
    never grounds to drop the entry — only a resolved address this machine
    itself holds on a host-only interface is (jStack#41).

    The lookup runs off the request: one worker owns it, a call waits at most
    `deadline` for it, and a lookup still running answers None now and serves
    the calls that come after it lands. An answer is kept for `RESOLVE_TTL`,
    so a resolver that takes 35s to fail costs one request one second every
    five minutes, never every request its whole wait."""
    now = time.monotonic()
    with _RESOLVE_LOCK:
        cached = _RESOLVED.get(name)
        if cached and now - cached[0] < RESOLVE_TTL:
            return cached[1]
        pending = _PENDING.get(name)
        if pending is None:
            pending = _PENDING[name] = _start_lookup(name, resolver)
    try:
        answer = pending.result(timeout=deadline)
    except concurrent.futures.TimeoutError:
        return None
    with _RESOLVE_LOCK:
        _RESOLVED[name] = (time.monotonic(), answer)
        if _PENDING.get(name) is pending:
            del _PENDING[name]
    return answer


def _forget_resolved() -> None:
    """Drop every cached and pending answer — for tests."""
    with _RESOLVE_LOCK:
        _RESOLVED.clear()
        _PENDING.clear()


def classify(inets: list[str], hostname: str, port: int,
             ifaces: dict[str, str] | None = None, domain: str = "",
             mesh_port: int | None = None,
             resolve_local=None) -> list[dict]:
    """The address list, ordered lan → local → mesh. Pure, so the ordering
    and the exclusions are what the tests actually pin.

    `mesh_port` defaults to `port` and exists because the two are not always
    the same number. `port` is the port the caller reached us on, which is the
    right one to hand back for the addresses the caller could reach the same
    way. The mesh address is not one of those: it is served by this process's
    own listener, so its port is a property of this host and survives nothing
    else. When a caller arrives through a forward — an ssh `-L 9091:…:9090`,
    a reverse proxy — the reached port is the forward's, and stamping it onto
    the mesh entry publishes an address that has never had anything behind it
    (proven: device run 8 — the phone pinned `http://10.66.0.1:9091` off the
    LAN and drew "Could not connect to the server." while the hub's only
    listener sat on 9090).

    Every entry here is reachable only from this LAN or from something already
    on this mesh. Nothing in this list gets a machine that is neither — see
    `adopt`, which has to say so rather than offer three addresses that will
    all time out.

    `ifaces` maps address → the interface holding it, and is what lets a VM
    bridge be told apart from the real LAN. Omitted, every address is treated
    as network-facing: an interface map is extra evidence for dropping an
    entry, never a precondition for keeping one.

    `resolve_local` answers what the Bonjour name resolves to right now — the
    same VM-bridge ambiguity `ifaces` resolves for a bare address applies to
    the name too (jStack#41): a Mac running VMs can have its own `.local` name
    answer from `bridge100`'s mDNS responder as easily as the real LAN one, and
    the address alone cannot tell them apart. Omitted or unresolvable, the
    entry is kept — no evidence is not evidence against it.
    """
    mesh_port = port if mesh_port is None else mesh_port
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

    # The tunnel address, published under `lan` rather than its own kind.
    #
    # Not cosmetic, and not a lie about what it is — it is the only tag that
    # reaches a device already in the field. Shipped clients filter this list
    # to `lan` and `local` and drop everything else, which is stated forty
    # lines up about the domain and then contradicted at the bottom of this
    # same function, where the mesh entry goes out under `mesh` and is deleted
    # by every installed app before a human sees it.
    #
    # What that cost, concretely: on 2026-09-18 this Mac's LAN address moved
    # and three devices lost the hub for three days. The address that would
    # have saved them was computed, serialised, sent, and thrown away at the
    # client. Meanwhile the one entry those clients *did* keep — the domain,
    # published as `local` — is refused by iOS App Transport Security, because
    # a real public name over plain http gets no local-networking exemption.
    # So the list contained exactly one address the app would accept and one
    # the OS would accept, and they were never the same entry.
    #
    # A private 10.66/24 address is exempt under NSAllowsLocalNetworking, so
    # this one clears ATS, and under `lan` it clears the client filter too. It
    # is emitted after the real LAN addresses on purpose: at home with the
    # tunnel down, the LAN entry still answers first and nothing waits on a
    # timeout; away, the LAN entry fails and this is the next thing tried.
    for addr in mesh:
        out.append({"kind": "lan", "host": addr,
                    "url": f"http://{addr}:{mesh_port}",
                    "note": "works from anywhere this device's tunnel is up"})

    # The configured domain leads the names: it survives a move the Bonjour
    # name also survives, and additionally works on a network where `.local`
    # resolution is blocked — which is most guest and corporate Wi-Fi.
    stable = (domain or "").strip().rstrip(".").lower()
    if stable:
        out.append({"kind": "local", "host": stable,
                    "url": f"http://{stable}:{port}",
                    "note": "this Mac's permanent name on this network"})

    name = (hostname or "").strip().rstrip(".")
    if name and name.lower() != "localhost":
        if not name.endswith(".local"):
            name = name.split(".")[0] + ".local"
        resolved = resolve_local(name) if resolve_local else None
        on_bridge = resolved is not None and _is_host_only((ifaces or {}).get(resolved, ""))
        if name.lower() != stable and not on_bridge:
            out.append({"kind": "local", "host": name,
                        "url": f"http://{name}:{port}",
                        "note": "survives this Mac changing address"})

    for addr in mesh:
        out.append({"kind": "mesh", "host": addr,
                    "url": f"http://{addr}:{mesh_port}",
                    "note": "for a device already paired onto this Mac's "
                            "tunnel"})
    return out


def reachable(port: int = DEFAULT_PORT, mesh_port: int | None = None) -> list[dict]:
    """Where a second machine could try to reach this one."""
    held = _inet_ifaces()
    return classify(list(held), _hostname(), port, held, hub_domain(),
                    mesh_port=mesh_port, resolve_local=_resolve_local)
