#!/usr/bin/env bash
# jStack live test — hooks/stop-compact-delivery.sh (how the Stop hook finds its engine).
#
# What it pins, and why each one is here rather than assumed:
#   - PATH WINS. An explicitly installed `jstack-host` is the one that runs, because an
#     install is a decision and a fallback is a guess.
#   - A MISSING SYMLINK NO LONGER SILENCES THE HOOK. This is the whole reason the file
#     was rewritten: on 2026-09-24 ~/.local/bin/jstack-host disappeared while the host
#     stayed installed and working, `command -v` returned nothing, and the hook exited 0
#     on every Stop for hours. No session was compacted and nothing reported it. With
#     the name gone the hook must still reach the checkout venv.
#   - NO HOST AT ALL IS STILL SILENT AND STILL GREEN. Delivery is opt-in; a machine that
#     never installed a host is not broken, and turning that into noise is how a real
#     signal gets ignored.
#   - `--which` NEVER DELIVERS, and answers even under the skip switch — a suppressed
#     delivery still has to tell a probe where its engine is, or the probe grades a
#     machine it cannot see.
#
# Exit 0 = all pass, exit 1 = any fail.

set -u

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$PLUGIN_ROOT/hooks/stop-compact-delivery.sh"

[[ -x "$HOOK" ]] || { echo "FAIL: $HOOK not executable" >&2; exit 1; }

TMP=$(mktemp -d /tmp/jstack-compact-delivery-test.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

fails=0
check() {  # check <name> <condition-as-exit-status-already-evaluated>
    if [ "$2" = "0" ]; then echo "ok: $1"; else echo "FAIL: $1"; fails=$((fails + 1)); fi
}

# A fake home with a fake host in each of the two places install.sh writes. The stub
# records that it ran and with what, so "the hook delivered" is read off disk rather
# than inferred from an exit code the real engine also returns when it does nothing.
mk_stub() {  # mk_stub <path>
    mkdir -p "$(dirname "$1")"
    cat > "$1" <<STUB
#!/bin/sh
echo "\$0 \$*" >> "$TMP/ran"
STUB
    chmod +x "$1"
}

HOME_DIR="$TMP/home"
CHECKOUT_HOST="$HOME_DIR/jStack/host/.venv/bin/jstack-host"
LOCAL_BIN="$HOME_DIR/.local/bin/jstack-host"
ELSEWHERE="$TMP/onpath/jstack-host"

mk_stub "$CHECKOUT_HOST"
mk_stub "$LOCAL_BIN"
mk_stub "$ELSEWHERE"

run() { env -i HOME="$HOME_DIR" PATH="$1" sh "$HOOK" "${@:2}"; }

# 1. PATH wins over both fallbacks.
got=$(run "$TMP/onpath:/usr/bin:/bin" --which 2>/dev/null)
[ "$got" = "$ELSEWHERE" ]; check "an installed jstack-host on PATH is the one used" $?

# 2. The symlink is gone from PATH's reach — the fallback must still find a host. This
#    is the exact 2026-09-24 failure.
got=$(run "/usr/bin:/bin" --which 2>/dev/null)
[ "$got" = "$LOCAL_BIN" ]; check "off PATH, the hook still resolves ~/.local/bin" $?

rm -f "$LOCAL_BIN"
got=$(run "/usr/bin:/bin" --which 2>/dev/null)
[ "$got" = "$CHECKOUT_HOST" ]; check "with the symlink deleted, the checkout venv answers" $?

# 3. `--which` answers under the skip switch: a probe must not be blinded by a mute.
got=$(env -i HOME="$HOME_DIR" PATH="/usr/bin:/bin" SKIP_SESSION_HOOK=1 \
      sh "$HOOK" --which 2>/dev/null)
[ "$got" = "$CHECKOUT_HOST" ]; check "--which answers even when delivery is switched off" $?

# 4. `--which` is a question, not a delivery.
[ ! -f "$TMP/ran" ]; check "--which never runs the engine" $?

# 5. A real Stop reaches the engine through the fallback, not just the query path.
run "/usr/bin:/bin" </dev/null >/dev/null 2>&1
grep -q "compact-delivery" "$TMP/ran" 2>/dev/null
check "a Stop with no jstack-host on PATH still delivers" $?

# 6. No host anywhere: silent, green, and nothing run.
rm -f "$CHECKOUT_HOST"
rm -f "$TMP/ran"
run "/usr/bin:/bin" </dev/null >/dev/null 2>&1
check "a machine with no host exits 0" $?
[ ! -f "$TMP/ran" ]; check "a machine with no host runs nothing" $?

# ...but it must say so when asked directly, or a probe cannot tell an opt-out from
# an outage.
run "/usr/bin:/bin" --which >/dev/null 2>&1
[ $? -ne 0 ]; check "--which fails loudly when there is no host to find" $?

[ "$fails" = "0" ] || { echo "$fails check(s) failed" >&2; exit 1; }
echo "all checks passed"
