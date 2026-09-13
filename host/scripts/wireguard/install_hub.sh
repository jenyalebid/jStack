#!/bin/bash
# One-time root install of a jRemote HUB tunnel — the machine every device and
# every leaf dials into:
#
#   sudo ./install_hub.sh --endpoint <host-or-ip>:51820
#
# A hub is the one machine with an inbound path: UDP 51820 reaches it from the
# internet, forwarded by the router or served on a public address. Everything
# else on the mesh dials out to it. `--endpoint` is what those machines will
# dial, so it is the hub's public address, not its LAN one.
#
# Installs (paths relative to the tree this script sits in, so the checkout
# here and an installed host payload both work):
#
#   <tree>/Credentials/wireguard/server.key|.pub    minted here if absent (0600)
#   <tree>/Credentials/wireguard/wg0.conf           the interface (0600)
#   <tree>/Credentials/wireguard/endpoint           what clients dial
#   /Library/Application Support/jRemote Hub/       wg_up.sh, wg_sync.sh
#   /Library/LaunchDaemons/com.jremote.hub.plist       the tunnel (KeepAlive)
#   /Library/LaunchDaemons/com.jremote.hub-sync.plist  live peer apply (WatchPaths)
#
# The credentials stay in the invoking user's tree, owned by that user, because
# `wg_peer.py` writes wg0.conf unprivileged — pairing a phone from the app must
# not need a password. Root only owns the two daemons that read it.
#
# Idempotent: safe to re-run after upgrading the payload or changing the
# endpoint. Re-running never regenerates keys — that would silently revoke
# every device already paired. Prereq:
#   brew install wireguard-go wireguard-tools
#
# Tests run this unprivileged with HUB_DEST=<tmpdir> and LAUNCHCTL=<mock>;
# they stop at the installed layout — liveness needs the real network.
set -euo pipefail

DEST="${HUB_DEST:-}"
LAUNCHCTL="${LAUNCHCTL:-/bin/launchctl}"
ENDPOINT=""
ADDR="${WG_ADDR:-10.66.0.1/24}"
SUBNET="${WG_SUBNET:-10.66.0.0/24}"
PORT="${WG_PORT:-51820}"
# Pinned into the daemon like every other knob: the hub must clamp too — a
# leaf at 1240 still stalls if the hub keeps emitting 1420-sized datagrams.
MTU="${WG_MTU:-1240}"

while [ $# -gt 0 ]; do
    case "$1" in
        --endpoint) ENDPOINT="${2:-}"; shift 2 ;;
        --addr)     ADDR="${2:-}"; shift 2 ;;
        --subnet)   SUBNET="${2:-}"; shift 2 ;;
        --port)     PORT="${2:-}"; shift 2 ;;
        -h|--help)  sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "unknown argument $1" >&2; exit 1 ;;
    esac
done

