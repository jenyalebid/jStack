#!/bin/bash
# WHAT: a hub that stands alone — installs, calls itself LOCAL (never "managed · offline"), serves, and can actually mint a leaf
# TIME: ~10m
# GUEST: pristine
#
# The independent-hub configuration: one Mac, no parent. The 2026-09-22
# screenshot showed a standalone Mac reading "managed · offline" with no local
# instance — a hub calling itself somebody's leaf. This pins the opposite: a
# fresh hub is LOCAL, answers on its own port, and — the real test of an
# INDEPENDENT hub, not just an installed one — can mint a host code, because a
# hub that cannot adopt a leaf is not a hub, it is a client that lies about it.
. "$(dirname "$0")/../../lib/common.sh"

guest_fresh vfy-hub-independent

cat > "$RECEIPTS/payload.sh" <<'EOF'
#!/bin/bash
set -u
export JSTACK_ROOT=/Users/admin/Desktop/Alpine
mkdir -p "$JSTACK_ROOT"
H="$HOME/.local/bin/jstack-host"

echo "== README install, from the CDN =="
curl -fsSL https://raw.githubusercontent.com/jenyalebid/jStack/main/install.sh \
    | bash -s -- --agent Jarvis
sleep 5

echo "== verdict: this is an independent hub =="
rel="$(sed -n 's/.*"release": "\([^"]*\)".*/\1/p' ~/jStack/host/release-identity.json 2>/dev/null)"
[ -n "$rel" ] && echo "OK installed release: $rel" || echo "FAIL no release identity"

# 1. It calls itself LOCAL. Not managed, not open. This is the screenshot bug.
MODE="$("$H" mode 2>&1 | head -3)"
case "$MODE" in
    *managed*) echo "FAIL a standalone hub reads MANAGED — it thinks it is a leaf: $MODE" ;;
    *local*)   echo "OK mode is local: $(echo "$MODE" | head -1)" ;;
    *)         echo "FAIL mode is neither local nor managed: $MODE" ;;
esac

# 2. It is up and answering on its own port — a local instance exists.
if "$H" status >/dev/null 2>&1; then echo "OK host status: installed, loaded, answering"
else echo "FAIL host status exit $? — not answering"; fi
HEALTH="$(curl -fsS http://127.0.0.1:9090/api/health 2>/dev/null)"
echo "$HEALTH" | grep -q '"ok":true\|"ok": true' && echo "OK /api/health served" || echo "FAIL /api/health did not serve"
echo "$HEALTH" | grep -q '"managed":false\|"managed": false' && echo "OK health says managed=false" || echo "FAIL health does not say managed=false"
hrel="$(echo "$HEALTH" | sed -n 's/.*"release": *"\([^"]*\)".*/\1/p')"
[ -n "$hrel" ] && echo "OK health carries the release ($hrel)" || echo "FAIL health carries no release — the version is not single-sourced"
[ "$hrel" = "$rel" ] && echo "OK health release matches release-identity ($hrel)" || echo "FAIL health release '$hrel' != identity '$rel' — two versions disagree"

# 3. The real test of an INDEPENDENT hub: it can mint a leaf. A hub that
#    cannot adopt is a client. --json so we read the outcome, not a paragraph.
ADOPT="$("$H" adopt vfy-selftest-leaf --json 2>&1)"
if echo "$ADOPT" | grep -q '"code"'; then
    echo "OK independent hub minted a host code (it can adopt a leaf)"
else
    echo "FAIL independent hub cannot mint a leaf code — not a real hub:"
    echo "$ADOPT" | sed 's/^/    adopt: /'
fi
# It should have zero leaves attached (it just minted a code, nobody redeemed).
"$H" leaves >/dev/null 2>&1 && echo "OK leaves query answers" || echo "FAIL leaves query errored"

echo "menu bar (read the glass in the screenshot):"
pgrep -f JStackHostBar >/dev/null && echo "OK menu bar running" || echo "FAIL menu bar not running"
echo DONE-HUB-INDEPENDENT
EOF

p="$(guest_payload "$RECEIPTS/payload.sh")"
guest_term bash "$p"
finish_verdict "$RECEIPTS/term.log" DONE-HUB-INDEPENDENT
