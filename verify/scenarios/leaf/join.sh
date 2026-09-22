#!/bin/bash
# WHAT: two real Macs — one hub adopts the other as a LEAF; the tunnel must handshake, the leaf must go managed, and its local instance must survive
# TIME: ~25m
# GUEST: pristine x2 (needs both VM slots free)
#
# The 2026-09-22 break, reproduced against real machines. A leaf join printed
# "com.jremote.leaf installed ... no handshake in 20s — outbound UDP to the
# hub's endpoint may be blocked ... the tunnel did not install — nothing else
# can work until it does", and it bricked the local instance (remote blank).
# This scenario is the whole user flow across two guests: the hub mints a code,
# the leaf redeems it, and the verdict is read from what actually formed — the
# WireGuard handshake, the leaf's mode, and whether the leaf can still serve
# itself. Nothing here is an API stand-in for a surface; the attach is the real
# command the hub told the user to run on the other Mac.
. "$(dirname "$0")/../../lib/common.sh"

HUB=vfy-leaf-join-hub
LEAF=vfy-leaf-join-leaf

boot() {  # <name> — fresh guest, return its ip on stdout
    vm rm "$1" >/dev/null 2>&1 || true
    vm up "$1" >&2
    vm ip "$1" 2>/dev/null | tail -1
}

install_line='curl -fsSL https://raw.githubusercontent.com/jenyalebid/jStack/main/install.sh | bash -s -- --agent Jarvis'

echo "== booting hub and leaf (both VM slots) =="
HUB_IP="$(boot "$HUB")";  [ -n "$HUB_IP" ]  || { echo "VERDICT: FAIL — hub got no ip"; exit 1; }
LEAF_IP="$(boot "$LEAF")"; [ -n "$LEAF_IP" ] || { echo "VERDICT: FAIL — leaf got no ip"; exit 1; }
echo "   hub=$HUB_IP  leaf=$LEAF_IP"

# ---- HUB: install, then mint a host code for the leaf -----------------------
cat > "$RECEIPTS/hub.sh" <<EOF
#!/bin/bash
set -u
export JSTACK_ROOT=/Users/admin/Desktop/Alpine; mkdir -p "\$JSTACK_ROOT"
H="\$HOME/.local/bin/jstack-host"
echo "== hub install =="
$install_line
sleep 5
"\$H" mode 2>&1 | sed 's/^/  hub-mode: /'
echo "== hub mints a code for the leaf =="
ADOPT="\$("\$H" adopt $LEAF --json 2>&1)"
echo "ADOPT-JSON \$ADOPT"
echo "\$ADOPT" | grep -q '"code"' && echo "OK hub minted a host code" || echo "FAIL hub could not mint a host code"
echo DONE-HUB
EOF
GUEST="$HUB"
p="$(guest_payload "$RECEIPTS/hub.sh")"
vm term "$HUB" bash "$p" 2>&1 | tee "$RECEIPTS/hub.log"

