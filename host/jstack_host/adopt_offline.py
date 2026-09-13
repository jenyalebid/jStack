"""Adopt a Mac that cannot reach this hub yet — the carried bundle.

`adopt` mints a code and prints the command to run on the other Mac. That
command redeems over HTTP, and this is where the whole thing has always come
apart: a Mac that has never been on the mesh has no address that answers it.
The mesh address is unroutable until the tunnel exists, and the tunnel is what
redeeming was supposed to hand back. Off the hub's own LAN there is no third
option — this host publishes no public HTTP, deliberately, and the only thing
open to the internet is the hub's WireGuard port.

So the artefact moves instead of the request. Everything the far Mac needs is
written into one folder, the folder is carried there by whatever means a person
has (AirDrop, a stick, a share), and it is run once. Nothing is typed and no
address is guessed.

The ordering is the entire trick, and it is why this composes out of parts that
already existed rather than adding a transport:

    1. `install_leaf.sh` brings the tunnel up. It needs no hub API at all —
       it dials OUT to the hub's public WireGuard endpoint.
    2. NOW the mesh address answers, because step 1 is what makes it answer.
    3. `jstack-host attach` redeems the code against it, exactly as it would
       have on the LAN.

The deadlock is not worked around here; it is sequenced out of existence.

A carried code expires and a carried tunnel does not, and `join.sh` is written
around that asymmetry rather than against it. If the code is dead by the time
the folder is opened, step 1 has still permanently fixed the thing that was
broken: that Mac is on the mesh, the hub is reachable from it, and a fresh code
attaches from there with no second trip. An expired code costs a command, not
another journey — so it is reported as the small thing it is.
"""

from __future__ import annotations

import stat
from pathlib import Path

#: Baked into `join.sh` rather than resolved on the far Mac. The hub's mesh
#: address is a property of the mesh the bundle carries, not of the machine
#: reading it — and that machine has, by construction, nothing to resolve it
#: with. Anything it could look up is something it does not have yet.
HUB_MESH_IP = "10.66.0.1"

JOIN_SCRIPT = "join.sh"


def _join_script(name: str, code: str, port: int, hub: str) -> str:
    """The folder form — `join.sh` sitting beside the files it installs."""
    return (f'#!/bin/bash\n'
            f'# Join this Mac to the jRemote hub as "{name}" — run from inside\n'
            f'# this folder:\n#\n#     ./{JOIN_SCRIPT}\n#\n'
            f'set -uo pipefail\n'
            f'SRC="$(cd "$(dirname "$0")" && pwd)"\n'
            + _join_body(name, code, port, hub))


