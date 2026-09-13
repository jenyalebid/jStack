#!/bin/bash
# One-time root install of a jRemote leaf tunnel — run from inside a leaf
# bundle (made on the hub by `wg_peer.py add --leaf <name>`) on the machine
# joining the mesh:
#
#   sudo ./install_leaf.sh
#
# A leaf dials OUT to the hub's WireGuard endpoint and is reached only through
# the mesh — nothing listens publicly, no port-forward, no inbound path.
# Installs:
#
#   /etc/wireguard/jrleaf.conf                           peer conf (0600)
#   /Library/Application Support/jRemote Leaf/           wg_up.sh, wg_leaf_watch.sh, leaf.env
#   /Library/LaunchDaemons/com.jremote.leaf.plist        the tunnel (KeepAlive)
#   /Library/LaunchDaemons/com.jremote.leaf-watch.plist  endpoint re-resolve, every 2 min
#
# Idempotent: safe to re-run after editing anything. Prereq:
#   brew install wireguard-go wireguard-tools
#
# Tests run this unprivileged with LEAF_DEST=<tmpdir> and LAUNCHCTL=<mock>;
# they stop at the installed layout — liveness is the real machine's own step.
set -euo pipefail

DEST="${LEAF_DEST:-}"
LAUNCHCTL="${LAUNCHCTL:-/bin/launchctl}"

if [ -z "$DEST" ]; then
    [ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
fi

SRC="$(cd "$(dirname "$0")" && pwd)"
for f in jrleaf.conf leaf.env wg_up.sh wg_leaf_watch.sh; do
    [ -f "$SRC/$f" ] || { echo "missing $SRC/$f — run from inside a leaf bundle" >&2; exit 1; }
done
# shellcheck source=/dev/null
. "$SRC/leaf.env"
: "${WG_ADDR:?leaf.env must set WG_ADDR}"
: "${WG_SUBNET:?leaf.env must set WG_SUBNET}"
: "${WG_HUB:?leaf.env must set WG_HUB}"
# Bundles minted before the MTU key existed have no WG_MTU line — they still
# get the clamp, or a re-install would quietly revert the leaf to 1420.
WG_MTU="${WG_MTU:-1240}"

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

APP_DIR="$DEST/Library/Application Support/jRemote Leaf"
CONF_DIR="$DEST/etc/wireguard"
CONF="$CONF_DIR/jrleaf.conf"
DAEMONS="$DEST/Library/LaunchDaemons"
LOG_DIR="$DEST/var/log/jremote-leaf"

install -d -m 0755 "$APP_DIR" "$DAEMONS" "$LOG_DIR"
install -d -m 0700 "$CONF_DIR"
install -m 0600 "$SRC/jrleaf.conf" "$CONF"
install -m 0755 "$SRC/wg_up.sh" "$SRC/wg_leaf_watch.sh" "$APP_DIR/"
install -m 0644 "$SRC/leaf.env" "$APP_DIR/"

cat > "$DAEMONS/com.jremote.leaf.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.jremote.leaf</string>
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
        <string>$WG_ADDR</string>
        <key>WG_SUBNET</key>
        <string>$WG_SUBNET</string>
        <key>WG_MTU</key>
        <string>$WG_MTU</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/leaf.out.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/leaf.err.log</string>
</dict>
</plist>
EOF

cat > "$DAEMONS/com.jremote.leaf-watch.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.jremote.leaf-watch</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$APP_DIR/wg_leaf_watch.sh</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>WG</key>
        <string>$WG_BIN</string>
        <key>WG_CONF</key>
        <string>$CONF</string>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>StartInterval</key>
    <integer>120</integer>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/leaf-watch.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/leaf-watch.log</string>
</dict>
</plist>
EOF

for name in com.jremote.leaf com.jremote.leaf-watch; do
    "$LAUNCHCTL" bootout system "$DAEMONS/$name.plist" 2>/dev/null || true
    "$LAUNCHCTL" bootstrap system "$DAEMONS/$name.plist"
    echo "installed $name"
done

# Tests stop here — the layout is the assertable part; a handshake needs the
# real hub on the real network.
[ -z "$DEST" ] || exit 0

NAME_FILE=/var/run/wireguard/jremote-wg.name
IFACE=""
for _ in $(seq 1 20); do
    IFACE="$(cat "$NAME_FILE" 2>/dev/null || true)"
    [ -n "$IFACE" ] && break
    sleep 0.5
done
[ -n "$IFACE" ] || { echo "tunnel never came up — see $LOG_DIR/leaf.err.log" >&2; exit 1; }

HS=""
for _ in $(seq 1 20); do
    # `|| true` is load-bearing, not habit: under `set -e` with `pipefail` an
    # assignment takes the status of its command substitution, so the one
    # second where `wg show` has no interface yet — exactly what this loop is
    # here to wait out — kills the script instead. Silently, and with wg's
    # stderr already sent to /dev/null, so the caller gets a bare exit 1 and
    # `attach` reports "the leaf installer failed" about a tunnel that came up
    # a moment later.
    HS="$("$WG_BIN" show "$IFACE" latest-handshakes 2>/dev/null \
          | awk 'NR==1 {print $2}' || true)"
    if [ -n "$HS" ] && [ "$HS" -gt 0 ]; then break; fi
    sleep 1
done
if [ -z "$HS" ] || [ "$HS" -eq 0 ]; then
    echo "no handshake in 20s — outbound UDP to the hub's endpoint may be blocked on this network" >&2
    exit 1
fi
echo "handshake OK on $IFACE"
if ping -c 1 -t 3 "$WG_HUB" >/dev/null 2>&1; then
    echo "hub $WG_HUB reachable — this machine is on the mesh"
else
    echo "handshake OK but ping to $WG_HUB failed — check the hub-side peer entry" >&2
fi
