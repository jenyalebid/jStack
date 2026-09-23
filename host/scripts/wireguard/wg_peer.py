#!/usr/bin/env python3
"""WireGuard peer pairing — add/list/remove devices, emit client config + QR.

Runs unprivileged. Appending a peer to wg0.conf is all it takes: the signed
Network owner (`live.jstack.network`, /Library/PrivilegedHelperTools/jStack
Network.app) watches the conf and applies it to the live interface — no sudo,
no restart, no tunnel drop.

Named exactly, because the name is load-bearing during an outage. This used to
say "the hub's sync LaunchDaemon", which resolved to a legacy sync daemon label
retired when the Network owner took over; it now names nothing. On 2026-09-21,
mid-outage, that sentence sent a reader hunting a service that does not exist —
`launchctl print` returned empty, which reads as
"the sync is dead" rather than "you asked for the wrong label". Verify the real
owner with `launchctl print system/live.jstack.network` before concluding the
sync is down.

    wg_peer.py add <device-name>          pair a new device (prints QR path)
    wg_peer.py add --leaf <machine-name>  enrol a leaf host (emits an install bundle)
    wg_peer.py list                       show paired devices
    wg_peer.py remove <device-name>       revoke a device

A leaf is a machine that joins the mesh by dialing out — same peer entry in
wg0.conf, but instead of a QR it gets a self-contained folder
(clients/<name>-leaf/) to copy over and `sudo ./install_leaf.sh` from:
setconf-style conf, the env-driven bringup scripts, and two LaunchDaemons.

Client private keys and QR PNGs land in Credentials/wireguard/clients/ (0600,
gitignored) so a device can be re-paired from the same QR without rotation.
Revoking removes the peer from the conf AND deletes the client files.

Env overrides (tests): WG_PEER_DIR, WG_BIN, WG_ENDPOINT, WG_SUBNET_PREFIX.
"""

import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_tree = Path(__file__).resolve().parents[2]
_default_credentials = _tree / "Credentials"
if str(_tree).endswith(".app/Contents/Resources/packages"):
    _default_credentials = Path(os.environ.get(
        "JREMOTE_CREDENTIALS_DIR", Path.home() / ".local/share/jremote/credentials"))
WG_DIR = Path(os.environ.get("WG_PEER_DIR", _default_credentials / "wireguard"))
WG_BIN = os.environ.get("WG_BIN", "/opt/homebrew/bin/wg")
SUBNET_PREFIX = os.environ.get("WG_SUBNET_PREFIX", "10.66.0")  # server = .1
# Every issued tunnel pins a conservative MTU: at wireguard-go's 1420 default,
# any path narrower than ~1480 (cellular, VPN, CGNAT middleboxes) blackholes
# bulk traffic while the handshake still succeeds — a connection that looks up
# and dies on real payloads. 1240 inner + 60 encapsulation clears a 1300 path.
MTU = os.environ.get("WG_MTU", "1240")


def _endpoint() -> str:
    """Where clients dial this hub — `<WG_DIR>/endpoint`, or WG_ENDPOINT.

    Every hub has a different one, so it is deployment state next to the keys
    rather than a default in the source: a literal here is one machine's
    address compiled into every other machine's installer. Missing is a hard
    error at pairing time, not a silent wrong address — a client conf carrying
    someone else's endpoint fails as "no handshake", which reads as a network
    problem and is not one.
    """
    env = os.environ.get("WG_ENDPOINT")
    if env:
        return env
    path = WG_DIR / "endpoint"
    if path.exists():
        value = path.read_text().strip()
        if value:
            return value
    sys.exit(f"no tunnel endpoint configured — write host:port to {path} "
             f"(the address clients dial to reach this hub), or set WG_ENDPOINT")




CONF = WG_DIR / "wg0.conf"
SERVER_PUB = WG_DIR / "server.pub"
CLIENTS = WG_DIR / "clients"

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
PEER_HEADER_RE = re.compile(r"^# device: (\S+) ")


def _wg(*args, stdin=None):
    return subprocess.run(
        [WG_BIN, *args], input=stdin, capture_output=True, text=True, check=True
    ).stdout.strip()


