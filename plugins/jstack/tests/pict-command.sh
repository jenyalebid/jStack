#!/usr/bin/env bash
# jStack live test — hooks/pict-command.py (`/pict` answered without a turn).
#
# Pipes fixture UserPromptSubmit JSON through the real hook against a temp tree,
# with a fake renderer and fake openers on PATH — this test is about placement,
# view selection and failure behaviour, not about what `pict` prints. Exit 2 IS
# the feature: a branch that answers on exit 0 has printed its answer AND spent
# the turn it exists to save, so every assertion checks the code as well.
#
#   - a prompt that is not /pict passes through untouched (exit 0, silent)
#   - bare /pict renders the session's own cwd, in the bare view
#   - a leading directory argument renders that instead; flags pass through
#   - --full is the hook's flag: it drops --bare and is never passed on
#   - the render lands in the workspace's pad when there is one, and in the
#     jStack directory under CLAUDE_CONFIG_DIR when there isn't
#   - the name is stable, so a second ask refreshes one document
#   - a failed render leaves nothing behind and does not replace a good one
#   - the host's own document router wins over the generic opener
#   - no opener at all still writes the file and reports its path
#   - /jstack:pict and the JSTACK_PICT_CMD stub expansion are the same command
#
# Exit 0 = all pass, exit 1 = any fail.

set -u
unset CODEX_THREAD_ID

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$PLUGIN_ROOT/hooks/pict-command.py"

[[ -x "$HOOK" ]] || { echo "FAIL: $HOOK not executable" >&2; exit 1; }

# Physical, not the /tmp symlink: the hook resolves the directory it is asked
# to render, so a test comparing against an unresolved path fails on macOS for
# a reason that has nothing to do with the hook.
TMP=$(cd "$(mktemp -d /tmp/jstack-pict-cmd-test.XXXXXX)" && pwd -P)
trap 'rm -rf "$TMP"' EXIT

SEAT="$TMP/seat"; mkdir -p "$SEAT/pad"
BARE="$TMP/bare"; mkdir -p "$BARE"          # a workspace with no pad
OTHER="$TMP/other"; mkdir -p "$OTHER"       # something to render by name
CFG="$TMP/cfg"; mkdir -p "$CFG"
BIN="$TMP/bin"; mkdir -p "$BIN"
export CLAUDE_CONFIG_DIR="$CFG"

fails=0
pass() { echo "ok: $1"; }
fail() { echo "FAIL: $1" >&2; fails=$((fails+1)); }

# The renderer, faked: echoes the arguments it was handed, so a test can prove
# WHAT was rendered and in which view. Overridden per-case by `renderer`.
renderer() { printf '%s\n' '#!/bin/bash' "$1" > "$TMP/pict"; chmod +x "$TMP/pict"; }
renderer 'echo "PICT $*"'

# Openers, faked: each records that it was called and with what.
opener() {   # opener <name> <exit-code>
  printf '%s\n' '#!/bin/bash' "echo \"$1 \$*\" >> \"$TMP/opened\"" "exit $2" \
    > "$BIN/$1"
  chmod +x "$BIN/$1"
}

# The fake renderer is pointed at, never swapped in for the real one: this is a
# shared checkout, and a test that moves `bin/pict` aside gives every other
# session on the machine the fake for as long as it runs.
export JSTACK_PICT_BIN="$TMP/pict"

run() {   # run <prompt> [cwd]
  local prompt="$1" cwd="${2-$SEAT}"
  OUT=$(python3 -c 'import json,sys; print(json.dumps({"prompt":sys.argv[1],"cwd":sys.argv[2],"session_id":"s1"}))' \
        "$prompt" "$cwd" \
        | PATH="$BIN:/usr/bin:/bin" "$HOOK" 2>&1 >"$TMP/stdout")
  CODE=$?
}

# 1. Anything that is not /pict is none of this hook's business
run "what does this seat load"
[[ $CODE == 0 && -z "$OUT" ]] \
  && pass "an ordinary prompt passes through" || fail "passthrough (code=$CODE out=$OUT)"

# 2. Bare /pict renders the session's own directory, in the reading copy
run "/pict"
DOC="$SEAT/pad/pict-seat.md"
[[ $CODE == 2 && -f "$DOC" ]] \
  && pass "bare /pict renders into the pad" || fail "pad render (code=$CODE out=$OUT)"
[[ "$(cat "$DOC")" == "PICT $SEAT --bare" ]] \
  && pass "the session's own directory, bare" || fail "render args ($(cat "$DOC"))"
[[ "$OUT" == *"seat · pict"* && "$OUT" == *"$DOC"* ]] \
  && pass "the answer names the document and its path" || fail "answer ($OUT)"

