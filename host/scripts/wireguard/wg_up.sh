#!/bin/bash
# WireGuard tunnel — supervised by a root launchd KeepAlive daemon (the hub's
# tunnel daemon, or com.jremote.leaf on a leaf). Creates the utun device via
# wireguard-go, applies the conf, addresses the interface, then blocks on the
# wireguard-go process so launchd KeepAlive supervision works: wireguard-go
# dies -> script exits -> launchd restarts.
#
# Every binary and path is env-overridable so the integration test can run
# this unprivileged against mocks.
set -euo pipefail

WG_GO="${WG_GO:-/opt/homebrew/bin/wireguard-go}"
WG="${WG:-/opt/homebrew/bin/wg}"
IFCONFIG="${IFCONFIG:-/sbin/ifconfig}"
ROUTE="${ROUTE:-/sbin/route}"
SYSCTL="${SYSCTL:-/usr/sbin/sysctl}"
# Three rungs. `WG_CONF` wins — install_hub.sh and install_leaf.sh both write an
# explicit one into the LaunchDaemon they make, so an installed copy never falls
# past it. Then `WG_PEER_DIR`, the variable `wg_peer.py` honours: a mesh that has
# been relocated has to bring the daemon that loads it along, or this brings up
# an interface from the tree's stale conf while every peer is added elsewhere.
# Then the derivation from this script's own location, which is what a hub
# checkout run by hand resolves, with no username or home layout baked into a
# file that ships in leaf bundles.
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
CONF="${WG_CONF:-${WG_PEER_DIR:-$SELF_DIR/../../Credentials/wireguard}/wg0.conf}"
RUN_DIR="${WG_RUN_DIR:-/var/run/wireguard}"
ADDR="${WG_ADDR:-10.66.0.1/24}"
SUBNET="${WG_SUBNET:-10.66.0.0/24}"
# The conf is setconf-style, and `wg setconf` has no MTU key — the interface
# MTU can only be set here. Left at wireguard-go's 1420 default, any real path
# narrower than ~1480 (cellular, VPN, CGNAT middleboxes) blackholes bulk
# traffic in whichever direction crosses it, so both ends run this clamp.
MTU="${WG_MTU:-1240}"

[ -f "$CONF" ] || { echo "wg_up: missing conf $CONF" >&2; exit 1; }

# The utun name file: shared runtime state between this script and whatever
# watches the tunnel (the hub's sync daemon, a leaf's watcher), so both halves
# of one install must be told the same path. Overridable for that reason, and
# defaulted to the name this machine's live daemons already read — renaming it
# from under a running tunnel is a coordinated restart, not an edit.
NAME_FILE="${WG_NAME_FILE:-$RUN_DIR/jremote-wg.name}"
RUN_DIR="$(dirname "$NAME_FILE")"
mkdir -p "$RUN_DIR"
rm -f "$NAME_FILE"

export WG_TUN_NAME_FILE="$NAME_FILE"
"$WG_GO" -f utun &
WG_PID=$!
trap 'kill "$WG_PID" 2>/dev/null || true' EXIT

# wireguard-go writes the assigned utunN name once the device exists
for _ in $(seq 1 50); do
  [ -s "$NAME_FILE" ] && break
  sleep 0.2
done
[ -s "$NAME_FILE" ] || { echo "wg_up: wireguard-go never wrote $NAME_FILE" >&2; exit 1; }
IFACE="$(cat "$NAME_FILE")"

"$WG" setconf "$IFACE" "$CONF"
IP="${ADDR%/*}"
"$IFCONFIG" "$IFACE" inet "$ADDR" "$IP" alias
"$IFCONFIG" "$IFACE" mtu "$MTU"
"$IFCONFIG" "$IFACE" up
"$ROUTE" -q -n add -inet "$SUBNET" -interface "$IFACE" >/dev/null 2>&1 || true

# Peer-to-peer: a packet from one peer to another (phone -> leaf) arrives here
# and must be forwarded back out the same interface. Gated so a leaf running
# this same script never turns its machine into a router — only the hub's
# LaunchDaemon sets WG_FORWARD=1. Cryptokey routing still pins every peer to
# its /32, so forwarding moves mesh traffic between known peers and nothing else.
if [ "${WG_FORWARD:-0}" = "1" ]; then
  "$SYSCTL" -w net.inet.ip.forwarding=1 >/dev/null
  echo "wg_up: ip forwarding on — peers reach peers through this hub"
fi

echo "wg_up: $IFACE live at $ADDR mtu $MTU"
wait "$WG_PID"
