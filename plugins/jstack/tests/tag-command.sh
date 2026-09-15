#!/usr/bin/env bash
# jStack live test — hooks/tag-command.py (`/tag` answered without a turn).
#
# Pipes fixture UserPromptSubmit JSON through the real hook against a temp
# JSTACK_TIMELINE_DIR (hermetic — never touches the real timeline), and checks
# both halves of every branch: what the user is told, and whether the prompt
# was stopped. Exit 2 IS the feature here — a branch that answers on exit 0
# has printed its answer AND spent the turn it exists to save, so every
# assertion checks the code as well as the text.
#
#   - a prompt that is not /tag passes through untouched (exit 0, silent)
#   - bare /tag says what this session carries, then the rest of the names
#   - the answer carries no use counts and no descriptions
#   - every answer goes out on BOTH channels — the stdout JSON the harness
#     renders, and the stderr text it falls back to — and they agree
#   - unknown name + trailing words mints, then attaches
#   - unknown name alone refuses, and names the near miss instead
#   - a carried tag toggles off; -name says it outright
#   - /jstack:tag and the JSTACK_TAG_CMD stub expansion are the same command
#   - no session id → refuses rather than tagging nothing
#
# Exit 0 = all pass, exit 1 = any fail.

set -u
unset CODEX_THREAD_ID

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$PLUGIN_ROOT/hooks/tag-command.py"

[[ -x "$HOOK" ]] || { echo "FAIL: $HOOK not executable" >&2; exit 1; }

TMP=$(mktemp -d /tmp/jstack-tag-cmd-test.XXXXXX)
trap 'rm -rf "$TMP"' EXIT
export JSTACK_TIMELINE_DIR="$TMP"

fails=0
pass() { echo "ok: $1"; }
fail() { echo "FAIL: $1" >&2; fails=$((fails+1)); }

# Runs the hook on one prompt. Sets $OUT (the stderr text — what the user sees
# when the harness ignores the JSON), $JSON (stdout — what it renders when it
# doesn't) and $CODE. All three come back because a right answer on the wrong
# exit code is a bug, and an answer on only one channel is half a hook.
run() {
  local prompt="$1" sid="${2-s-one}"
  OUT=$(printf '{"session_id":"%s","prompt":%s}' "$sid" \
        "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$prompt")" \
        | "$HOOK" 2>&1 >"$TMP/stdout")
  CODE=$?
  JSON=$(cat "$TMP/stdout")
}

# The `reason` the harness would put on screen, or empty if stdout was not the
# block payload. Read out rather than grepped: the point is that it parses.
reason() {
  python3 - "$JSON" <<'PY' 2>/dev/null
import json, sys
d = json.loads(sys.argv[1])
h = d.get("hookSpecificOutput") or {}
assert d.get("decision") == "block", d
assert h.get("hookEventName") == "UserPromptSubmit", d
assert h.get("suppressOriginalPrompt") is True, d
print(d["reason"], end="")
PY
}

# 1. Anything that is not /tag is none of this hook's business
run "what did we ship yesterday"
[[ $CODE == 0 && -z "$OUT" ]] \
  && pass "an ordinary prompt passes through" || fail "passthrough (code=$CODE out=$OUT)"

# 2. Bare /tag on an empty vocabulary says how to start one
run "/tag"
[[ $CODE == 2 && "$OUT" == *"no tags yet"* ]] \
  && pass "empty vocabulary explains the mint" || fail "empty list (code=$CODE out=$OUT)"

# 3. Unknown name + trailing words: mint, then attach
run "/tag deploys everything about shipping builds to devices"
[[ $CODE == 2 && "$OUT" == *"tagged"*"deploys"* ]] \
  && pass "a described unknown name mints and attaches" || fail "mint (code=$CODE out=$OUT)"

# 4. The list marks what this session carries — and only this session
run "/tag"
[[ "$OUT" == *"● deploys"* ]] \
  && pass "list marks the carried tag" || fail "carried marker ($OUT)"
run "/tag" "s-two"
[[ "$OUT" != *"● deploys"* && "$OUT" == *"deploys"* ]] \
  && pass "another session sees it uncarried" || fail "marker leaked across sessions ($OUT)"
[[ "$OUT" == "○ untagged"* ]] \
  && pass "a session carrying nothing says so" || fail "untagged head ($OUT)"