def _write_private(path, content):
    path.write_text(content)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _peer_blocks():
    """Parse wg0.conf into (interface_text, [(name, added, pubkey, ip)])."""
    text = CONF.read_text()
    parts = text.split("# device: ")
    peers = []
    for chunk in parts[1:]:
        header, _, body = chunk.partition("\n")
        name, _, added = header.partition(" added ")
        pub = re.search(r"PublicKey\s*=\s*(\S+)", body)
        ip = re.search(rf"AllowedIPs\s*=\s*({re.escape(SUBNET_PREFIX)}\.\d+)/32", body)
        peers.append((name, added, pub.group(1) if pub else "?", ip.group(1) if ip else "?"))
    return parts[0], peers


def _next_ip(peers):
    used = {int(ip.rsplit(".", 1)[1]) for _, _, _, ip in peers if ip != "?"} | {1}
    for octet in range(2, 255):
        if octet not in used:
            return f"{SUBNET_PREFIX}.{octet}"
    raise SystemExit("subnet full")


LEAF_SCRIPTS = ("wg_up.sh", "wg_leaf_watch.sh", "install_leaf.sh")


def _leaf_readme(name, ip):
    return (
        f"# jRemote leaf — {name}\n"
        f"\n"
        f"This machine dials out to the hub's WireGuard endpoint and joins the\n"
        f"mesh as {ip}. Nothing listens publicly — no port-forward, no inbound\n"
        f"path. Prereq (once): `brew install wireguard-go wireguard-tools`.\n"
        f"\n"
        f"1. `sudo ./install_leaf.sh` — installs the tunnel LaunchDaemons,\n"
        f"   brings it up, verifies the handshake and pings the hub.\n"
        f"2. Install the jRemote host payload (the shared \"jRemote Host\"\n"
        f"   folder) per its own README — note the token it prints.\n"
        f"3. In the app on any device: Instances > Add a Mac — address\n"
        f"   `http://{ip}:9090`, the token from step 2.\n"
        f"\n"
        f"Devices other than the hub reach this machine through the hub, which\n"
        f"needs forwarding on — re-run `sudo scripts/wireguard/install_hub.sh`\n"
        f"there after upgrading it. If a corporate VPN claims routes over the\n"
        f"mesh subnet, the mesh loses.\n"
    )


def _emit_leaf_bundle(name, ip, client_key, server_pub):
    """A self-contained folder the leaf machine installs from.

    The conf is setconf-style — no `Address` or `MTU` line, which `wg setconf`
    rejects as wg-quick syntax; both travel in leaf.env and wg_up.sh puts them
    on the interface. The bringup scripts are the hub's own, copied in: every
    path in them is env-overridable, and the installer writes those envs into
    the LaunchDaemons it generates.
    """
    bundle = CLIENTS / f"{name}-leaf"
    bundle.mkdir(mode=0o700, exist_ok=True)
    _write_private(
        bundle / "jrleaf.conf",
        f"[Interface]\n"
        f"PrivateKey = {client_key}\n"
        f"\n"
        f"[Peer]\n"
        f"PublicKey = {server_pub}\n"
        f"AllowedIPs = {SUBNET_PREFIX}.0/24\n"
        f"Endpoint = {_endpoint()}\n"
        f"PersistentKeepalive = 25\n",
    )
    # Owner-only throughout, scripts included. Nothing in this bundle is the
    # group's or the world's business: it is written INSIDE the credential
    # store, next to the private key above, and `ops_hygiene_audit`'s
    # credential_containment check holds the whole store to that (issue #94).
    # 0755 here meant every new leaf re-opened a handful of paths in the store
    # the night after the audit closed them. The leaf still runs them — a
    # 0700 script is executable by the account that unpacks the bundle, which
    # is the only account that ever runs it.
    _write_private(
        bundle / "leaf.env",
        f"WG_ADDR={ip}/32\n"
        f"WG_SUBNET={SUBNET_PREFIX}.0/24\n"
        f"WG_HUB={SUBNET_PREFIX}.1\n"
        f"WG_MTU={MTU}\n",
    )
    here = Path(__file__).resolve().parent
    for script in LEAF_SCRIPTS:
        target = bundle / script
        target.write_bytes((here / script).read_bytes())
        target.chmod(0o700)
    _write_private(bundle / "README.md", _leaf_readme(name, ip))
    return bundle


