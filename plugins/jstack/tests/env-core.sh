#!/usr/bin/env bash
# jStack live test — the environment's dispatcher (hooks/trigger-dispatch.sh) and its
# place in hooks.json.
#
# What it pins:
#   - ONE ENTRY PER EVENT, APPENDED LAST. Codex keys hook trust by an entry's position:
#     a group inserted above an existing one shifts every later ordinal and silently
#     un-trusts whatever moved. The dispatcher groups are written out literally below,
#     so a group added above one fails here instead of disarming Codex.
#   - AN UNARMED EVENT NEVER STARTS THE HOST. `.armed` lists the events some trigger
#     listens to; every other event drains stdin and exits in the shell.
#   - THE HOST IS FOUND BY ADDRESS — PATH, then ~/.local/bin, then the checkout venv —
#     the search stop-compact-delivery.sh learned when a removed symlink silenced it.
#   - THE REGISTRY AND ITS CONDITIONS: host/tests/test_triggers.py, run last.
#
# Exit 0 = all pass, exit 1 = any fail.

set -u

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="$(cd "$PLUGIN_ROOT/../../host" && pwd)"
HOOK="$PLUGIN_ROOT/hooks/trigger-dispatch.sh"
PY="${JSTACK_TEST_PYTHON:-python3}"

[[ -x "$HOOK" ]] || { echo "FAIL: $HOOK not executable" >&2; exit 1; }

TMP=$(mktemp -d "${TMPDIR:-/tmp}/jstack-env-core.XXXXXX")
trap 'rm -rf "$TMP"' EXIT

fails=0
check() { if [ "$2" = "0" ]; then echo "ok: $1"; else echo "FAIL: $1"; fails=$((fails + 1)); fi; }

HOME_DIR="$TMP/home"
TRIG="$HOME_DIR/.config/jstack/triggers"
mkdir -p "$TRIG"
mk_stub() {
    mkdir -p "$(dirname "$1")"
    printf '#!/bin/sh\necho "$0 $*" >> "%s/ran"\ncat > "%s/stdin"\n' "$TMP" "$TMP" > "$1"
    chmod +x "$1"
}
LOCAL_BIN="$HOME_DIR/.local/bin/jstack-host"
CHECKOUT_HOST="$HOME_DIR/jStack/host/.venv/bin/jstack-host"
ELSEWHERE="$TMP/onpath/jstack-host"
mk_stub "$LOCAL_BIN"; mk_stub "$CHECKOUT_HOST"; mk_stub "$ELSEWHERE"

run() { echo '{"session_id":"s"}' | env -i HOME="$HOME_DIR" PATH="$1" "${@:3}" sh "$HOOK" "$2"; }

# 1. No .armed yet: ask the host, and PATH wins.
run "$TMP/onpath:/usr/bin:/bin" Stop
grep -q "^$ELSEWHERE trigger dispatch Stop$" "$TMP/ran" 2>/dev/null
check "with nothing armed yet, the host on PATH is asked" $?
grep -q '"session_id":"s"' "$TMP/stdin"; check "the payload reaches the host on stdin" $?

# 2. An event nothing listens to never starts the host.
printf 'Stop\n' > "$TRIG/.armed"; rm -f "$TMP/ran"
run "$TMP/onpath:/usr/bin:/bin" PreToolUse
[ ! -f "$TMP/ran" ]; check "an unarmed event exits in the shell" $?
run "$TMP/onpath:/usr/bin:/bin" Stop
[ -f "$TMP/ran" ]; check "an armed event reaches the host" $?

# 3. Off PATH, the fallbacks in order.
rm -f "$TMP/ran"
run "/usr/bin:/bin" Stop
grep -q "^$LOCAL_BIN " "$TMP/ran"; check "off PATH, ~/.local/bin answers" $?
rm -f "$LOCAL_BIN" "$TMP/ran"
run "/usr/bin:/bin" Stop
grep -q "^$CHECKOUT_HOST " "$TMP/ran"; check "with the symlink gone, the checkout venv answers" $?

# 4. The skip switch and no host at all: silent, green, nothing run.
rm -f "$TMP/ran"
run "/usr/bin:/bin" Stop SKIP_SESSION_HOOK=1
[ $? = 0 ] && [ ! -f "$TMP/ran" ]; check "SKIP_SESSION_HOOK runs nothing" $?
rm -f "$CHECKOUT_HOST"
out=$(run "/usr/bin:/bin" Stop); rc=$?
[ $rc = 0 ] && [ -z "$out" ] && [ ! -f "$TMP/ran" ]; check "no host anywhere exits 0 and says nothing" $?

# 5. The retired compact hook no longer delivers (the trigger does), and still answers --which.
mk_stub "$CHECKOUT_HOST"; rm -f "$TMP/ran"
echo '{}' | env -i HOME="$HOME_DIR" PATH=/usr/bin:/bin sh "$PLUGIN_ROOT/hooks/stop-compact-delivery.sh"
[ ! -f "$TMP/ran" ]; check "stop-compact-delivery.sh no longer delivers" $?

# 6. The ordinals, pinned: the dispatcher is its event's LAST group, alone in it.
"$PY" - "$PLUGIN_ROOT/hooks/hooks.json" <<'PYEOF'
import json, sys
m = json.load(open(sys.argv[1]))["hooks"]
for event, group in (("SessionStart", 1), ("UserPromptSubmit", 2), ("PreToolUse", 6),
                     ("PostToolUse", 4), ("Stop", 2), ("PreCompact", 1), ("SessionEnd", 1)):
    gs = m[event]
    assert group == len(gs) - 1, f"{event}: the dispatcher group is no longer last"
    [h] = gs[group]["hooks"]
    assert h["command"] == "${CLAUDE_PLUGIN_ROOT}/hooks/trigger-dispatch.sh " + event, h
    assert "matcher" not in gs[group], f"{event}: the dispatcher sees every tool"
PYEOF
check "one dispatcher group per event, appended last" $?

# 7. The registry, the conditions, the fire log, compact-on-delivery on both engines.
( cd "$HOST" && "$PY" -m pytest -q tests/test_triggers.py >"$TMP/pytest.log" 2>&1 )
rc=$?; tail -1 "$TMP/pytest.log"; check "host/tests/test_triggers.py" $rc

[ "$fails" = "0" ] || { echo "$fails check(s) failed" >&2; exit 1; }
echo "all checks passed"