# 4b. Names and nothing else. The counts and descriptions are real, and they
#     belong to the question `log_event tag list` answers — putting them here
#     turned a one-line answer into a table.
run "/tag" "s-two"
[[ "$OUT" != *"sessions"* && "$OUT" != *"shipping builds"* ]] \
  && pass "no use counts, no descriptions" || fail "listing carries bloat ($OUT)"
[[ "$(printf '%s' "$OUT" | wc -l | tr -d ' ')" == "1" ]] \
  && pass "the answer is two lines" || fail "line count ($OUT)"

# 4c. Both channels carry the same answer, and the stdout one is the block
#     payload the harness renders without welding the hook's path onto it.
run "/tag"
[[ "$(reason)" == "$OUT" ]] \
  && pass "the JSON reason is the text on stderr" \
  || fail "channels disagree (json=$(reason) stderr=$OUT)"
[[ "$JSON" != *"$HOOK"* ]] \
  && pass "the answer does not name the hook that printed it" \
  || fail "hook path leaked into the answer ($JSON)"

# 5. Naming a carried tag takes it off — one word, no verb
run "/tag deploys"
[[ $CODE == 2 && "$OUT" == *"untagged"*"deploys"* ]] \
  && pass "a carried tag toggles off" || fail "toggle off (code=$CODE out=$OUT)"

# 6. ...and naming it again puts it back
run "/tag deploys"
[[ "$OUT" == *"tagged"*"deploys"* && "$OUT" != *"untagged"* ]] \
  && pass "a known tag toggles back on" || fail "toggle on ($OUT)"

# 7. An unknown name alone is refused, with the near miss named. The gate is
#    the point: a tag with no description is one the next session cannot match.
run "/tag deployment"
[[ $CODE == 2 && "$OUT" == *"no tag 'deployment'"* && "$OUT" == *"deploys"* ]] \
  && pass "an undescribed unknown name is refused, near miss named" \
  || fail "undescribed mint refused (code=$CODE out=$OUT)"
run "/tag"
[[ "$OUT" != *"deployment"* ]] \
  && pass "the refused name was not minted anyway" || fail "refusal still minted ($OUT)"

# 8. -name removes without the toggle having to be reasoned about
run "/tag -deploys"
[[ $CODE == 2 && "$OUT" == *"untagged"* ]] \
  && pass "-name removes outright" || fail "explicit unset (code=$CODE out=$OUT)"

# 9. All three spellings are one command — raw, namespaced, and the stub's
#    expansion, since which one reaches a hook is the harness's business
run "/jstack:tag deploys"
[[ "$OUT" == *"tagged"*"deploys"* ]] \
  && pass "/jstack:tag is the same command" || fail "namespaced form ($OUT)"
run "JSTACK_TAG_CMD deploys"
[[ "$OUT" == *"untagged"* ]] \
  && pass "the stub expansion is the same command" || fail "sentinel form ($OUT)"
run '$jstack:tag deploys'
[[ $CODE == 2 && "$OUT" == *"tagged"*"deploys"* ]] \
  && pass "native skill spelling blocks and tags" || fail "native spelling ($OUT)"
run '$jstack:tag deploys'
[[ $CODE == 2 && "$OUT" == *"untagged"* ]] \
  && pass "native spelling toggles the same tag" || fail "native toggle ($OUT)"

# 10. Case and a leading # normalize the way the CLI normalizes them
run "/tag #DEPLOYS"
[[ "$OUT" == *"tagged"*"deploys"* && "$OUT" != *"no tag"* ]] \
  && pass "name normalizes (case, leading #)" || fail "normalization ($OUT)"

# 11. No session id → say so, rather than attach a tag to nothing
OUT=$(printf '{"prompt":"/tag deploys"}' | "$HOOK" 2>&1); CODE=$?
[[ $CODE == 2 && "$OUT" == *"no session"* ]] \
  && pass "a sessionless payload is refused" || fail "no session (code=$CODE out=$OUT)"

# 12. Malformed stdin is the harness's problem, not the user's — stay out of it
OUT=$(printf 'not json' | "$HOOK" 2>&1); CODE=$?
[[ $CODE == 0 && -z "$OUT" ]] \
  && pass "unparseable payload passes through" || fail "bad stdin (code=$CODE out=$OUT)"

echo ""
if [[ $fails -gt 0 ]]; then
  echo "$fails check(s) failed" >&2
  exit 1
fi
echo "ALL PASS — /tag answers every branch without a turn"