def _write_qr(name, client_conf):
    """The pairing QR, or None on a host with no QR library.

    Rendering a PNG needs `qrcode` and its imaging stack, which the host
    payload deliberately does not carry — it is six wheels for one picture, on
    an install whose whole point is that it needs nothing from the network.
    Best-effort rather than required, because by the time this runs the peer is
    already in wg0.conf: raising here would leave the caller a half-pairing
    (an entry the hub honours, no config handed back) over a missing
    convenience. The conf is the credential; the QR is a way to type it.
    """
    try:
        import qrcode
    except ImportError:
        return None
    qr_path = CLIENTS / f"{name}.png"
    qrcode.make(client_conf).save(str(qr_path))
    qr_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return qr_path


def add(name, leaf=False):
    if not NAME_RE.match(name):
        raise SystemExit(f"bad device name {name!r} (lowercase, digits, dashes)")
    _, peers = _peer_blocks()
    if any(p[0] == name for p in peers):
        raise SystemExit(f"device {name!r} already paired — remove it first")

    ip = _next_ip(peers)
    client_key = _wg("genkey")
    client_pub = _wg("pubkey", stdin=client_key)
    server_pub = SERVER_PUB.read_text().strip()
    added = datetime.now().strftime("%Y-%m-%d")

    with CONF.open("a") as f:
        f.write(
            f"\n# device: {name} added {added}\n"
            f"[Peer]\n"
            f"PublicKey = {client_pub}\n"
            f"AllowedIPs = {ip}/32\n"
        )

    if leaf:
        CLIENTS.mkdir(mode=0o700, exist_ok=True)
        bundle = _emit_leaf_bundle(name, ip, client_key, server_pub)
        print(f"paired {name} at {ip}")
        print(f"leaf bundle: {bundle}")
        print("copy the folder to the machine and run: sudo ./install_leaf.sh")
        return

    client_conf = (
        f"[Interface]\n"
        f"PrivateKey = {client_key}\n"
        f"Address = {ip}/32\n"
        f"MTU = {MTU}\n"
        f"\n"
        f"[Peer]\n"
        f"PublicKey = {server_pub}\n"
        f"AllowedIPs = {SUBNET_PREFIX}.0/24\n"
        f"Endpoint = {_endpoint()}\n"
        f"PersistentKeepalive = 25\n"
    )
    CLIENTS.mkdir(mode=0o700, exist_ok=True)
    conf_path = CLIENTS / f"{name}.conf"
    _write_private(conf_path, client_conf)

    print(f"paired {name} at {ip}")
    qr_path = _write_qr(name, client_conf)
    if qr_path:
        print(f"qr: {qr_path}")
    else:
        print("qr: skipped — no qrcode module on this host; the conf is the pairing")
    print(f"conf: {conf_path}")


def list_peers():
    _, peers = _peer_blocks()
    if not peers:
        print("no devices paired")
        return
    for name, added, _, ip in peers:
        print(f"{name}  {ip}  added {added}")


def remove(name):
    interface, peers = _peer_blocks()
    keep = [p for p in peers if p[0] != name]
    if len(keep) == len(peers):
        raise SystemExit(f"no device {name!r}")
    out = interface
    for pname, added, pub, ip in keep:
        out += (
            f"# device: {pname} added {added}\n"
            f"[Peer]\n"
            f"PublicKey = {pub}\n"
            f"AllowedIPs = {ip}/32\n\n"
        )
    _write_private(CONF, out.rstrip("\n") + "\n")
    for suffix in (".conf", ".png"):
        (CLIENTS / f"{name}{suffix}").unlink(missing_ok=True)
    # A leaf's material is a folder, not the .conf/.png a device gets; leaving it
    # behind keeps a revoked machine's private key and install bundle on disk,
    # re-pairable from the same key. Revoke means gone.
    shutil.rmtree(CLIENTS / f"{name}-leaf", ignore_errors=True)
    print(f"removed {name}")


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    cmd = sys.argv[1]
    if cmd == "add" and len(sys.argv) == 3:
        add(sys.argv[2])
    elif cmd == "add" and len(sys.argv) == 4 and sys.argv[2] == "--leaf":
        add(sys.argv[3], leaf=True)
    elif cmd == "list":
        list_peers()
    elif cmd == "remove" and len(sys.argv) == 3:
        remove(sys.argv[2])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
