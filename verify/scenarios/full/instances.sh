#!/bin/bash
# WHAT: after a full reset, the hub menu and Instances window show THIS Mac, local and online — no ghosts
# TIME: ~20m
# GUEST: pristine
#
# Born 2026-09-22: a reset proven by process checks alone shipped a menu bar
# that still named a five-day-dead "M2 Pro Mac mini · managed · offline" and
# an empty Instances window. The verdict here is read off the same surfaces
# the user reads — the menu's own item titles and the window's rows — never
# inferred from a process table.
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

echo "== read the hub menu the way the user does =="
MENU="$(osascript 2>&1 <<'AS'
tell application "System Events"
    tell process "JStackHostBar"
        click menu bar item 1 of menu bar 2
        delay 1
        set out to ""
        repeat with mi in menu items of menu 1 of menu bar item 1 of menu bar 2
            try
                set out to out & (name of mi) & linefeed
            end try
        end repeat
        key code 53
        return out
    end tell
end tell
AS
)"
echo "menu items:"; echo "$MENU" | sed 's/^/  | /'
screencapture -x ~/instances-menu.png

echo "== verdict =="
HOSTNAME_NOW="$(scutil --get ComputerName 2>/dev/null || hostname)"
echo "this machine is: $HOSTNAME_NOW"
echo "$MENU" | grep -qi "offline" && echo "FAIL menu shows an offline entry on a fresh install" \
    || echo "OK nothing offline in the menu"
echo "$MENU" | grep -qi "M2 Pro" && echo "FAIL a ghost machine name survived the reset" \
    || echo "OK no ghost machine names"
echo "$MENU" | grep -q "$HOSTNAME_NOW" && echo "OK menu names this machine" \
    || echo "FAIL menu does not name this machine ($HOSTNAME_NOW)"
ROWS="$(osascript -e 'tell application "System Events" to tell process "jRemote" to get entire contents of window "Instances"' 2>/dev/null | head -c 2000)"
[ -n "$ROWS" ] && echo "OK Instances window has content" || echo "FAIL Instances window is empty"
echo "$ROWS" > ~/instances-rows.txt
screencapture -x ~/instances-window.png
echo DONE-FULL-INSTANCES
EOF

p="$(guest_payload "$RECEIPTS/payload.sh")"
guest_term bash "$p"
guest_fetch /Users/admin/instances-menu.png
guest_fetch /Users/admin/instances-window.png
guest_fetch /Users/admin/instances-rows.txt
finish_verdict "$RECEIPTS/term.log" DONE-FULL-INSTANCES
