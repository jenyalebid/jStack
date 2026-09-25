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
#
# Pictures are taken from OUTSIDE the guest. `screencapture` inside it raises
# macOS 26's private-window-picker consent dialog, which takes the keyboard and
# holds it: run 6 drove File ▸ New Chat under one and got -10000, run 7 got
# -1743 on every AppleEvent after it and read no windows at all. So the run is
# two payloads with a host-side `guest_shot` between them.
. "$(dirname "$0")/../../lib/common.sh"

# The client under test. Unset, the install downloads the published signed
# release — what a person actually gets, and the right default. Set to a
# locally built jRemote.app, that bundle is staged into /Applications first and
# the install is told --no-app: the host still fires its own two links, at the
# build being judged rather than at the last one shipped.
APP="${JSTACK_VERIFY_CLIENT_APP:-}"
STAGED=0
if [ -n "$APP" ]; then
    [ -d "$APP/Contents/MacOS" ] || { echo "FAIL no app bundle at $APP"; exit 1; }
    # Refused here rather than in the guest, where it costs a full install to
    # find out. A development-signed bundle names the Macs it may run on, and
    # a guest is never one of them — macOS refuses it at launch and the run
    # ends on an empty screen that reads exactly like the bug being chased.
    # `mac.sh` already builds the other way (CODE_SIGNING_ALLOWED=NO, then
    # `codesign --sign -` against jRemoteMac.entitlements minus the two keys a
    # profile is what grants); that bundle is what belongs here.
    if [ -f "$APP/Contents/embedded.provisionprofile" ]; then
        echo "FAIL $APP is development-signed — it will not launch in a guest."
        echo "     Build it unsigned and sign it ad-hoc; see jRemote-Code/jRemote/mac.sh."
        exit 1
    fi
    # The installer hands whatever jRemote is on the disk to the Hub build as
    # its client component, and that build refuses a bundle that cannot name
    # the commit it came from (build_source.client_component). Only
    # release-mac.sh stamps the key, so a bundle from mac.sh or a bare
    # xcodebuild has to be stamped and re-signed before it is staged — the
    # plist is inside the signature, so in that order.
    if ! defaults read "$APP/Contents/Info" JStackSourceCommit >/dev/null 2>&1; then
        echo "FAIL $APP records no JStackSourceCommit — the Hub build refuses it as a client."
        echo "     PlistBuddy the 40-hex commit into Contents/Info.plist, then re-sign."
        exit 1
    fi
    STAGED=1
fi

guest_fresh vfy-full-first-session
guest_signin
vm cp "$GUEST" "$(dirname "$0")/../../lib/guest_ui.sh" /Users/admin/guest_ui.sh >/dev/null
if [ "$STAGED" = 1 ]; then
    # tar, not `vm cp` on the directory: a .app is a tree of symlinks and an
    # executable bit, and a copy that flattens either produces a bundle that
    # launches on this Mac and not in the guest.
    tar -C "$(dirname "$APP")" -czf "$RECEIPTS/client-app.tgz" "$(basename "$APP")"
    vm cp "$GUEST" "$RECEIPTS/client-app.tgz" /Users/admin/client-app.tgz >/dev/null
fi

cat > "$RECEIPTS/payload.sh" <<EOF
#!/bin/bash
REF='${JSTACK_VERIFY_REF:-dev}'
STAGED=$STAGED
EOF

cat >> "$RECEIPTS/payload.sh" <<'EOF'
DONE=DONE-INSTALL
. /Users/admin/guest_ui.sh
export JSTACK_ROOT=/Users/admin/Desktop/Alpine
export PATH="$HOME/.local/bin:$PATH"
mkdir -p "$JSTACK_ROOT"
SEAT="$JSTACK_ROOT/Agents/Jarvis/chat"

say "== nothing installed to begin with =="
[ -d /Applications/jRemote.app ] && bail "this guest already has jRemote — it is not the machine this journey is about"
say "  no client, no hub, no root"

