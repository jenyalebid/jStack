#!/bin/bash
# WHAT: full product on one Mac — hub via README, then the published jRemote client lands and launches
# TIME: ~15m
# GUEST: pristine
. "$(dirname "$0")/../../lib/common.sh"

# The exact client artifact the current published release pins — never a local build.
ASSET_URL="$(gh release view --repo jenyalebid/jStack \
    --json assets --jq '.assets[] | select(.name | startswith("jRemote-")) | .url' | head -1)"
[ -n "$ASSET_URL" ] || { echo "FAIL no jRemote client asset on the latest release"; exit 1; }
echo "client asset: $ASSET_URL"

guest_fresh vfy-full-client-install

sed "s|@ASSET_URL@|$ASSET_URL|" > "$RECEIPTS/payload.sh" <<'EOF'
#!/bin/bash
set -u
export JSTACK_ROOT=/Users/admin/Desktop/Alpine
mkdir -p "$JSTACK_ROOT"

echo "== hub first, from the CDN =="
curl -fsSL https://raw.githubusercontent.com/jenyalebid/jStack/main/install.sh \
    | bash -s -- --agent Jarvis

echo "== the published client =="
curl -fsSL -o ~/Downloads/jRemote.zip "@ASSET_URL@"
ditto -x -k ~/Downloads/jRemote.zip /Applications/
[ -d /Applications/jRemote.app ] && echo "OK jRemote.app installed" || echo "FAIL client did not land"
open /Applications/jRemote.app
sleep 10

echo "== verdict =="
pgrep -x jRemote >/dev/null && echo "OK client running" || echo "FAIL client not running"
pgrep -f JStackHostBar >/dev/null && echo "OK hub menu bar running" || echo "FAIL menu bar not running"
codesign -v /Applications/jRemote.app 2>/dev/null && echo "OK client signature valid" \
    || echo "FAIL client signature invalid"
echo DONE-FULL-CLIENT
EOF

p="$(guest_payload "$RECEIPTS/payload.sh")"
guest_term bash "$p"
guest_shot client-on-screen
finish_verdict "$RECEIPTS/term.log" DONE-FULL-CLIENT
