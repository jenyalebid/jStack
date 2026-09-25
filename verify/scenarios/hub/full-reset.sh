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
echo "installed release: $(sed -n 's/.*"release": "\([^"]*\)".*/\1/p' "/Applications/jStack Hub.app/Contents/Resources/packages/release-identity.json" 2>/dev/null)"
sleep 5
~/jStack/plugins/jstack/bin/jstack-scheduler status >/dev/null 2>&1 \
    && echo "OK scheduler service healthy" || echo "FAIL scheduler status exit $?"
# Login Items & Extensions groups its rows by the app that REGISTERED each job,
# and `parent bundle identifier` is the field it groups on — readable in the user
# domain without root. One bundle across every jStack job IS one row, which is
# the whole point of the scheduler being a Hub service rather than a LaunchAgent
# of its own.
rows_ok=1; rows_seen=0
for l in $(launchctl list | awk '{print $3}' | grep -i jstack); do
    rows_seen=$((rows_seen + 1))
    if launchctl print "gui/$(id -u)/$l" 2>/dev/null \
            | grep -q 'parent bundle identifier = live.jstack.hub'; then
        echo "  bundle: $l -> live.jstack.hub"
    else
        echo "  bundle: $l -> NOT registered by the Hub"
        rows_ok=0
    fi
done
[ "$rows_seen" -gt 0 ] && [ "$rows_ok" = 1 ] \
    && echo "OK all $rows_seen jstack jobs answer to one bundle — one Login Items row" \
    || echo "FAIL jstack jobs are registered by more than one bundle, or none were found"
launchctl print "gui/$(id -u)/live.jstack.hub.scheduler" 2>/dev/null | grep -q 'state = running' \
    && echo "OK the scheduler runs as the Hub's own sealed service" \
    || echo "FAIL live.jstack.hub.scheduler is not a running Hub service"
ls ~/Library/LaunchAgents 2>/dev/null | grep -qi jstack \
    && echo "FAIL a jstack plist sits in ~/Library/LaunchAgents — registered by no app, so its own row" \
    || echo "OK no jstack plist in ~/Library/LaunchAgents"
pgrep -f JStackHostBar >/dev/null && echo "OK menu bar running" || echo "FAIL menu bar not running"
echo DONE-FULL-RESET
EOF

p="$(guest_payload "$RECEIPTS/payload.sh")"
guest_term bash "$p"
finish_verdict "$RECEIPTS/term.log" DONE-FULL-RESET
