#!/bin/bash
# USER: User opens the app onto the local hub after a full reset
# WHAT: after a full reset, the hub menu reads "· local" — never managed/offline/ghost — and jRemote opens onto this Mac
# TIME: ~20m
# GUEST: pristine
#
# Born 2026-09-22: a reset proven by process checks alone shipped a menu bar
# that still named a five-day-dead "M2 Pro Mac mini · managed · offline" and
# an empty Instances view. The verdict here is read off the same surfaces
# the user reads — the status menu's own item titles and the client's
# window — never inferred from a process table.
#
# What a healthy fresh install actually shows (established on a live guest,
# same day): the menu names the machine by HARDWARE MODEL ("M2 Pro Mac
# mini", "M2 Max (Virtual) …"), never by ComputerName — so the ghost is not
# detectable by name. The state line is: "Port 9090 · local" when healthy,
# "· managed · offline" in the defect. The status item may be in menu bar 1
# or 2 depending on session state — always address the LAST menu bar.
. "$(dirname "$0")/../../lib/common.sh"

ASSET_URL="$(gh release view --repo jenyalebid/jStack \
    --json assets --jq '.assets[] | select(.name | startswith("jRemote-")) | .url' | head -1)"
[ -n "$ASSET_URL" ] || { echo "FAIL no jRemote client asset on the latest release"; exit 1; }

guest_fresh vfy-full-instances

sed "s|@ASSET_URL@|$ASSET_URL|" > "$RECEIPTS/payload.sh" <<'EOF'
#!/bin/bash
set -u
export JSTACK_ROOT=/Users/admin/Desktop/Alpine
mkdir -p "$JSTACK_ROOT"

echo "== seed ghost app data, then the README full reset =="
C="$HOME/Library/Containers/dev.jenya.jRemote"
mkdir -p "$C/Data"; echo ghost > "$C/Data/hosts.marker"
security add-generic-password -U -s jRemote -a jremote.host.token.GHOST -w oldsecret
curl -fsSL https://raw.githubusercontent.com/jenyalebid/jStack/main/install.sh | bash -s -- --purge
rm -rf ~/jStack
curl -fsSL https://raw.githubusercontent.com/jenyalebid/jStack/main/install.sh | bash -s -- --agent Jarvis

echo "== the client =="
curl -fsSL -o ~/Downloads/jRemote.zip "@ASSET_URL@"
ditto -x -k ~/Downloads/jRemote.zip /Applications/
open /Applications/jRemote.app
sleep 15

echo "== read the hub status menu the way the user does =="
MENU="$(osascript 2>&1 <<'AS'
tell application "System Events"
    tell process "JStackHostBar"
        set mb to menu bar item 1 of menu bar (count of menu bars)
        ignoring application responses
            click mb
        end ignoring
    end tell
end tell
delay 2
set names to {}
tell application "System Events"
    tell process "JStackHostBar"
        set mb to menu bar item 1 of menu bar (count of menu bars)
        set names to name of every menu item of menu 1 of mb
        key code 53
    end tell
end tell
set AppleScript's text item delimiters to linefeed
return names as text
AS
)"
echo "menu items:"; echo "$MENU" | sed 's/^/  | /'
echo "$MENU" | grep -q "execution error" \
    && echo "FAIL menu read failed (harness, not product): $MENU"

echo "== verdict =="
echo "$MENU" | grep -qi "offline" && echo "FAIL menu shows an offline entry after a fresh reset" \
    || echo "OK nothing offline in the menu"
echo "$MENU" | grep -qi "managed" && echo "FAIL fresh standalone install claims to be hub-managed" \
    || echo "OK not managed"
echo "$MENU" | grep -q "· local" && echo "OK menu shows this Mac as local" \
    || echo "FAIL menu has no '· local' line"
FIRST="$(echo "$MENU" | head -1)"
[ -n "$FIRST" ] && echo "OK menu names a machine: $FIRST" || echo "FAIL menu names no machine"
# A fresh install IS the latest release — offering it an update is the
# fleet grading "no job history" as "available" (fleet_updates.py inventory).
echo "$MENU" | grep -q "Update Available" \
    && echo "FAIL fresh install at the latest release advertises an update to itself" \
    || echo "OK no self-update offered"

WINS="$(osascript -e 'tell application "System Events" to tell process "jRemote" to get name of every window' 2>&1)"
echo "jRemote windows: $WINS"
echo "$WINS" | grep -q "Home" \
    && echo "OK client opened onto this Mac (Home window)" \
    || echo "FAIL client did not open onto the local instance (windows: $WINS)"
echo DONE-FULL-INSTANCES
EOF

p="$(guest_payload "$RECEIPTS/payload.sh")"
guest_term bash "$p"
finish_verdict "$RECEIPTS/term.log" DONE-FULL-INSTANCES