def _join_body(name: str, code: str, port: int, hub: str) -> str:
    """Everything after `$SRC` is known — shared by the folder and the packed file.

    One body, two wrappers. The ordering this encodes is the whole feature, and
    a second copy of it is a second chance to get the order wrong in only one
    of them.
    """
    parent = f"http://{hub}:{port}"
    return f'''
CODE="{code}"
PARENT="{parent}"
HUB="{hub}"

say() {{ printf '\\n== %s\\n' "$1"; }}
die() {{ printf '\\n!! %s\\n' "$1" >&2; exit 1; }}

[ -f "$SRC/install_leaf.sh" ] || die "the tunnel installer is missing — this file is incomplete."

# ---------------------------------------------------------------- prereqs
# Checked before anything is installed rather than discovered halfway: the
# tunnel installer needs both binaries, and a half-installed tunnel on a
# machine you had to travel to is the expensive failure here.
missing=""
for b in wireguard-go wg; do
    command -v "$b" >/dev/null 2>&1 || {{
        found=""
        for pfx in /opt/homebrew/bin /usr/local/bin; do
            [ -x "$pfx/$b" ] && found=1
        done
        [ -n "$found" ] || missing="$missing $b"
    }}
done
if [ -n "$missing" ]; then
    die "missing:$missing
   Install them first, then re-run this:
       brew install wireguard-go wireguard-tools"
fi

# ------------------------------------------------------------- 1. tunnel
say "Bringing up the mesh tunnel (asks for your password)"
sudo "$SRC/install_leaf.sh" || die "the tunnel did not install — nothing else can work until it does."

# ----------------------------------------------------- 2. wait for the hub
# The daemon comes up asynchronously and the first handshake crosses the
# internet, so the hub is not reachable the instant the installer returns.
# Polled rather than slept: a fixed sleep is either a stall or a lie.
say "Waiting for the hub to answer on the mesh"
reached=""
for _ in $(seq 1 30); do
    if ping -c 1 -W 1000 "$HUB" >/dev/null 2>&1; then reached=1; break; fi
    sleep 1
done
if [ -z "$reached" ]; then
    die "the tunnel installed but $HUB does not answer.
   Check it with:  sudo wg show
   A corporate VPN that claims routes over 10.66.0.0/24 will take this one."
fi
echo "   $HUB is reachable — this Mac is on the mesh."

# ------------------------------------------------------------- 3. the host
if ! command -v jstack-host >/dev/null 2>&1; then
    say "jstack-host is not installed on this Mac"
    cat <<'NEEDHOST'
   The tunnel is up and permanent — the hard part is done and this Mac
   will rejoin it by itself from now on.

   Install the host, then run the attach line printed below:
       curl -fsSL <your jStack install.sh> | bash
NEEDHOST
    echo "       jstack-host attach $CODE --parent $PARENT"
    exit 1
fi

# ----------------------------------------------- 3b. can it finish the job?
# Installed is not the same as new enough, and `version` cannot tell them
# apart — it has printed 0.1.0 since the first commit. A host older than
# delegated minting attaches cleanly and hands back no grant, leaving a Mac
# the hub can never mint onto again, with every step reporting success. A
# build too old to delegate is too old to have this subcommand, so it fails
# the probe by exiting non-zero and needs no cooperation to be caught.
if ! jstack-host capabilities 2>/dev/null | grep -qx delegated-minting; then
    say "the jstack-host on this Mac is too old to finish adoption"
    cat <<'TOOOLD'
   The tunnel is up and permanent — that half is done and it survives this.

   But this Mac's host cannot hand a grant back to the hub, so attaching now
   would produce a machine your devices can never reach without someone
   typing a second code on it. Upgrade the host here first:

       curl -fsSL <your jStack install.sh> | bash
TOOOLD
    echo "   then run:  jstack-host attach $CODE --parent $PARENT"
    exit 1
fi

# -------------------------------------------------------------- 4. attach
say "Attaching to the hub as \\"{name}\\""
if jstack-host attach "$CODE" --parent "$PARENT"; then
    printf '\\n== Done — this Mac is a managed hub on the mesh.\\n'
    exit 0
fi

# The code is the only part of this that expires. Saying so is the difference
# between "it failed" and "you are one command from finished" — and the tunnel
# standing means that second trip is never needed again.
cat <<ENDFAIL

!! The tunnel is UP, but the enrolment code did not redeem.

   Codes expire; tunnels do not. This Mac is on the mesh permanently now,
   so the hub is reachable from here and you do not need to carry anything
   again. On the HUB, mint a fresh one:

       jstack-host adopt {name}

   then run it here:

       jstack-host attach <NEW-CODE> --parent $PARENT
ENDFAIL
exit 1
'''


#: Where the payload starts inside a packed file. Read by the script itself,
#: so it is a contract between the packer and the thing it packs.
PAYLOAD_MARKER = "__JREMOTE_PAYLOAD__"


