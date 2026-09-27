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
    wg_peer.py list                       show paired devices, with last handshake
    wg_peer.py remove <device-name>       revoke a device (a leaf needs --yes)
    wg_peer.py remove <machine-name> --yes  revoke a leaf, having read the warning
    wg_peer.py refresh                    re-copy the bringup scripts into every leaf bundle
    wg_peer.py prune --older-than <days> [--yes]
                                           list (--yes: remove) peers idle past the
                                           window; see `liveness()` for what "idle"
                                           can and cannot mean without a password

A leaf is a machine that joins the mesh by dialing out — same peer entry in
wg0.conf, but instead of a QR it gets a self-contained folder
(clients/<name>-leaf/) to copy over and `sudo ./install_leaf.sh` from:
setconf-style conf, the env-driven bringup scripts, and two LaunchDaemons.

Client private keys and QR PNGs land in Credentials/wireguard/clients/ (0600,
gitignored) so a device can be re-paired from the same QR without rotation.
Revoking removes the peer from the conf AND deletes the client files.

Env overrides (tests): WG_PEER_DIR, WG_BIN, WG_ENDPOINT, WG_SUBNET_PREFIX,
WG_SUDO_BIN, WG_NAME_FILE.
"""

import os
import re
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

_tree = Path(__file__).resolve().parents[2]
_default_credentials = _tree / "Credentials"
if str(_tree).endswith(".app/Contents/Resources/packages"):
    _default_credentials = Path(os.environ.get(
        "JREMOTE_CREDENTIALS_DIR", Path.home() / ".local/share/jremote/credentials"))
WG_DIR = Path(os.environ.get("WG_PEER_DIR", _default_credentials / "wireguard"))
WG_BIN = os.environ.get("WG_BIN", "/opt/homebrew/bin/wg")
SUDO_BIN = os.environ.get("WG_SUDO_BIN", "/usr/bin/sudo")
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
    _copy_leaf_scripts(bundle)
    _write_private(bundle / "README.md", _leaf_readme(name, ip))
    return bundle


def _copy_leaf_scripts(bundle):
    here = Path(__file__).resolve().parent
    for script in LEAF_SCRIPTS:
        target = bundle / script
        target.write_bytes((here / script).read_bytes())
        target.chmod(0o700)


def refresh():
    """Bring every existing leaf bundle's script copies up to the hub's own.

    A bundle is minted with a byte copy of the three bringup scripts, and the
    folder ships wholesale — so a script fixed here after the mint leaves the
    old copy waiting in every bundle on disk (the shipped-content scan grades
    them for exactly that). Keys, conf and env are not touched; a re-install
    from the bundle picks the refreshed scripts up, an installed leaf is not
    changed by this.
    """
    for bundle in sorted(CLIENTS.glob("*-leaf")):
        if not bundle.is_dir():
            continue
        stale = [s for s in LEAF_SCRIPTS
                 if not (bundle / s).exists()
                 or (bundle / s).read_bytes() != (Path(__file__).resolve().parent / s).read_bytes()]
        if stale:
            _copy_leaf_scripts(bundle)
        print(f"{bundle.name}: {'refreshed ' + ', '.join(stale) if stale else 'current'}")


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


#: The utun name file the hub tunnel daemon writes on startup — mirrors
#: `jstack_host.open_mode.wg_iface` without importing the package, since this
#: script ships standalone. WG_NAME_FILE overrides it for tests.
_HUB_NAME_FILE = "/var/run/wireguard/jremote-hub.name"


#: Why the last liveness read came back unknown — set by `_handshakes`, read
#: by the list and prune output so "unknown" names its cause rather than
#: guessing at sudo.
_unknown_reason = ""


def _iface() -> str:
    """The utun name the hub's tunnel landed on, or "" if it never came up —
    nothing for `_handshakes` to ask `wg show` about.

    The name file first; failing that, `sudo -n wg show interfaces`.  On a
    real hub the tunnel daemon writes its name file root-only (0400), so a
    non-root seat cannot read it even though the sudoers grant for `wg show`
    covers asking wg directly which interface it has.  Several interfaces
    (a hub that is also a leaf) are joined with spaces; `_handshakes` asks
    each."""
    path = os.environ.get("WG_NAME_FILE", _HUB_NAME_FILE)
    try:
        name = Path(path).read_text().strip()
    except OSError:
        name = ""
    if name:
        return name
    try:
        run = subprocess.run([SUDO_BIN, "-n", WG_BIN, "show", "interfaces"],
                             capture_output=True, text=True, timeout=5)
    except OSError:
        return ""
    if run.returncode != 0:
        return ""
    return " ".join(run.stdout.split())


def _handshakes(iface: str) -> dict[str, int] | None:
    """pubkey -> latest handshake (unix seconds, 0 = never), via the one read
    this seat can make without a password: `sudo -n wg show <iface>
    latest-handshakes` (operator issue 91). None on ANY failure — no iface, no
    NOPASSWD sudoers entry, `wg` missing — never `{}`: "could not ask" and
    "asked and heard nothing" must stay two different answers, or a caller
    reading None as empty would treat every peer as one it has evidence
    against. This never writes a sudoers entry; it only ever asks `-n`,
    which fails closed the moment there isn't one.
    """
    global _unknown_reason
    if not iface:
        _unknown_reason = ("no tunnel interface visible — the name file is unreadable "
                           "and sudo -n wg show interfaces was refused or empty")
        return None
    out: dict[str, int] = {}
    for one in iface.split():
        try:
            run = subprocess.run(
                [SUDO_BIN, "-n", WG_BIN, "show", one, "latest-handshakes"],
                capture_output=True, text=True, timeout=5)
        except OSError:
            _unknown_reason = f"{WG_BIN} could not be run"
            return None
        if run.returncode != 0:
            err = (run.stderr or run.stdout).strip().splitlines()
            _unknown_reason = ("sudo -n refused — no passwordless read configured"
                               if "password" in (err[-1] if err else "").lower()
                               else f"wg show {one} failed: {err[-1] if err else run.returncode}")
            return None
        for line in run.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("-").isdigit():
                out[parts[0]] = int(parts[1])
    _unknown_reason = ""
    return out


def liveness(pub: str, handshakes: dict[str, int] | None) -> str:
    """"unknown" (the sudo read failed outright), "never" (a read that
    succeeded and found no handshake for this key), or the handshake's unix
    time as a string. Deliberately never "dead": that is a retention call
    `prune` makes on top of this reading, not a fact this function alone has
    grounds to assert — conflating the two is exactly how an unreadable
    roster becomes a deletable one."""
    if handshakes is None:
        return "unknown"
    seen = handshakes.get(pub, 0)
    return "never" if seen == 0 else str(seen)


def _fmt_liveness(seen: str) -> str:
    if seen == "unknown":
        return f"unknown ({_unknown_reason or 'liveness read failed'})"
    if seen == "never":
        return "never"
    return datetime.fromtimestamp(int(seen)).strftime("%Y-%m-%d %H:%M")


def list_peers():
    _, peers = _peer_blocks()
    if not peers:
        print("no devices paired")
        return
    handshakes = _handshakes(_iface())
    for name, added, pub, ip in peers:
        seen = _fmt_liveness(liveness(pub, handshakes))
        print(f"{name}  {ip}  added {added}  last handshake: {seen}")


def prune(older_than_days, confirmed=False):
    """List (or, confirmed, remove) every peer idle past the window — never
    one this seat could not verify (operator issue 91). A peer this read cannot see
    is left alone regardless of age: "no sudoers entry yet" and "confirmed
    dead" must never be the same action, or the day that read is finally
    granted, its first act is deleting every peer prune had only ever been
    guessing about. `confirmed=False` (the default) removes nothing — it
    prints exactly what a `--yes` run would act on, and nothing this call
    does not print is ever touched by one.
    """
    _, peers = _peer_blocks()
    handshakes = _handshakes(_iface())
    cutoff = time.time() - older_than_days * 86400
    for name, added, pub, ip in peers:
        seen = liveness(pub, handshakes)
        if seen == "unknown":
            print(f"skip {name}: liveness unknown — {_unknown_reason or 'read failed'}; refusing to prune")
            continue
        if seen != "never" and int(seen) >= cutoff:
            continue                                      # handshake inside the window: alive
        verb = "removed" if confirmed else "would remove (re-run with --yes)"
        print(f"{verb} {name}  {ip}  added {added}  last handshake: {_fmt_liveness(seen)}")
        if confirmed:
            remove(name, confirmed=True)


def remove(name, confirmed=False):
    """Revoke a peer — destroys its keys and client files, no un-revoke.

    A leaf's bundle IS the machine's whole way back onto this mesh once it is
    off the LAN (jStack#55): the 2026-09-11 incident revoked work-mac's peer
    this way and recovery needed `adopt --offline` to be built first. `remove`
    on a leaf now refuses without `--yes` — a plain device (a phone, re-pairable
    on the spot) is unaffected, since losing it costs nothing this sharp.
    """
    interface, peers = _peer_blocks()
    keep = [p for p in peers if p[0] != name]
    if len(keep) == len(peers):
        raise SystemExit(f"no device {name!r}")
    if (CLIENTS / f"{name}-leaf").is_dir() and not confirmed:
        raise SystemExit(
            f"{name!r} is a leaf machine's own credential — its only way back "
            f"onto this mesh once it is off this LAN. Revoking destroys the "
            f"keys now, with no un-revoke; physical LAN access or "
            f"`adopt --offline` is the way back after. Re-run with --yes to "
            f"confirm.")
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
    elif cmd == "remove" and len(sys.argv) == 4 and sys.argv[3] == "--yes":
        remove(sys.argv[2], confirmed=True)
    elif cmd == "refresh" and len(sys.argv) == 2:
        refresh()
    elif cmd == "prune" and len(sys.argv) >= 4 and sys.argv[2] == "--older-than":
        rest = sys.argv[4:]
        if rest not in ([], ["--yes"]):
            raise SystemExit(__doc__)
        prune(int(sys.argv[3]), confirmed=(rest == ["--yes"]))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