INSTALL_ARGS=(--yes --ref "$REF" --agent Jarvis)
if [ "$STAGED" = 1 ]; then
    tar -xzf /Users/admin/client-app.tgz -C /Applications
    [ -d /Applications/jRemote.app ] || bail "the staged client did not unpack"
    # A bundle that arrived as a tarball carries no quarantine flag, but one
    # that ever passed through a browser or an unzip does; stripping it here
    # keeps a staged build from failing on a Gatekeeper sheet nobody is at the
    # screen to dismiss.
    xattr -dr com.apple.quarantine /Applications/jRemote.app 2>/dev/null
    # A bundle untarred into /Applications is on the disk and not in
    # LaunchServices' database. The install opens the app by NAME — install.sh
    # runs `open -a jRemote` — and that asks the database, not the disk, so
    # without this the app never launches and the run ends on an empty screen
    # that reads exactly like the bug being chased. Registered, not launched:
    # the condition under test is a link arriving at an app that has never run.
    LSREG=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
    "$LSREG" -f /Applications/jRemote.app
    say "  the client under test is a local build, staged before the install"
    INSTALL_ARGS+=(--no-app)
fi

say "== the one command a person runs, from $REF =="
# refs/heads/ keeps a ref with a slash in it unambiguous. Absent a staged
# build, the app this lands is the published signed release — install.sh
# downloads it rather than building from the ref — so the default client under
# test is the one a person actually gets.
# The whole install goes to a file and only its last screenful to the terminal.
# Truncating at the screen cost a run: `tail -45` cut the app and pairing steps
# clean out, and the receipts held the same 45 lines, so the log could not say
# whether the app had been opened at all.
curl -fsSL "https://raw.githubusercontent.com/jenyalebid/jStack/refs/heads/$REF/install.sh" \
    | bash -s -- "${INSTALL_ARGS[@]}" > /Users/admin/install.log 2>&1
tail -45 /Users/admin/install.log

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
WINDOWS="$(ui_windows jRemote)"
printf '%s\n' "$WINDOWS" | sed 's/^/  window| /'

GHOSTS="$(printf '%s\n' "$WINDOWS" | grep -cx 'Thread' || true)"
[ "$GHOSTS" = 0 ] \
    && say "OK the install left no empty shell on screen" \
    || echo "FAIL the install left $GHOSTS empty 'Thread' window(s) — the blank windows on a fresh Mac"

# A window still titled "Thread" is either a shell that never got its link or a
# thread that got one and drew nothing, and the screenshot cannot tell them
# apart. This can: an empty shell has nothing in it at all, and a substituted
# gate reports its own text.
if [ "$GHOSTS" != 0 ]; then
    say "  what is inside it:"
    ui_window_contents jRemote Thread | head -25 | sed 's/^/  inside| /'
fi

# A session window is titled by the agent, never by the app: `LiveChatTitle`
# sets it to the record's preview and falls back to `agent.name`, so the
# install's own session reads "Jarvis" until its first reply lands. The app's
# own windows are the rest, and they have to be named to be excluded — an
# earlier version counted any title that was not "Thread" and passed on the
# board window alone, with no session on screen at all. A title this list
# misses shows up as the FAIL printing the window list.
CHROME='Thread|Home|Hubs|Instances|Documents|Settings'
CHATS="$(printf '%s\n' "$WINDOWS" | grep -v '^$' | grep -cvxE "$CHROME" || true)"
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

echo "$DONE"
EOF

# The person's own New Chat, in a second visit to the same Terminal: the app
# and its windows are exactly where the install left them, and the picture in
# between is taken from out here.
cat > "$RECEIPTS/payload-2.sh" <<'EOF'
#!/bin/bash
DONE=DONE-FULL-FIRST-SESSION
. /Users/admin/guest_ui.sh

say "== now the person starts one themselves: File ▸ New Chat =="
BEFORE_WINDOWS="$(ui_windows jRemote)"
BEFORE="$(printf '%s\n' "$BEFORE_WINDOWS" | grep -cv '^$' || true)"
ui_new_chat 2>&1 | sed 's/^/  menu| /'
sleep 15
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
guest_shot ui-1-after-install
# A run that never finished the install has nothing for the second half to
# drive, and a menu driven against a half-installed app fails for a reason
# that has nothing to do with the app.
if grep -q DONE-INSTALL "$RECEIPTS/term.log"; then
    p2="$(guest_payload "$RECEIPTS/payload-2.sh")"
    guest_term bash "$p2"
    guest_shot ui-2-after-new-chat
fi
guest_fetch /Users/admin/install.log
finish_verdict "$RECEIPTS/term.log" DONE-FULL-FIRST-SESSION
