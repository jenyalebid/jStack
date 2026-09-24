#!/bin/bash
# WHAT: README full reset kills 5-day-old ghost app state (container + keychain) and comes back alive
# TIME: ~15m
# GUEST: pristine
#
# The 2026-09-22 regression scenario: purge used to leave the sandboxed app's
# Core Data container and keychain tokens behind, so every "clean" reinstall
# resurrected dead paired hosts; the scheduler died at spawn (launchd exit 78)
# when its plist pointed logs into a TCC-protected root under ~/Desktop.
. "$(dirname "$0")/../../lib/common.sh"

guest_fresh vfy-hub-full-reset

cat > "$RECEIPTS/payload.sh" <<'EOF'
#!/bin/bash
set -u
export JSTACK_ROOT=/Users/admin/Desktop/Alpine
mkdir -p "$JSTACK_ROOT"
C="$HOME/Library/Containers/dev.jenya.jRemote"

echo "== seed ghost app data (the 5-day-old state) =="
mkdir -p "$C/Data"; echo ghost > "$C/Data/hosts.marker"
security add-generic-password -U -s jRemote -a jremote.host.token.GHOST -w oldsecret
echo seeded

echo "== README full reset (--agent answers the one interactive question) =="
curl -fsSL https://raw.githubusercontent.com/jenyalebid/jStack/main/install.sh | bash -s -- --purge
rm -rf ~/jStack
curl -fsSL https://raw.githubusercontent.com/jenyalebid/jStack/main/install.sh | bash -s -- --agent Jarvis

echo "== verdict =="
# A fresh install legitimately re-creates the container; what must be dead is
# the seeded ghost state, not the folder.
[ -f "$C/Data/hosts.marker" ] && echo "FAIL ghost container data survived" || echo "OK ghost container data gone"
security find-generic-password -s jRemote -a jremote.host.token.GHOST >/dev/null 2>&1 \
    && echo "FAIL ghost keychain token survived" || echo "OK ghost keychain token gone"
echo "installed release: $(sed -n 's/.*"\(build\|release\)": "\([^"]*\)".*/\2/p' "$(p=/Applications/jStack\ Hub.app/Contents/Resources/packages; [ -f "$p/build-identity.json" ] && echo "$p/build-identity.json" || echo "$p/release-identity.json")" 2>/dev/null)"
sleep 5
~/jStack/plugins/jstack/bin/jstack-scheduler status >/dev/null 2>&1 \
    && echo "OK scheduler service healthy" || echo "FAIL scheduler status exit $?"
grep -q "Library/Logs/jstack-scheduler" ~/Library/LaunchAgents/com.jstack.scheduler.plist 2>/dev/null \
    && echo "OK plist uses safe log paths" || echo "FAIL plist still points logs at the root"
pgrep -f JStackHostBar >/dev/null && echo "OK menu bar running" || echo "FAIL menu bar not running"
echo DONE-FULL-RESET
EOF

p="$(guest_payload "$RECEIPTS/payload.sh")"
guest_term bash "$p"
finish_verdict "$RECEIPTS/term.log" DONE-FULL-RESET