# 3. The name is stable — a second ask refreshes one document, never stacks two
run "/pict"
[[ "$(ls "$SEAT/pad" | wc -l | tr -d ' ')" == "1" ]] \
  && pass "asking twice refreshes one document" || fail "stacked ($(ls "$SEAT/pad"))"

# 4. --full is the hook's own flag: it drops --bare and is not passed on
run "/pict --full"
[[ "$(cat "$DOC")" == "PICT $SEAT" ]] \
  && pass "--full drops the bare default and is not forwarded" || fail "full ($(cat "$DOC"))"

# 5. A leading directory is the target; everything after it is the renderer's
run "/pict $OTHER --exec-hooks"
OTHER_DOC="$SEAT/pad/pict-other.md"
[[ -f "$OTHER_DOC" && "$(cat "$OTHER_DOC")" == "PICT $OTHER --bare --exec-hooks" ]] \
  && pass "a named directory renders, flags pass through" \
  || fail "named dir ($(cat "$OTHER_DOC" 2>/dev/null))"
# ...and it still lands in the pad of the seat this was RUN from, which is
# where the person who asked will read it.
[[ "$(dirname "$OTHER_DOC")" == "$SEAT/pad" ]] \
  && pass "a render of elsewhere lands in this seat's pad" || fail "pad of record"

# 6. A workspace with no pad falls back to the jStack directory
run "/pict" "$BARE"
[[ -f "$CFG/jstack/pict/pict-bare.md" ]] \
  && pass "no pad → the jStack directory under the config" \
  || fail "fallback ($(ls -R "$CFG"))"

# 7. The host's own document router wins — it knows which screen is driving
rm -f "$TMP/opened"
opener show-doc 0
opener open-artifact 0
run "/pict"
[[ "$(cat "$TMP/opened")" == "show-doc $DOC --title seat · pict" ]] \
  && pass "show-doc wins, and is given the title" || fail "router ($(cat "$TMP/opened"))"

# 8. ...and when it refuses, the generic opener still gets its turn
rm -f "$TMP/opened"; opener show-doc 1
run "/pict"
[[ "$(grep -c open-artifact "$TMP/opened")" == "1" ]] \
  && pass "a refusing router falls through to the opener" || fail "fallthrough ($(cat "$TMP/opened"))"

# 9. No opener at all is not a failure — the render is the product
rm -f "$BIN/show-doc" "$BIN/open-artifact" "$TMP/opened"
run "/pict"
[[ $CODE == 2 && "$OUT" == *"$DOC"* && ! -f "$TMP/opened" ]] \
  && pass "no opener still writes the file and names it" || fail "openerless ($OUT)"

# 10. A failed render leaves nothing half-written, and keeps the last good one
GOOD="$(cat "$DOC")"
renderer 'echo "half a document"; echo "no walk-up here" >&2; exit 1'
run "/pict"
[[ $CODE == 2 && "$OUT" == *"no walk-up here"* ]] \
  && pass "a failed render says why" || fail "render failure (code=$CODE out=$OUT)"
[[ "$(cat "$DOC")" == "$GOOD" ]] \
  && pass "the last good document is untouched" || fail "clobbered ($(cat "$DOC"))"
[[ -z "$(find "$SEAT/pad" -name '.pict-*')" ]] \
  && pass "no half-written temp left behind" || fail "temp leaked ($(ls -a "$SEAT/pad"))"

# 11. All three spellings are one command — raw, namespaced, and the stub's
renderer 'echo "PICT $*"'
run "/jstack:pict"
[[ $CODE == 2 && "$OUT" == *"seat · pict"* ]] \
  && pass "/jstack:pict is the same command" || fail "namespaced ($OUT)"
run "JSTACK_PICT_CMD"
[[ $CODE == 2 && "$OUT" == *"seat · pict"* ]] \
  && pass "the stub expansion is the same command" || fail "sentinel ($OUT)"
run '$jstack:pict'
[[ $CODE == 2 && "$OUT" == *"seat · pict"* ]] \
  && pass "native skill spelling blocks and renders" || fail "native spelling ($OUT)"

# 12. Malformed stdin is the harness's problem, not the user's — stay out of it
OUT=$(printf 'not json' | "$HOOK" 2>&1); CODE=$?
[[ $CODE == 0 && -z "$OUT" ]] \
  && pass "unparseable payload passes through" || fail "bad stdin (code=$CODE out=$OUT)"

echo ""
if [[ $fails -gt 0 ]]; then
  echo "$fails check(s) failed" >&2
  exit 1
fi
echo "ALL PASS — /pict renders, places and opens without a turn"
