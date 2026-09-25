#!/bin/bash
# USER: User drives the Mac app through every journey
# WHAT: jRemoteUITests against the Mac app inside the Xcode guest
# TIME: ~45m
# GUEST: derived from $JSTACK_VERIFY_XCODE_BASE (a guest with Xcode)
. "$(dirname "$0")/../../lib/common.sh"

SRC="${JSTACK_VERIFY_CLIENT_SRC:-$HOME/Projects/jRemote-Project/jRemote-Code}"
[ -d "$SRC/jRemote/jRemote.xcodeproj" ] || { echo "FAIL no client checkout at $SRC"; exit 1; }

guest_from "${JSTACK_VERIFY_XCODE_BASE:-jr-xcode-base}" vfy-device-uitests-mac

tar -C "$(dirname "$SRC")" -czf "$RECEIPTS/client-src.tgz" "$(basename "$SRC")"
vm cp "$GUEST" "$RECEIPTS/client-src.tgz" /Users/admin/client-src.tgz

cat > "$RECEIPTS/payload.sh" <<'EOF'
#!/bin/bash
set -u
tar -xzf ~/client-src.tgz -C ~
cd ~/jRemote-Code/jRemote
xcodebuild test -project jRemote.xcodeproj -scheme jRemote \
    -destination 'platform=macOS' -only-testing:jRemoteUITests 2>&1 | tail -40
rc=${PIPESTATUS[0]}
[ "$rc" = 0 ] && echo "OK jRemoteUITests (macOS) passed" || echo "FAIL jRemoteUITests exit $rc"
echo DONE-DEVICE-MAC
EOF

p="$(guest_payload "$RECEIPTS/payload.sh")"
VM_TERM_TIMEOUT=3600 guest_term bash "$p"
finish_verdict "$RECEIPTS/term.log" DONE-DEVICE-MAC
