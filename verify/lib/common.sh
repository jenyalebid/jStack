# Sourced by every scenario. Provides a GUI guest, a visible way to run
# things in it, and a verdict that only reads what it observed.
#
# Scenario contract: set $GUEST via guest_fresh, ship a payload with
# guest_payload, run it with guest_term, screenshot with guest_shot, and end
# with finish_verdict <log> <done-marker>. A payload prints "FAIL …" lines
# for anything wrong and its done-marker last; a run that never reaches the
# marker is a failure, not a silence.
set -u

# The driver that boots and talks to a guest. Whatever boots VMs here:
# named by $VM_SH, or `vm.sh` on PATH. There is no default path — a
# guessed one belongs to whoever wrote the guess, not to whoever is
# running the suite.
VM="${VM_SH:-$(command -v vm.sh 2>/dev/null || true)}"
: "${VM:?set VM_SH to the script that boots your test VMs}"
TART="${TART:-/opt/homebrew/bin/tart}"
: "${RECEIPTS:?run through verify/run.sh}"
GUEST=""

vm() { "$VM" "$@"; }

# A pristine guest: thrown away and re-cloned from the base image every run.
guest_fresh() {
    GUEST="$1"
    vm rm "$GUEST" >/dev/null 2>&1 || true
    vm up "$GUEST"
}

# A guest derived from a prepared working image (authed Claude Code, Xcode …).
# The source image stays stopped and is never booted itself.
guest_from() {
    local base="$1"; GUEST="$2"
    "$TART" list 2>/dev/null | grep -q "^local *$base " || {
        echo "FAIL derived base '$base' does not exist" >&2; exit 1; }
    vm rm "$GUEST" >/dev/null 2>&1 || true
    "$TART" clone "$base" "$GUEST"
    vm gui "$GUEST"
}

# The lab sign-in. The authed base image's interactive Claude login expires
# (jStack #140), and a session that cannot sign in fails every work journey on
# its first prompt. The acceptance guests carry a long-lived OAuth token from
# the credentials dir instead (git/acceptance/provision_guest.py); a verify
# guest gets the same one, the same two ways: ~/.zshenv for the login shell the
# guest Terminal opens the payload in, launchctl setenv for Terminal itself,
# which `open` launches through launchd. It goes in before the first
# guest_term — Terminal reads its environment once, at launch. The token never
# enters the payload, which is copied into the receipts, and is never printed.
guest_signin() {
    local tok="${JSTACK_VERIFY_OAUTH_TOKEN_FILE:-$HOME/Operations/Infrastructure/Credentials/claude_code_oauth_token}"
    [ -s "$tok" ] || { echo "FAIL no lab sign-in token at $tok (#140)" >&2; exit 1; }
    printf 'export CLAUDE_CODE_OAUTH_TOKEN=%q\n' "$(cat "$tok")" \
        | vm ssh "$GUEST" 'cat >> ~/.zshenv' >/dev/null 2>&1 \
        || { echo "FAIL could not place the lab sign-in in $GUEST" >&2; exit 1; }
    vm ssh "$GUEST" 'source ~/.zshenv && /bin/launchctl setenv CLAUDE_CODE_OAUTH_TOKEN "$CLAUDE_CODE_OAUTH_TOKEN"' >/dev/null 2>&1 \
        || { echo "FAIL could not hand the lab sign-in to launchd in $GUEST" >&2; exit 1; }
}

guest_payload() {  # <local-file> -> /Users/admin/<basename>, executable
    local src="$1" dst="/Users/admin/$(basename "$1")"
    vm cp "$GUEST" "$src" "$dst" >/dev/null
    vm ssh "$GUEST" "chmod +x $dst" >/dev/null 2>&1
    echo "$dst"
}

guest_term() {  # run visibly in the guest's own Terminal, log kept
    vm term "$GUEST" "$@" 2>&1 | tee -a "$RECEIPTS/term.log"
}

guest_shot() { vm shot "$GUEST" "$RECEIPTS/$1.png"; }

guest_fetch() {  # <remote-path> — copy a file out of the guest into receipts
    local ip; ip="$(vm ip "$GUEST" 2>/dev/null | tail -1)"
    [ -n "$ip" ] && scp -q -o StrictHostKeyChecking=no -o BatchMode=yes \
        "admin@$ip:$1" "$RECEIPTS/" 2>/dev/null || true
}

finish_verdict() {  # <log> <done-marker>
    local log="$1" marker="$2"
    guest_shot final || true
    if ! grep -q "$marker" "$log"; then
        echo "VERDICT: FAIL — run never reached $marker"; return 1
    fi
    if grep -q '^FAIL' "$log"; then
        echo "VERDICT: FAIL"; grep '^FAIL' "$log"; return 1
    fi
    echo "VERDICT: PASS"; grep '^OK' "$log" || true
}