if [ -z "$DEST" ]; then
    [ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
fi

SRC="$(cd "$(dirname "$0")" && pwd)"
for f in wg_up.sh wg_sync.sh wg_peer.py; do
    [ -f "$SRC/$f" ] || { echo "missing $SRC/$f — run this from the scripts/wireguard folder of a host install" >&2; exit 1; }
done

find_bin() {
    command -v "$1" 2>/dev/null && return 0
    for pfx in /opt/homebrew/bin /usr/local/bin; do
        if [ -x "$pfx/$1" ]; then echo "$pfx/$1"; return 0; fi
    done
    return 1
}
WG_GO="${WG_GO:-$(find_bin wireguard-go || true)}"
WG_BIN="${WG:-$(find_bin wg || true)}"
if [ -z "$WG_GO" ] || [ -z "$WG_BIN" ]; then
    echo "missing wireguard-go / wg — brew install wireguard-go wireguard-tools" >&2
    exit 1
fi

# ── the hub's own credentials, in the user's tree ──
# `WG_PEER_DIR` first, then the derivation. That order is not a convenience: it
# is the variable `wg_peer.py` honours, and this script mints the keys and the
# wg0.conf that `wg_peer.py` later appends peers to. Deriving only — which this
# did — meant that setting `WG_PEER_DIR`, the documented way to relocate the
# mesh, pointed the pairing tool at one directory while the installer minted the
# conf in another and wrote *that* path into both LaunchDaemons. Pairing then
# succeeded into a file nothing loaded and nothing watched: a device handed a
# working config, a hub that never got the peer, and no error on either side.
#
# The derivation stays as the default because under sudo $HOME is root's, so the
# keys must land beside the host code that reads them whoever ran the installer.
WG_DIR="${WG_PEER_DIR:-$(cd "$SRC/../.." && pwd)/Credentials/wireguard}"
OWNER="${SUDO_USER:-$(id -un)}"

install -d -m 0700 "$WG_DIR"
if [ ! -f "$WG_DIR/server.key" ]; then
    umask 077
    "$WG_BIN" genkey > "$WG_DIR/server.key"
    "$WG_BIN" pubkey < "$WG_DIR/server.key" > "$WG_DIR/server.pub"
    echo "minted a server keypair"
else
    echo "server keypair already present — keeping it (regenerating revokes every paired device)"
fi
chmod 0600 "$WG_DIR/server.key"

CONF="$WG_DIR/wg0.conf"
if [ ! -f "$CONF" ]; then
    umask 077
    printf '[Interface]\nPrivateKey = %s\nListenPort = %s\n' \
        "$(cat "$WG_DIR/server.key")" "$PORT" > "$CONF"
    echo "wrote $CONF"
else
    echo "wg0.conf already present — keeping it and its peers"
fi
chmod 0600 "$CONF"

# The address clients dial. Required on a first install: without it wg_peer.py
# refuses to pair rather than handing out a conf with no endpoint in it.
if [ -n "$ENDPOINT" ]; then
    printf '%s\n' "$ENDPOINT" > "$WG_DIR/endpoint"
    chmod 0600 "$WG_DIR/endpoint"
    echo "endpoint: $ENDPOINT"
elif [ ! -s "$WG_DIR/endpoint" ]; then
    echo "no endpoint configured — re-run with --endpoint <host-or-ip>:$PORT" >&2
    echo "(the address other machines dial to reach this hub; pairing refuses without it)" >&2
    exit 1
else
    echo "endpoint: $(cat "$WG_DIR/endpoint") (unchanged)"
fi

# install(1) ran as root made these root-owned; the host process that pairs
# devices is not root, and a hub whose wg0.conf it cannot append to answers
# every pairing request with a permission error.
if [ "$(id -u)" -eq 0 ]; then
    chown -R "$OWNER" "$WG_DIR"
fi

# ── the daemons ──
APP_DIR="$DEST/Library/Application Support/jRemote Hub"
DAEMONS="$DEST/Library/LaunchDaemons"
LOG_DIR="$DEST/var/log/jremote-hub"
NAME_FILE="/var/run/wireguard/jremote-hub.name"

install -d -m 0755 "$APP_DIR" "$DAEMONS" "$LOG_DIR"
install -m 0755 "$SRC/wg_up.sh" "$SRC/wg_sync.sh" "$APP_DIR/"

cat > "$DAEMONS/com.jremote.hub.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.jremote.hub</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$APP_DIR/wg_up.sh</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>WG_GO</key>
        <string>$WG_GO</string>
        <key>WG</key>
        <string>$WG_BIN</string>
        <key>WG_CONF</key>
        <string>$CONF</string>
        <key>WG_ADDR</key>
        <string>$ADDR</string>
        <key>WG_SUBNET</key>
        <string>$SUBNET</string>
        <key>WG_MTU</key>
        <string>$MTU</string>
        <key>WG_NAME_FILE</key>
        <string>$NAME_FILE</string>
        <key>WG_FORWARD</key>
        <string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/hub.out.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/hub.err.log</string>
</dict>
</plist>
EOF

cat > "$DAEMONS/com.jremote.hub-sync.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.jremote.hub-sync</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$APP_DIR/wg_sync.sh</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>WG</key>
        <string>$WG_BIN</string>
        <key>WG_CONF</key>
        <string>$CONF</string>
        <key>WG_NAME_FILE</key>
        <string>$NAME_FILE</string>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>WatchPaths</key>
    <array>
        <string>$CONF</string>
    </array>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/hub-sync.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/hub-sync.log</string>
</dict>
</plist>
EOF

for name in com.jremote.hub com.jremote.hub-sync; do
    "$LAUNCHCTL" bootout system "$DAEMONS/$name.plist" 2>/dev/null || true
    "$LAUNCHCTL" bootstrap system "$DAEMONS/$name.plist"
    echo "installed $name"
done

# Tests stop here — the layout is the assertable part; a live interface needs
# the real kernel and the real binaries.
[ -z "$DEST" ] || exit 0

IFACE=""
for _ in $(seq 1 20); do
    IFACE="$(cat "$NAME_FILE" 2>/dev/null || true)"
    [ -n "$IFACE" ] && break
    sleep 0.5
done
[ -n "$IFACE" ] || { echo "tunnel never came up — see $LOG_DIR/hub.err.log" >&2; exit 1; }
echo "tunnel up on $IFACE at $ADDR"
"$WG_BIN" show "$IFACE" | grep -v 'private key' || true

cat <<EOF

This machine is now a hub. Two things it cannot check for itself:

  · UDP $PORT must reach it from outside — forward it on the router to this
    machine, or give it a public address. Nothing else needs opening.
  · The endpoint above must resolve to that address from wherever the other
    machines are. A dynamic home IP needs a hostname that follows it.

Pair a device from the app (Settings > This Mac), or from this machine:
  $SRC/wg_peer.py add <device-name>
EOF
