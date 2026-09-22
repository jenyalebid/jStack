#!/bin/bash
# WHAT: README full reset on a machine that was ADOPTED as a managed leaf — the attachment must come off with everything else
# TIME: ~15m
# GUEST: pristine
#
# The 2026-09-22 regression scenario, second half: the leaf attachment lives in
# /Library as root, beyond every rm the purge did. A reset machine kept reading
# the daemon's presence, called itself "managed · offline" forever, and the app
# showed no local instance. The purge now detaches (install.sh 3686e0b); this
# pins it.
. "$(dirname "$0")/../../lib/common.sh"

guest_fresh vfy-hub-adopted-reset

cat > "$RECEIPTS/payload.sh" <<'EOF'
#!/bin/bash
set -u
export JSTACK_ROOT=/Users/admin/Desktop/Alpine
mkdir -p "$JSTACK_ROOT"
C="$HOME/Library/Containers/dev.jenya.jRemote"
echo admin | sudo -S true 2>/dev/null

echo "== seed the wreck: ghost app data + adopted-leaf attachment =="
mkdir -p "$C/Data"; echo ghost > "$C/Data/hosts.marker"
security add-generic-password -U -s jRemote -a jremote.host.token.GHOST -w oldsecret
sudo tee /Library/LaunchDaemons/com.jremote.leaf.plist >/dev/null <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.jremote.leaf</string>
  <key>ProgramArguments</key><array><string>/usr/bin/true</string></array>
  <key>RunAtLoad</key><false/>
</dict></plist>
PLIST
echo seeded

echo "== README full reset (--agent answers the one interactive question) =="
curl -fsSL https://raw.githubusercontent.com/jenyalebid/jStack/main/install.sh | bash -s -- --purge
rm -rf ~/jStack
curl -fsSL https://raw.githubusercontent.com/jenyalebid/jStack/main/install.sh | bash -s -- --agent Jarvis

echo "== verdict =="
[ -f "$C/Data/hosts.marker" ] && echo "FAIL ghost container data survived" || echo "OK ghost container data gone"
security find-generic-password -s jRemote -a jremote.host.token.GHOST >/dev/null 2>&1 \
    && echo "FAIL ghost keychain token survived" || echo "OK ghost keychain token gone"
[ -f /Library/LaunchDaemons/com.jremote.leaf.plist ] \
    && echo "FAIL leaf attachment survived — this Mac still reads managed" || echo "OK leaf attachment gone"
MODE="$("$HOME/.local/bin/jstack-host" mode 2>/dev/null | head -1)"
case "$MODE" in
    *local*) echo "OK mode is local: $MODE" ;;
    *)       echo "FAIL mode is not local: $MODE" ;;
esac
echo "installed release: $(sed -n 's/.*"release": "\([^"]*\)".*/\1/p' ~/jStack/host/release-identity.json 2>/dev/null)"
pgrep -f JStackHostBar >/dev/null && echo "OK menu bar running" || echo "FAIL menu bar not running"
echo DONE-ADOPTED-RESET
EOF

p="$(guest_payload "$RECEIPTS/payload.sh")"
guest_term bash "$p"
finish_verdict "$RECEIPTS/term.log" DONE-ADOPTED-RESET
