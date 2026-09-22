#!/bin/bash
# WHAT: fresh Mac, README one-liner from the CDN — hub, scheduler, menu bar, doctor all alive
# TIME: ~10m
# GUEST: pristine
. "$(dirname "$0")/../../lib/common.sh"

guest_fresh vfy-hub-install

cat > "$RECEIPTS/payload.sh" <<'EOF'
#!/bin/bash
set -u
export JSTACK_ROOT=/Users/admin/Desktop/Alpine
mkdir -p "$JSTACK_ROOT"

echo "== README install, from the CDN =="
curl -fsSL https://raw.githubusercontent.com/jenyalebid/jStack/main/install.sh \
    | bash -s -- --agent Jarvis

echo "== verdict =="
rel="$(sed -n 's/.*"release": "\([^"]*\)".*/\1/p' ~/jStack/host/release-identity.json 2>/dev/null)"
[ -n "$rel" ] && echo "OK installed release: $rel" || echo "FAIL no release identity"
sleep 5
if ~/jStack/plugins/jstack/bin/jstack-scheduler status >/dev/null 2>&1; then
    echo "OK scheduler service healthy (status exit 0)"
else
    echo "FAIL scheduler status exit $?"
fi
grep -q "Library/Logs/jstack-scheduler" ~/Library/LaunchAgents/com.jstack.scheduler.plist 2>/dev/null \
    && echo "OK scheduler plist uses safe log paths" || echo "FAIL plist points logs into the root"
pgrep -f JStackHostBar >/dev/null && echo "OK menu bar running" || echo "FAIL menu bar not running"
launchctl list | grep -i jstack | sed 's/^/  launchd: /'
~/jStack/plugins/jstack/bin/jstack-doctor 2>&1 | grep -i 'versions' | sed 's/^/  doctor: /'
~/jStack/plugins/jstack/bin/jstack-doctor 2>&1 | grep -qi 'shipped copy' \
    && echo "OK doctor grades the shipped copy" || echo "FAIL doctor mis-grades the install"
echo DONE-HUB-INSTALL
EOF

p="$(guest_payload "$RECEIPTS/payload.sh")"
guest_term bash "$p"
finish_verdict "$RECEIPTS/term.log" DONE-HUB-INSTALL
