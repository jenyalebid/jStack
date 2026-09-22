#!/bin/bash
# WHAT: pairing through the real UI — hub menu bar "Pair a Device", client "Add a Mac", code typed in
# TIME: ~20m
# GUEST: pristine
#
# The one scenario that must never be proven through an API: the pairing code
# is read from the hub's own window and typed into the client's own dialog,
# exactly as a user does it. Every step leaves a screenshot; the UI element
# tree is dumped alongside, so a layout change fails loudly with the map in
# the receipts instead of silently clicking nothing.
. "$(dirname "$0")/../../lib/common.sh"

ASSET_URL="$(gh release view --repo jenyalebid/jStack \
    --json assets --jq '.assets[] | select(.name | startswith("jRemote-")) | .url' | head -1)"
[ -n "$ASSET_URL" ] || { echo "FAIL no jRemote client asset on the latest release"; exit 1; }

guest_fresh vfy-full-pair


sed "s|@ASSET_URL@|$ASSET_URL|" > "$RECEIPTS/payload.sh" <<'EOF'
#!/bin/bash
set -u
export JSTACK_ROOT=/Users/admin/Desktop/Alpine
mkdir -p "$JSTACK_ROOT"
curl -fsSL https://raw.githubusercontent.com/jenyalebid/jStack/main/install.sh \
    | bash -s -- --agent Jarvis
curl -fsSL -o ~/Downloads/jRemote.zip "@ASSET_URL@"
ditto -x -k ~/Downloads/jRemote.zip /Applications/
open /Applications/jRemote.app
sleep 8

echo "== open Pair a Device from the menu bar =="
osascript <<'AS' 2>&1
tell application "System Events"
    tell process "JStackHostBar"
        click menu bar item 1 of menu bar 2
        delay 1
        set names to name of every menu item of menu 1 of menu bar item 1 of menu bar 2
        log names
        click menu item "Pair a Device" of menu 1 of menu bar item 1 of menu bar 2
    end tell
end tell
AS
sleep 3
screencapture -x ~/pair-1-hub-code.png

echo "== read the code off the hub window =="
CODE="$(osascript -e 'tell application "System Events" to tell process "JStackHostBar" to get value of every static text of window 1' 2>/dev/null | tr ',' '\n' | grep -Eo '[0-9]{4,8}' | head -1)"
echo "pairing code read from window: ${CODE:-NONE}"
[ -n "$CODE" ] || { echo "FAIL could not read a pairing code from the hub window"; \
    osascript -e 'tell application "System Events" to tell process "JStackHostBar" to entire contents of window 1' > ~/pair-ui-tree.txt 2>&1; }

echo "== Add a Mac in the client, type address and code =="
osascript <<AS 2>&1
tell application "jRemote" to activate
delay 1
tell application "System Events"
    tell process "jRemote"
        try
            click menu item "Add a Mac" of menu "File" of menu bar 1
        on error
            log (name of every menu item of menu "File" of menu bar 1)
        end try
        delay 2
        keystroke "127.0.0.1"
        keystroke tab
        keystroke "$CODE"
        delay 1
        keystroke return
    end tell
end tell
AS
sleep 5
screencapture -x ~/pair-2-client.png

echo "== verdict =="
security find-generic-password -s jRemote >/dev/null 2>&1 \
    && echo "OK client holds a hub token in the keychain" \
    || echo "FAIL no token minted — pairing did not complete"
echo DONE-FULL-PAIR
EOF

p="$(guest_payload "$RECEIPTS/payload.sh")"
guest_term bash "$p"
guest_fetch /Users/admin/pair-1-hub-code.png
guest_fetch /Users/admin/pair-2-client.png
guest_fetch /Users/admin/pair-ui-tree.txt
finish_verdict "$RECEIPTS/term.log" DONE-FULL-PAIR
