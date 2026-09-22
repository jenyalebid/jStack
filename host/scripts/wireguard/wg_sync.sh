#!/bin/bash
# Applies peer changes from wg0.conf to the live interface without dropping it.
# Triggered by the hub's sync LaunchDaemon (root, WatchPaths on the conf)
# whenever it changes — pairing a new device needs no sudo and no restart.
set -euo pipefail

WG="${WG:-/opt/homebrew/bin/wg}"
IFCONFIG="${IFCONFIG:-/sbin/ifconfig}"
# Three rungs, and the middle one is the fix: `WG_CONF` (what install_hub.sh
# writes into this daemon's plist) wins, then `WG_PEER_DIR` — the variable
# `wg_peer.py` honours, so a relocated mesh moves its readers with it — then the
# derivation, which is what a checkout run by hand resolves. Without the middle
# rung this file syncs the tree's conf while the pairing tool writes another.
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
CONF="${WG_CONF:-${WG_PEER_DIR:-$SELF_DIR/../../Credentials/wireguard}/wg0.conf}"
NAME_FILE="${WG_NAME_FILE:-/var/run/wireguard/jremote-wg.name}"

[ -s "$NAME_FILE" ] || { echo "wg_sync: tunnel not up (no $NAME_FILE) — nothing to sync"; exit 0; }
IFACE="$(cat "$NAME_FILE")"

# The conf is replaced by rename, and the WatchPaths fire can land inside that
# window — for a moment the path names no file. One bare `syncconf` there dies
# on fopen, and launchd never re-fires for a failed run: the new peer would
# stay off the live interface until the next conf write, silently. Wait the
# window out; hold `wg`'s complaints back unless every try is spent, so a
# transient miss leaves one clean line instead of fopen noise.
err=""
for _ in 1 2 3 4 5; do
    if [ -s "$CONF" ] && err="$("$WG" syncconf "$IFACE" "$CONF" 2>&1)"; then
        # Convergence includes the MTU clamp (see wg_up.sh): a tunnel that came
        # up before the clamp existed heals on its next sync, live, without
        # dropping the interface or a single handshake. Unconditional because it
        # is idempotent — setting an MTU the interface already has changes
        # nothing.
        "$IFCONFIG" "$IFACE" mtu "${WG_MTU:-1240}"
        echo "wg_sync: $CONF -> $IFACE (mtu ${WG_MTU:-1240})"
        exit 0
    fi
    sleep 1
done
[ -n "$err" ] && printf '%s\n' "$err" >&2
echo "wg_sync: $CONF -> $IFACE failed after 5 tries — interface left unsynced" >&2
exit 1
