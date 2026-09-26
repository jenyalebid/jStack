#!/bin/bash
# USER: User runs the installer a second time on a Mac that already has jStack
# WHAT: the re-run adds no second menu bar, no second host and no unsealed agent — one installation, one icon
# TIME: ~15m
# GUEST: pristine
#
# Journey 1's single-installation invariant, checked where it can actually
# break. Every other scenario installs once onto a pristine guest, so the
# installer's "a Hub is already here and answering" branch had no journey at
# all — and that branch fell through to the unsealed `host/install.sh`, which
# put `com.jremote.menubar` beside the Hub's own `live.jstack.hub.menu`. A
# first install showed one icon and every re-run showed two.
#
# The leaf joiner runs the same installer whenever a Mac's release does not
# match the hub's, so this is also the shape a person hits from "add a leaf".
. "$(dirname "$0")/../../lib/common.sh"

guest_fresh vfy-hub-reinstall

# The ref under test, in both the URL and the --ref: a release candidate has to
# be provable before it is main.
REF="${JSTACK_VERIFY_REF:-main}"

cat > "$RECEIPTS/payload.sh" <<EOF
#!/bin/bash
set -u
REF="$REF"
EOF
cat >> "$RECEIPTS/payload.sh" <<'EOF'
export JSTACK_ROOT=/Users/admin/Desktop/Alpine
mkdir -p "$JSTACK_ROOT"
URL="https://raw.githubusercontent.com/jenyalebid/jStack/$REF/install.sh"
# A branch that is not a release line installs only as a debug build.
DEBUG=""
case "$REF" in main|dev) ;; *) DEBUG=--debug ;; esac

bars()   { pgrep -f JStackHostBar | wc -l | tr -d ' '; }
agents() { launchctl list | awk '{print $3}' | grep -c '^com\.jremote\.' || true; }

echo "== first install ($REF) =="
curl -fsSL "$URL" | bash -s -- --agent Jarvis --ref "$REF" $DEBUG

FIRST_BARS="$(bars)"
HUB_ID="/Applications/jStack Hub.app/Contents/Resources/packages/release-identity.json"
FIRST_REL="$(sed -n 's/.*"release": "\([^"]*\)".*/\1/p' "$HUB_ID" 2>/dev/null)"
echo "   after one install: $FIRST_BARS menu bar process(es), release ${FIRST_REL:-none}"

echo "== second install, same Mac, same command =="
curl -fsSL "$URL" | bash -s -- --agent Jarvis --ref "$REF" $DEBUG

echo "== verdict =="
# One process and one registration. Counted, not probed for presence: the old
# check was `pgrep -f JStackHostBar` for a single truthy answer, which passes
# just as happily with two icons on the bar as with one.
SECOND_BARS="$(bars)"
[ "$SECOND_BARS" = 1 ] \
    && echo "OK one menu bar process after the re-run" \
    || echo "FAIL $SECOND_BARS menu bar processes after the re-run (was $FIRST_BARS)"
pgrep -f "JStack Host.app/Contents/MacOS/JStackHostBar" >/dev/null \
    && echo "FAIL the unsealed menu bar app is running beside the Hub's" \
    || echo "OK no unsealed menu bar app"
[ -d ~/Library/Application\ Support/jStack/JStack\ Host.app ] \
    && echo "FAIL the unsealed menu bar bundle was installed" \
    || echo "OK no unsealed menu bar bundle on disk"
# The labels the unsealed installer registers. They are `com.jremote.*`, not
# `*jstack*`, which is why the fresh-install journey's LaunchAgents check could
# not see them.
[ "$(agents)" = 0 ] \
    && echo "OK no com.jremote.* job registered" \
    || echo "FAIL $(agents) com.jremote.* job(s) registered: $(launchctl list | awk '{print $3}' | grep '^com\.jremote\.' | tr '\n' ' ')"
ls ~/Library/LaunchAgents 2>/dev/null | grep -q . \
    && echo "FAIL a plist sits in ~/Library/LaunchAgents: $(ls ~/Library/LaunchAgents | tr '\n' ' ')" \
    || echo "OK ~/Library/LaunchAgents is empty — every job answers to the Hub bundle"
# The wrapper is how every later step and every agent reaches the CLI. The
# unsealed installer replaces it with a link into host/.venv, which leaves the
# Mac driving the unsealed CLI against the sealed Hub's state.
grep -q JStackCLI ~/.local/bin/jstack-host 2>/dev/null \
    && echo "OK jstack-host still runs the sealed Hub's CLI" \
    || echo "FAIL jstack-host no longer points at the sealed CLI: $(cat ~/.local/bin/jstack-host 2>/dev/null | tr '\n' ' ')"
# A re-run that leaves the Mac on a different release than it installed has
# rebuilt or replaced a working Hub, which is the other half of "it changes
# only what has drifted".
SECOND_REL="$(sed -n 's/.*"release": "\([^"]*\)".*/\1/p' "$HUB_ID" 2>/dev/null)"
[ -n "$SECOND_REL" ] && [ "$SECOND_REL" = "$FIRST_REL" ] \
    && echo "OK the Hub kept its release ($SECOND_REL)" \
    || echo "FAIL release went from ${FIRST_REL:-none} to ${SECOND_REL:-none}"
curl -fsS -m 10 http://127.0.0.1:9090/api/health >/dev/null 2>&1 \
    && echo "OK host answering on 9090 after the re-run" \
    || echo "FAIL host not answering after the re-run"
launchctl list | grep -iE 'jstack|jremote' | sed 's/^/  launchd: /'
echo DONE-HUB-REINSTALL
EOF

p="$(guest_payload "$RECEIPTS/payload.sh")"
guest_term bash "$p"
guest_shot menubar-after-reinstall
finish_verdict "$RECEIPTS/term.log" DONE-HUB-REINSTALL
