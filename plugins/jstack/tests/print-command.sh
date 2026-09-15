#!/usr/bin/env bash
# jStack live test — hooks/print-command.py (`/print` answered without a turn).
#
# Pipes fixture UserPromptSubmit JSON through the real hook against a temp tree.
# Exit 2 IS the feature: a branch that answers on exit 0 has printed its answer
# AND spent the turn it exists to save, so every assertion checks the code too.
#
#   - a prompt that is not /print passes through untouched (exit 0, silent)
#   - the payload's transcript_path is the answer, verbatim and alone
#   - a path the harness names but has not written yet is still the answer,
#     said as such — never suppressed, never guessed around
#   - with no transcript_path, the session-id glob answers instead
#   - the glob never degrades to "newest .jsonl": a session with no file of its
#     own is told so, even with other sessions' transcripts sitting beside it
#   - the same id under two project dirs is flagged, not silently picked from
#   - a payload with neither field fails by name
#   - /jstack:print and the JSTACK_PRINT_CMD stub expansion are one command
#
# Exit 0 = all pass, exit 1 = any fail.

set -u
unset CODEX_THREAD_ID

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$PLUGIN_ROOT/hooks/print-command.py"

[[ -x "$HOOK" ]] || { echo "FAIL: $HOOK not executable" >&2; exit 1; }

TMP=$(cd "$(mktemp -d /tmp/jstack-print-cmd-test.XXXXXX)" && pwd -P)
trap 'rm -rf "$TMP"' EXIT

# A fixture config dir, so the glob branch never reads the real ~/.claude —
# this is a shared machine and other sessions' transcripts are not fixtures.
CFG="$TMP/cfg"
PROJ="$CFG/projects/-Users-someone-seat"
mkdir -p "$PROJ"
export CLAUDE_CONFIG_DIR="$CFG"

fails=0
pass() { echo "ok: $1"; }
fail() { echo "FAIL: $1" >&2; fails=$((fails+1)); }

run() {   # run <prompt> <json-payload-extras>
  local prompt="$1" extras="${2-{\}}"
  OUT=$(python3 -c '
import json, sys
d = json.loads(sys.argv[2]); d["prompt"] = sys.argv[1]; d.setdefault("cwd", "/tmp")
print(json.dumps(d))' "$prompt" "$extras" | "$HOOK" 2>&1 >/dev/null)
  CODE=$?
}

# 1. Anything that is not /print is none of this hook's business
run "print the timeline for yesterday"
[[ $CODE == 0 && -z "$OUT" ]] \
  && pass "an ordinary prompt passes through" || fail "passthrough (code=$CODE out=$OUT)"

# 2. The harness's own answer wins — the path, alone, nothing wrapped around it
LIVE="$PROJ/aaaaaaaa-1111-2222-3333-444444444444.jsonl"
: > "$LIVE"
run "/print" "{\"transcript_path\":\"$LIVE\",\"session_id\":\"aaaaaaaa-1111-2222-3333-444444444444\"}"
[[ $CODE == 2 && "$OUT" == "$LIVE" ]] \
  && pass "the payload's transcript path is the answer" || fail "path (code=$CODE out=$OUT)"

# 3. A path with nothing at it yet is still the answer, and says so
run "/print" "{\"transcript_path\":\"$PROJ/not-yet.jsonl\"}"
[[ $CODE == 2 && "$OUT" == *"not-yet.jsonl"* && "$OUT" == *"not written yet"* ]] \
  && pass "an unwritten transcript is named and flagged" || fail "unwritten ($OUT)"

# 4. No transcript_path → the session-id glob, across project dirs
run "/print" "{\"session_id\":\"aaaaaaaa-1111-2222-3333-444444444444\"}"
[[ $CODE == 2 && "$OUT" == "$LIVE" ]] \
  && pass "the glob answers when the payload does not" || fail "glob ($OUT)"

# 5. ...and it never degrades to the newest file in the project dir. Another
#    session's transcript sitting right there must not be handed back as ours.
touch "$PROJ/bbbbbbbb-9999-9999-9999-999999999999.jsonl"
run "/print" "{\"session_id\":\"cccccccc-0000-0000-0000-000000000000\"}"
[[ $CODE == 2 && "$OUT" == *"no transcript on disk"* ]] \
  && pass "a session with no file is told so, not given a neighbour's" \
  || fail "mtime guess ($OUT)"

# 6. The same id under two project dirs is an anomaly, and reads as one
OTHER="$CFG/projects/-Users-someone-other"; mkdir -p "$OTHER"
touch "$OTHER/aaaaaaaa-1111-2222-3333-444444444444.jsonl"
run "/print" "{\"session_id\":\"aaaaaaaa-1111-2222-3333-444444444444\"}"
[[ $CODE == 2 && "$OUT" == *"should not happen"* && "$OUT" == *"$OTHER"* && "$OUT" == *"$PROJ"* ]] \
  && pass "two project dirs, one id — both printed and flagged" || fail "ambiguous ($OUT)"
rm -rf "$OTHER"

# 7. Neither field is a harness failure, and fails by name
run "/print" "{}"
[[ $CODE == 2 && "$OUT" == *"no transcript path and no session id"* ]] \
  && pass "an empty payload fails by name" || fail "empty payload ($OUT)"

# 8. Trailing words are ignored, not refused — the answer has no parameters
run "/print please" "{\"transcript_path\":\"$LIVE\"}"
[[ $CODE == 2 && "$OUT" == "$LIVE" ]] \
  && pass "trailing words are ignored" || fail "args ($OUT)"

# 9. All three spellings are one command — raw, namespaced, and the stub's
for spelling in "/jstack:print" '$jstack:print' "JSTACK_PRINT_CMD"; do
  run "$spelling" "{\"transcript_path\":\"$LIVE\"}"
  [[ $CODE == 2 && "$OUT" == "$LIVE" ]] \
    && pass "$spelling is the same command" || fail "$spelling ($OUT)"
done

# 10. Malformed stdin is the harness's problem, not the user's — stay out of it
OUT=$(printf 'not json' | "$HOOK" 2>&1); CODE=$?
[[ $CODE == 0 && -z "$OUT" ]] \
  && pass "unparseable payload passes through" || fail "bad stdin (code=$CODE out=$OUT)"

echo ""
if [[ $fails -gt 0 ]]; then
  echo "$fails check(s) failed" >&2
  exit 1
fi
echo "ALL PASS — /print answers from the payload without a turn"