def pack(name: str, code: str, port: int, hub: str = HUB_MESH_IP,
         dest: Path | None = None) -> Path:
    """One executable file that carries the whole join — keys, code and scripts.

    A folder is not a thing a person carries to another machine; it is eight
    things, and the one that has to be run is not obviously the one to run. So
    the folder is packed into a single self-extracting script: it unpacks
    itself into a private temp directory, runs the same ordered join, and
    deletes what it unpacked on the way out.

    The file is a credential. It carries this machine's WireGuard private key
    and a live enrolment code, so it is written 0600 and says so — anyone
    holding it can join the mesh as this machine until it is deleted.
    """
    import base64
    import io
    import tarfile

    from . import tunnel

    folder = tunnel.leaf_bundle_dir(name)
    if not folder.is_dir():
        raise tunnel.TunnelError(
            f"no leaf bundle for {name} at {folder} — there is nothing to pack")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for item in sorted(folder.iterdir()):
            # The packed file is not packed into itself, and neither is the
            # folder form of the same script — one runner per artefact.
            if item.name == JOIN_SCRIPT or item.name.startswith("join-"):
                continue
            if item.is_file():
                tar.add(item, arcname=item.name)
    payload = base64.b64encode(buf.getvalue()).decode()

    body = _join_body(name, code, port, hub)
    script = f'''#!/bin/bash
# Join this Mac to the jRemote hub as "{name}".
#
#     ./{_packed_name(name)}
#
# Self-contained: the tunnel keys, the bringup scripts and a one-time
# enrolment code are all inside this file. Nothing is downloaded and nothing
# is typed. You are asked for your password once, by the tunnel installer.
#
# This file IS a credential. Delete it once the join succeeds.
set -uo pipefail

SRC="$(mktemp -d "${{TMPDIR:-/tmp}}/jremote-join.XXXXXX")"
chmod 700 "$SRC"
trap 'rm -rf "$SRC"' EXIT INT TERM

# The payload is everything after the marker. `sed` finds it by name rather
# than by a line number the file would have to keep true through every edit.
sed -n "/^{PAYLOAD_MARKER}$/,\\$p" "$0" | tail -n +2 | base64 -d | tar xzf - -C "$SRC" || {{
    printf '\\n!! this file is damaged — its payload did not unpack.\\n' >&2
    exit 1
}}
{body}
{PAYLOAD_MARKER}
{payload}
'''
    out = Path(dest) if dest else folder.parent / _packed_name(name)
    out.write_text(script)
    out.chmod(0o700)
    return out


def _packed_name(name: str) -> str:
    return f"join-{name}.sh"


def _join_readme(name: str, code: str, port: int, hub: str) -> str:
    return f"""# Join this Mac to the hub — "{name}"

Carried here because this Mac cannot reach the hub over HTTP yet. It has no
route to the hub until the tunnel below exists, and the tunnel is what this
folder installs.

## Run

    ./{JOIN_SCRIPT}

Once. It asks for your password (the tunnel installs as root), brings the
tunnel up, waits for the hub to answer, and redeems the enrolment code.

Prereq, if you do not have it:

    brew install wireguard-go wireguard-tools

## What it does

1. Installs the WireGuard leaf tunnel. This Mac dials OUT to the hub's public
   endpoint — nothing listens here, no port is forwarded, no inbound path is
   opened. It works from any network with internet.
2. Waits for `{hub}` to answer, which it can only do once step 1 is up.
3. Redeems `{code}` against `http://{hub}:{port}`.

## If the code has expired

It does not cost you a second trip. Step 1 is permanent — once the tunnel is
up, this Mac is on the mesh for good and the hub is reachable from here. Mint
a fresh code on the hub with `jstack-host adopt {name}` and run the
`jstack-host attach` line it prints, right here.

## Keep this folder private

`jrleaf.conf` holds this machine's private key. Anyone who has it can join the
mesh as this machine. Delete the folder once the join succeeds.
"""


def _conf_field(conf: str, key: str) -> str:
    import re
    m = re.search(rf"^\s*{key}\s*=\s*(.+?)\s*$", conf, re.M)
    return m.group(1) if m else ""