CODE="$(sed -n 's/.*ADOPT-JSON.*"code": *"\([^"]*\)".*/\1/p' "$RECEIPTS/hub.log" | head -1)"
if [ -z "$CODE" ]; then echo "VERDICT: FAIL — hub never minted a code (see hub.log)"; exit 1; fi
# A never-joined leaf has no route to the 10.66 mesh yet, so its FIRST attach
# uses the hub's LAN address — exactly what `adopt` names for a machine that has
# not held the tunnel before.
PARENT="http://$HUB_IP:9090"
echo "== leaf will attach with code $CODE against $PARENT =="

# ---- LEAF: install, then attach — the handshake is the whole test -----------
cat > "$RECEIPTS/leaf.sh" <<EOF
#!/bin/bash
set -u
export JSTACK_ROOT=/Users/admin/Desktop/Alpine; mkdir -p "\$JSTACK_ROOT"
H="\$HOME/.local/bin/jstack-host"
echo admin | sudo -S true 2>/dev/null   # prime sudo — attach installs the tunnel as root
echo "== leaf install =="
$install_line
sleep 5
"\$H" mode 2>&1 | sed 's/^/  leaf-mode-before: /'

echo "== ATTACH — this is the real join; the tunnel must handshake =="
if "\$H" attach $CODE --parent $PARENT 2>&1 | tee /tmp/attach.log; then
    echo "OK attach returned success"
else
    echo "FAIL attach failed (rc \$?) — the leaf never joined"
fi
grep -qi "no handshake\|did not install\|blocked" /tmp/attach.log \
    && echo "FAIL attach reported the tunnel/handshake failure" \
    || echo "OK attach reported no handshake/tunnel failure"

echo "== leaf must now BE a managed leaf =="
LM="\$("\$H" mode 2>&1)"
echo "\$LM" | sed 's/^/  leaf-mode-after: /'
echo "\$LM" | grep -qi managed && echo "OK leaf mode is managed" || echo "FAIL leaf mode is not managed after attach"

echo "== the tunnel actually handshaked (WireGuard, not a claim) =="
HS="\$(echo admin | sudo -S wg show all latest-handshakes 2>/dev/null)"
echo "\$HS" | sed 's/^/  wg: /'
NOW=\$(date +%s); GOT=0
while read -r _if _peer t; do [ -n "\$t" ] && [ "\$t" -gt 0 ] && [ \$((NOW - t)) -lt 180 ] && GOT=1; done <<<"\$HS"
[ "\$GOT" = 1 ] && echo "OK a recent WireGuard handshake exists" || echo "FAIL no recent WireGuard handshake — the tunnel is not up"

echo "== the leaf can reach the hub THROUGH the mesh (10.66.0.1) =="
curl -fsS --max-time 8 http://10.66.0.1:9090/api/health >/dev/null 2>&1 \
    && echo "OK leaf reached the hub over the tunnel" \
    || echo "FAIL leaf cannot reach the hub over the tunnel"

echo "== the leaf's OWN local instance survived (not bricked, remote not blank) =="
if "\$H" status >/dev/null 2>&1; then echo "OK leaf host still installed/answering"; else echo "FAIL leaf host not answering — local bricked"; fi
curl -fsS --max-time 8 http://127.0.0.1:9090/api/health >/dev/null 2>&1 \
    && echo "OK leaf serves its own /api/health" || echo "FAIL leaf serves nothing locally — remote would be blank"
echo DONE-LEAF
EOF
GUEST="$LEAF"
p="$(guest_payload "$RECEIPTS/leaf.sh")"
vm term "$LEAF" bash "$p" 2>&1 | tee "$RECEIPTS/leaf.log"

# ---- HUB: it now lists the leaf, and can reach back into it -----------------
cat > "$RECEIPTS/hub-after.sh" <<EOF
#!/bin/bash
set -u
H="\$HOME/.local/bin/jstack-host"
echo "== hub sees the leaf it adopted =="
LV="\$("\$H" leaves 2>&1)"
echo "\$LV" | sed 's/^/  leaves: /'
echo "\$LV" | grep -qi "$LEAF" && echo "OK hub lists the leaf $LEAF" || echo "FAIL hub does not list the leaf"
echo DONE-HUB-AFTER
EOF
GUEST="$HUB"
p="$(guest_payload "$RECEIPTS/hub-after.sh")"
vm term "$HUB" bash "$p" 2>&1 | tee "$RECEIPTS/hub-after.log"

vm shot "$LEAF" "$RECEIPTS/leaf-final.png" || true
vm shot "$HUB"  "$RECEIPTS/hub-final.png"  || true

# ---- verdict: every marker reached, no FAIL line anywhere -------------------
cat "$RECEIPTS/hub.log" "$RECEIPTS/leaf.log" "$RECEIPTS/hub-after.log" > "$RECEIPTS/all.log"
ok=1
for m in DONE-HUB DONE-LEAF DONE-HUB-AFTER; do
    grep -q "$m" "$RECEIPTS/all.log" || { echo "VERDICT: FAIL — run never reached $m"; ok=0; }
done
if grep -q '^FAIL' "$RECEIPTS/all.log"; then echo "VERDICT: FAIL"; grep '^FAIL' "$RECEIPTS/all.log"; ok=0; fi
[ "$ok" = 1 ] && { echo "VERDICT: PASS"; grep '^OK' "$RECEIPTS/all.log"; exit 0; } || exit 1
