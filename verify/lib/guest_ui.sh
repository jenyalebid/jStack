# Sourced by app payloads INSIDE the guest. Everything here drives the two
# products through the surfaces a person uses — the hub's menu bar item, the
# client's File menu, the window list — and reads its answers off the same
# ones. The caller sets DONE before sourcing; a bail prints it, because
# finish_verdict reads a missing marker as "never reached" and stops before it
# prints the FAIL line that says why.
#
# Nothing here reaches for an API. A journey that asks "what did the person
# see" and answers from a port has proven a different sentence.

set -u
say() { printf '%s\n' "$*"; }
bail() { echo "FAIL $*"; echo "${DONE:-DONE}"; exit 0; }

ui_shot() { screencapture -x "$HOME/$1.png"; }

# Window titles of a running app, one per line. Joined on a linefeed inside
# AppleScript rather than split on a comma out here: a session window's title
# is the agent's own text and carries commas of its own.
ui_windows() {  # <process name>
    osascript <<AS 2>/dev/null
tell application "System Events" to tell process "$1"
    set AppleScript's text item delimiters to linefeed
    return (name of every window) as text
end tell
AS
}

# What the client did with the `jremote://` links it was handed — its own
# record, over the loopback listener the board owns (BoardControl.swift).
# Empty is itself an answer: nothing was ever handed to the app.
ui_link_trace() { printf 'LINK-TRACE\n' | nc -w 2 127.0.0.1 52845 2>/dev/null; }

# Start a chat the way the menu offers it: File ▸ New Chat ▸ the first agent.
# The submenu is addressed by name off its own item — `menu "New Chat" of menu
# item "New Chat"` — and never by index off a stale reference: `menu 1 of
# <item>` read after clicking that item returned -10000 on the first run of
# full/first-session. Two of the File menu's rows report as `missing value`
# (SwiftUI items carrying an image), so an index into that menu is not a name.
ui_new_chat() {
    osascript <<'AS' 2>&1
tell application "jRemote" to activate
delay 1
tell application "System Events"
    tell process "jRemote"
        click menu bar item "File" of menu bar 1
        delay 1
        set AppleScript's text item delimiters to ", "
        log "file menu: " & ((name of every menu item of menu "File" of menu bar 1) as text)
        set parent to menu item "New Chat" of menu "File" of menu bar 1
        click parent
        delay 1
        set rows to name of every menu item of menu "New Chat" of parent
        log "new chat: " & (rows as text)
        if (count of rows) is 0 then
            key code 53
            error "New Chat offers no agent"
        end if
        click menu item 1 of menu "New Chat" of parent
        return item 1 of rows
    end tell
end tell
AS
}
