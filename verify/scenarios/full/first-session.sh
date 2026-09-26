#!/bin/bash
# USER: User's first chat spawns on a Mac with nothing installed
# WHAT: one README command on a pristine Mac, then New Chat from the app's own File menu — the windows on screen are the verdict
# TIME: ~20m
# GUEST: pristine
#
# The install fires two `jremote://` links of its own within seconds of putting
# the app on the disk — `pair --open`, then `welcome` — at an app that has
# never launched. Every such link makes SwiftUI stand a thread window up to
# have somewhere to deliver it, and that shell has no chat in it; a shell the
# app fails to take back off is a window titled exactly "Thread" — the blank
# windows reported from fresh Macs, more than one at a time.
#
# So the verdict is the window list, not an API: a session window carries the
# agent's own text, an empty shell carries the WindowGroup's declared title.
# The app's own record of what it did with each link rides along in the
# receipts, because "the window is missing" and "the link never arrived" are
# different bugs with the same screenshot.
. "$(dirname "$0")/../../lib/common.sh"

guest_fresh vfy-full-first-session
guest_signin
vm cp "$GUEST" "$(dirname "$0")/../../lib/guest_ui.sh" /Users/admin/guest_ui.sh >/dev/null

cat > "$RECEIPTS/payload.sh" <<EOF
#!/bin/bash
REF='${JSTACK_VERIFY_REF:-dev}'
EOF

cat >> "$RECEIPTS/payload.sh" <<'EOF'
DONE=DONE-FULL-FIRST-SESSION
. /Users/admin/guest_ui.sh
export JSTACK_ROOT=/Users/admin/Desktop/Alpine
export PATH="$HOME/.local/bin:$PATH"
mkdir -p "$JSTACK_ROOT"
SEAT="$JSTACK_ROOT/Agents/Jarvis/chat"

say "== nothing installed to begin with =="
[ -d /Applications/jRemote.app ] && bail "this guest already has jRemote — it is not the machine this journey is about"
say "  no client, no hub, no root"

say "== the one command a person runs, from $REF =="
# refs/heads/ keeps a ref with a slash in it unambiguous. The app it lands is
# the published signed release either way — install.sh:1208 downloads it
# rather than building from the ref — so the client under test is the one a
# person actually gets.
curl -fsSL "https://raw.githubusercontent.com/jenyalebid/jStack/refs/heads/$REF/install.sh" \
    | bash -s -- --yes --ref "$REF" --agent Jarvis 2>&1 | tail -45

say "== the agent the installer made =="
[ -f "$SEAT/CLAUDE.md" ] \
    && say "OK the installer made the chat seat at $SEAT" \
    || echo "FAIL no chat seat at $SEAT — every later session opens one level below whatever this install drove"

# The install's own two links land at an app that is still launching. This is
# the wait for them to be acted on, not a wait for anything to settle down.
sleep 30
pgrep -x jRemote >/dev/null || bail "the client never came up — no windows to read"

say "== what the app did with the links the install handed it =="
ui_link_trace | sed 's/^/  trace| /'

say "== what is on screen after the install =="
ui_shot ui-1-after-install
WINDOWS="$(ui_windows jRemote)"
printf '%s\n' "$WINDOWS" | sed 's/^/  window| /'

GHOSTS="$(printf '%s\n' "$WINDOWS" | grep -cx 'Thread' || true)"
[ "$GHOSTS" = 0 ] \
    && say "OK the install left no empty shell on screen" \
    || echo "FAIL the install left $GHOSTS empty 'Thread' window(s) — the blank windows on a fresh Mac"

CHATS="$(printf '%s\n' "$WINDOWS" | grep -v '^$' | grep -vx 'Thread' | grep -vx 'Home' | wc -l | tr -d ' ')"
[ "$CHATS" -ge 1 ] \
    && say "OK the install's own session is on screen as a window of its own" \
    || echo "FAIL the install opened no session window — welcome went nowhere"

say "== is an agent actually running, and where =="
CWDS="$(pgrep -f claude 2>/dev/null | while read -r p; do
    lsof -a -p "$p" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p'
done | sort -u)"
printf '%s\n' "$CWDS" | grep -v '^$' | sed 's/^/  claude cwd| /'
if printf '%s\n' "$CWDS" | grep -qx "$SEAT"; then
    say "OK a live agent is running in the chat seat"
elif [ -n "$CWDS" ]; then
    echo "FAIL an agent is running, but never in $SEAT — the first session opened somewhere else"
else
    echo "FAIL no agent process at all — nothing spawned"
fi

say "== now the person starts one themselves: File ▸ New Chat =="
BEFORE="$(printf '%s\n' "$WINDOWS" | grep -cv '^$' || true)"
ui_new_chat 2>&1 | sed 's/^/  menu| /'
sleep 15
ui_shot ui-2-after-new-chat
AFTER_WINDOWS="$(ui_windows jRemote)"
printf '%s\n' "$AFTER_WINDOWS" | sed 's/^/  window| /'
AFTER="$(printf '%s\n' "$AFTER_WINDOWS" | grep -cv '^$' || true)"

[ "$AFTER" -gt "$BEFORE" ] \
    && say "OK New Chat opened a window ($BEFORE then $AFTER)" \
    || echo "FAIL New Chat opened nothing — $BEFORE windows before, $AFTER after"

GHOSTS="$(printf '%s\n' "$AFTER_WINDOWS" | grep -cx 'Thread' || true)"
[ "$GHOSTS" = 0 ] \
    && say "OK New Chat left no empty shell either" \
    || echo "FAIL New Chat left $GHOSTS empty 'Thread' window(s)"

say "== the app's record, once more, with the spawn in it =="
ui_link_trace | sed 's/^/  trace| /'
echo "$DONE"
EOF

p="$(guest_payload "$RECEIPTS/payload.sh")"
guest_term bash "$p"
for f in ui-1-after-install.png ui-2-after-new-chat.png; do
    guest_fetch "/Users/admin/$f"
done
finish_verdict "$RECEIPTS/term.log" DONE-FULL-FIRST-SESSION
