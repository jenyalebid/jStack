#!/bin/bash
# USER: User fresh installs the hub
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
# The identity moved with the install shape. A release snapshot carried it in
# the checkout; a checkout is a git clone and carries no such file. What this
# Mac runs is the bundle it compiled, and the bundle is where the identity is
# — sealed under Contents/Resources, so it cannot be edited after signing.
HUB_ID="/Applications/jStack Hub.app/Contents/Resources/packages/release-identity.json"
rel="$(sed -n 's/.*"release": "\([^"]*\)".*/\1/p' "$HUB_ID" 2>/dev/null)"
[ -n "$rel" ] && echo "OK installed release: $rel" || echo "FAIL no release identity"
# `origin` is written only by a machine that compiled the bundle for itself,
# and a publisher's release carries none — so this is the one honest answer to
# "did this Mac build what it is running".
grep -q '"kind"[[:space:]]*:[[:space:]]*"source-build"' "$HUB_ID" 2>/dev/null \
    && echo "OK built on this Mac (origin: source-build)" \
    || echo "FAIL the Hub does not record this Mac as its builder"
[ -d ~/jStack/.git ] && echo "OK the install is a checkout, not a snapshot" \
    || echo "FAIL ~/jStack is not a git checkout"
curl -fsS -m 10 http://127.0.0.1:9090/api/health >/dev/null 2>&1 \
    && echo "OK host answering on 9090" || echo "FAIL host not answering"
sleep 5
if ~/jStack/plugins/jstack/bin/jstack-scheduler status >/dev/null 2>&1; then
    echo "OK scheduler service healthy (status exit 0)"
else
    echo "FAIL scheduler status exit $?"
fi
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
launchctl list | grep -i jstack | sed 's/^/  launchd: /'
~/jStack/plugins/jstack/bin/jstack-doctor 2>&1 | grep -i 'versions' | sed 's/^/  doctor: /'
# "shipped copy" was the old answer: a release snapshot served the plugin.
# A checkout serves it now, and the doctor says so in those words.
~/jStack/plugins/jstack/bin/jstack-doctor 2>&1 | grep -qi 'serves from the checkout' \
    && echo "OK doctor grades the install as a checkout" || echo "FAIL doctor mis-grades the install"
echo DONE-HUB-INSTALL
EOF

p="$(guest_payload "$RECEIPTS/payload.sh")"
guest_term bash "$p"
finish_verdict "$RECEIPTS/term.log" DONE-HUB-INSTALL
