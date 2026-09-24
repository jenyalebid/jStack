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