def relift(name: str) -> Path:
    """Rebuild a leaf bundle for a machine that is already a peer.

    `wg_peer add` refuses a name that is already in the peer table, and it is
    right to: re-adding mints a new keypair, and the hub would start expecting
    a public key the far machine has no way to hold. But that refusal leaves a
    real machine unreachable — a Mac paired as a *device* (or one whose bundle
    folder was deleted) can never be handed the leaf artefacts, because the one
    code path that writes them is the one that refuses to run.

    Nothing has to be re-minted to fix that. The hub still stores the peer's
    own client conf, private key included, and a leaf bundle is the same
    credential in a different shape — `jrleaf.conf` is that conf with the
    `Address` line lifted out into `leaf.env`, because `wg setconf` rejects
    wg-quick syntax. So the bundle is rebuilt from what the peer already has
    and the peer table is left untouched.
    """
    from . import tunnel

    conf_path = tunnel.CLIENTS_DIR / f"{name}.conf"
    if not conf_path.exists():
        raise tunnel.TunnelError(
            f"{name} is in the peer table but its conf is gone from "
            f"{conf_path} — the private key only ever existed there and on "
            "that machine, so this peer cannot be rebuilt. Remove it with "
            f"`wg_peer.py remove {name}` and adopt it fresh.")

    conf = conf_path.read_text()
    address = _conf_field(conf, "Address")
    private = _conf_field(conf, "PrivateKey")
    public = _conf_field(conf, "PublicKey")
    endpoint = _conf_field(conf, "Endpoint")
    allowed = _conf_field(conf, "AllowedIPs")
    missing = [k for k, v in (("Address", address), ("PrivateKey", private),
                              ("PublicKey", public), ("Endpoint", endpoint))
               if not v]
    if missing:
        raise tunnel.TunnelError(
            f"{conf_path.name} is missing {', '.join(missing)} — it cannot be "
            "turned into a tunnel that comes up")

    subnet = allowed or f"{address.rsplit('.', 1)[0]}.0/24"
    hub_ip = subnet.split("/")[0].rsplit(".", 1)[0] + ".1"
    # A conf minted before the MTU key existed still gets the clamp: at the
    # 1420 default, constrained paths pass the handshake and drop bulk traffic.
    mtu = _conf_field(conf, "MTU") or "1240"

    folder = tunnel.leaf_bundle_dir(name)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)

    leaf_conf = folder / "jrleaf.conf"
    leaf_conf.write_text(
        f"[Interface]\n"
        f"PrivateKey = {private}\n"
        f"\n"
        f"[Peer]\n"
        f"PublicKey = {public}\n"
        f"AllowedIPs = {subnet}\n"
        f"Endpoint = {endpoint}\n"
        f"PersistentKeepalive = 25\n")
    leaf_conf.chmod(0o600)

    (folder / "leaf.env").write_text(
        f"WG_ADDR={address if '/' in address else address + '/32'}\n"
        f"WG_SUBNET={subnet}\n"
        f"WG_HUB={hub_ip}\n"
        f"WG_MTU={mtu}\n")

    # The bringup scripts are the hub's own, copied in — same three
    # `wg_peer.py` copies, read from where it reads them so a leaf rebuilt
    # today gets today's installer rather than a frozen one.
    source = tunnel.PEER_SCRIPT.parent
    for script in ("install_leaf.sh", "wg_up.sh", "wg_leaf_watch.sh"):
        src = source / script
        if not src.is_file():
            raise tunnel.TunnelError(
                f"the hub's {script} is not at {src} — a bundle without it "
                "installs nothing")
        target = folder / script
        target.write_bytes(src.read_bytes())
        target.chmod(0o755)

    (folder / "README.md").write_text(
        f"# jRemote leaf — {name}\n"
        f"\n"
        f"This machine dials out to the hub's WireGuard endpoint and joins the\n"
        f"mesh as {address}. Nothing listens publicly — no port-forward, no\n"
        f"inbound path. Prereq (once):\n"
        f"`brew install wireguard-go wireguard-tools`.\n"
        f"\n"
        f"Rebuilt from this peer's existing credentials — the keypair is the\n"
        f"one the hub already expects, so nothing on the hub changed.\n"
        f"\n"
        f"Run `./join.sh` — see JOIN.md.\n")
    return folder


def emit(name: str, code: str, port: int, hub: str = HUB_MESH_IP) -> Path:
    """Write the carryable join artefacts into `name`'s leaf bundle.

    The tunnel half of the bundle is not written here — `tunnel.issue(leaf=True)`
    already writes it, and re-deriving it would be a second implementation of a
    keypair that must stay the one the hub's peer table holds. This adds only
    what turns that folder from parts into a thing that runs: the ordered
    script, and the page explaining what to do when the code has aged out.

    Returns the folder to carry.
    """
    from . import tunnel

    folder = tunnel.leaf_bundle_dir(name)
    if not folder.is_dir():
        raise tunnel.TunnelError(
            f"no leaf bundle for {name} at {folder} — the tunnel half has to "
            "exist before the join script can point at it")

    script = folder / JOIN_SCRIPT
    script.write_text(_join_script(name, code, port, hub))
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # Written beside the tunnel's own README rather than over it: that one is
    # the hub's description of a leaf and is read back by `tunnel._read_bundle`
    # as issued state. Clobbering it would change what every future redeem of
    # this peer hands back.
    (folder / "JOIN.md").write_text(_join_readme(name, code, port, hub))
    return folder
