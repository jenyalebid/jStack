#!/usr/bin/env bash
# Prove off-network reachability: a peer on a foreign network reaches the hub's
# app through the mesh by dialing the hub's PUBLIC endpoint and hairpinning back.
#
# This is the leg acceptance step 3 calls "the cellular leg", automated. A phone
# on a carrier and a leaf VM take the identical path: neither sits on the hub's
# LAN, both reach it only through the public UDP endpoint — out to the internet
# and back. XCUITest cannot drive a carrier radio; this proves the transport the
# radio would ride, end to end, from a machine that is not the hub.
#
# What it asserts, in order, and fails red on the first that breaks:
#   1. the leaf's live tunnel dials a PUBLIC endpoint, not a LAN address —
#      a leaf pointed at 192.168.x would "reach" the hub without ever leaving
#      the LAN, which proves nothing about off-network.
#   2. the handshake is recent — the tunnel is up now, not a stale row.
#   3. the hub's app answers 200 with ok:true over the mesh — the data path
#      carries real product traffic, not just ICMP.
#
#   offnet-test.sh [--leaf NAME] [--endpoint HOST:PORT] [--hub-ip IP] [--port N] [--max-age SECONDS]
#
#   --leaf NAME       off-LAN peer guest to drive (default: jr-leaf)
#   --endpoint H:P    require the tunnel to dial exactly this public endpoint;
#                     without it, any non-RFC1918 endpoint passes check 1
#   --hub-ip IP       the hub's mesh address (default: 10.66.0.1)
#   --port N          the host app port on the mesh (default: 9090)
#   --max-age SECONDS handshake no older than this (default: 180)
#
# Env: VM_SH — the vm.sh that drives the guest (default: on PATH). Exits 0 green.

set -euo pipefail

VM_SH="${VM_SH:-$(command -v vm.sh 2>/dev/null || true)}"
: "${VM_SH:?set VM_SH to the vm.sh that drives your leaf guest}"
[ -x "$VM_SH" ] || { echo "no vm.sh at $VM_SH" >&2; exit 2; }

LEAF="jr-leaf"
WANT_ENDPOINT=""
HUB_IP="10.66.0.1"
PORT="9090"
MAX_AGE="180"

while [ $# -gt 0 ]; do
    case "$1" in
        --leaf)     LEAF="$2"; shift 2 ;;
        --endpoint) WANT_ENDPOINT="$2"; shift 2 ;;
        --hub-ip)   HUB_IP="$2"; shift 2 ;;
        --port)     PORT="$2"; shift 2 ;;
        --max-age)  MAX_AGE="$2"; shift 2 ;;
        -h|--help)  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
pass() { printf '\033[32m✓ %s\033[0m\n' "$*"; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

vssh() { "$VM_SH" ssh "$LEAF" "$@"; }

# Is an IPv4 address one of the private / link-local ranges? Such an endpoint
# means the "off-LAN" peer is really on the LAN, and reaching the hub proves
# nothing about the internet path.
is_private() {
    case "$1" in
        10.*|127.*|169.254.*|192.168.*) return 0 ;;
        172.1[6-9].*|172.2[0-9].*|172.3[0-1].*) return 0 ;;
        *) return 1 ;;
    esac
}

say "off-network reachability — leaf '$LEAF' → hub $HUB_IP:$PORT via its public endpoint"

# ── gather live state from the leaf in one round-trip ──
# `wg show all dump` is the machine-readable form: interface line first, then a
# tab-separated peer line per peer. Field 4 is the endpoint, field 6 the latest
# handshake as epoch seconds (0 = never). Curl runs from the leaf so the request
# actually crosses the mesh; loopback here would be a lie.
RAW="$(vssh 'set -e
echo "NOW=$(date +%s)"
echo "---WG---"
sudo /opt/homebrew/bin/wg show all dump 2>/dev/null || sudo wg show all dump 2>/dev/null || true
echo "---HEALTH---"
curl -sS -m 8 -o /tmp/offnet_body -w "%{http_code}" "http://'"$HUB_IP"':'"$PORT"'/api/health" 2>/dev/null || true
echo
echo "---BODY---"
cat /tmp/offnet_body 2>/dev/null || true
')" || die "cannot reach the leaf guest '$LEAF' over ssh — is it up? (\`$VM_SH up $LEAF\`)"

NOW="$(printf '%s\n' "$RAW" | sed -n 's/^NOW=//p' | head -1)"
WG="$(printf '%s\n' "$RAW" | awk '/^---WG---$/{f=1;next} /^---HEALTH---$/{f=0} f')"
HTTP="$(printf '%s\n' "$RAW" | awk '/^---HEALTH---$/{f=1;next} /^---BODY---$/{f=0} f' | tr -d '[:space:]')"
BODY="$(printf '%s\n' "$RAW" | awk '/^---BODY---$/{f=1;next} f')"

[ -n "$WG" ] || die "the leaf has no WireGuard tunnel at all — nothing to reach the hub with"

# The peer line with a real endpoint is the hub. dump columns are tab-separated;
# a peer line has >=8 fields, the interface line has 5.
PEER_LINE="$(printf '%s\n' "$WG" | awk -F'\t' 'NF>=8 && $4!="(none)" && $4!=""' | head -1)"
[ -n "$PEER_LINE" ] || die "the leaf's tunnel has no peer with a live endpoint — it is not dialing any hub"

ENDPOINT="$(printf '%s\n' "$PEER_LINE" | cut -f4)"
HS_EPOCH="$(printf '%s\n' "$PEER_LINE" | cut -f6)"
EP_IP="${ENDPOINT%:*}"

# ── check 1: the endpoint is public (this is what makes it off-network) ──
if is_private "$EP_IP"; then
    die "leaf dials $ENDPOINT — a private address. That is on-LAN, not off-network; the test would prove nothing."
fi
if [ -n "$WANT_ENDPOINT" ] && [ "$ENDPOINT" != "$WANT_ENDPOINT" ]; then
    die "leaf dials $ENDPOINT but this run required $WANT_ENDPOINT"
fi
pass "off-network path: leaf dials PUBLIC endpoint $ENDPOINT (hairpins out to the internet and back)"

# ── check 2: the tunnel is alive now ──
if [ -z "$HS_EPOCH" ] || [ "$HS_EPOCH" = "0" ]; then
    die "no handshake ever with $ENDPOINT — the tunnel never came up"
fi
AGE=$(( NOW - HS_EPOCH ))
[ "$AGE" -ge 0 ] || AGE=0
if [ "$AGE" -gt "$MAX_AGE" ]; then
    die "last handshake was ${AGE}s ago (limit ${MAX_AGE}s) — the tunnel is stale, not live"
fi
pass "tunnel live: handshake ${AGE}s ago"

# ── check 3: the hub's app answers over the mesh ──
[ "$HTTP" = "200" ] || die "hub app returned HTTP ${HTTP:-<none>} over the mesh — the data path does not carry product traffic"
case "$BODY" in
    *'"ok":true'*) : ;;
    *) die "hub answered 200 but body is not a healthy jremote-host: $BODY" ;;
esac
pass "hub app reachable over the mesh: HTTP 200 $BODY"

say "OFF-NETWORK REACHABILITY: GREEN — a foreign-network peer reaches the hub app through the tunnel."
