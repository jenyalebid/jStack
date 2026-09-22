#!/bin/bash
# WHAT: jRemoteUITests on an iOS simulator inside the Xcode guest — the client driven by XCUITest
# TIME: ~45m
# GUEST: derived from $JSTACK_VERIFY_XCODE_BASE (a guest with Xcode and simulators)
#
# The client source ships in from the local checkout: the app repo is private
# and the guest holds no credentials. The tests themselves are the product's
# own UI suite, not probes written here.
. "$(dirname "$0")/../../lib/common.sh"

SRC="${JSTACK_VERIFY_CLIENT_SRC:-$HOME/Projects/jRemote-Project/jRemote-Code}"
[ -d "$SRC/jRemote/jRemote.xcodeproj" ] || { echo "FAIL no client checkout at $SRC"; exit 1; }

guest_from "${JSTACK_VERIFY_XCODE_BASE:-jr-xcode-base}" vfy-device-uitests-ios

tar -C "$(dirname "$SRC")" -czf "$RECEIPTS/client-src.tgz" "$(basename "$SRC")"
vm cp "$GUEST" "$RECEIPTS/client-src.tgz" /Users/admin/client-src.tgz

cat > "$RECEIPTS/payload.sh" <<'EOF'
#!/bin/bash
set -u
tar -xzf ~/client-src.tgz -C ~
cd ~/jRemote-Code/jRemote
DEST="$(xcrun simctl list devices available | grep -Eo 'iPhone [0-9]+[^(]*' | head -1 | sed 's/ *$//')"
echo "simulator: ${DEST:-none}"
[ -n "$DEST" ] || { echo "FAIL no available iPhone simulator"; echo DONE-DEVICE-IOS; exit 0; }
xcodebuild test -project jRemote.xcodeproj -scheme jRemote \
    -destination "platform=iOS Simulator,name=$DEST" \
    -only-testing:jRemoteUITests 2>&1 | tail -40
rc=${PIPESTATUS[0]}
[ "$rc" = 0 ] && echo "OK jRemoteUITests passed" || echo "FAIL jRemoteUITests exit $rc"
echo DONE-DEVICE-IOS
EOF

p="$(guest_payload "$RECEIPTS/payload.sh")"
VM_TERM_TIMEOUT=3600 guest_term bash "$p"
finish_verdict "$RECEIPTS/term.log" DONE-DEVICE-IOS
