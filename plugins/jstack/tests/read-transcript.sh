#!/usr/bin/env bash
# jStack live test — hooks/pretooluse-read-transcript.py (a transcript reads as dialogue).
#
# Pipes fixture PreToolUse JSON through the real hook against a temp tree. The deny
# IS the feature: a Read that is allowed through has pulled a machine log into a
# context window, which is the exact failure this hook exists to make impossible.
#
#   - a Read of an ordinary file passes through untouched (exit 0, silent)
#   - a non-Read tool is none of this hook's business
#   - a Claude session file is denied, and the denial carries the speech
#   - tool calls, tool results, thinking and injected wrappers are all absent
#   - a system-reminder wrapped INSIDE a real prompt is stripped, and the words
#     around it survive — the turn is speech even when the harness wrote into it
#   - a Codex rollout is recognized by name and by session_meta, and its
#     input_text/output_text shape renders the same way
#   - a .jsonl that is not a transcript at all is left alone
#   - an unreadable/garbage file never blocks the read
#
# Exit 0 = all pass, exit 1 = any fail.

set -u
unset CODEX_THREAD_ID

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$PLUGIN_ROOT/hooks/pretooluse-read-transcript.py"

[[ -x "$HOOK" ]] || { echo "FAIL: $HOOK not executable" >&2; exit 1; }

TMP=$(cd "$(mktemp -d /tmp/jstack-read-transcript-test.XXXXXX)" && pwd -P)
trap 'rm -rf "$TMP"' EXIT

PROJ="$TMP/.claude/projects/-Users-someone-seat"
mkdir -p "$PROJ"
SID="aaaaaaaa-1111-2222-3333-444444444444"
CLAUDE_FILE="$PROJ/$SID.jsonl"

fails=0
pass() { echo "ok: $1"; }
fail() { echo "FAIL: $1" >&2; fails=$((fails+1)); }

# A Claude session holding one of everything: speech, machinery, and a prompt
# the harness wrote into.
python3 - "$CLAUDE_FILE" <<'PY'
import json, sys
rows = [
  {"type": "user", "timestamp": "2026-09-22T10:00:00Z",
   "message": {"content": "<system-reminder>SECRET RULES</system-reminder>what broke?"}},
  {"type": "assistant", "timestamp": "2026-09-22T10:00:01Z",
   "message": {"content": [
      {"type": "thinking", "thinking": "THINKING NOISE"},
      {"type": "text", "text": "the daemon died."},
      {"type": "tool_use", "name": "Bash", "input": {"command": "TOOL NOISE"}}]}},
  {"type": "user", "timestamp": "2026-09-22T10:00:02Z", "toolUseResult": {"stdout": "RESULT NOISE"},
   "message": {"content": [{"type": "tool_result", "content": "RESULT NOISE"}]}},
  {"type": "user", "isMeta": True, "message": {"content": "META NOISE"}},
  {"type": "user", "isSidechain": True, "message": {"content": "SIDECHAIN NOISE"}},
  {"type": "file-history-snapshot", "snapshot": {"x": "SNAPSHOT NOISE"}},
]
with open(sys.argv[1], "w") as fh:
    for r in rows:
        fh.write(json.dumps(r) + "\n")
PY

run() {   # run <tool_name> <file_path>
  OUT=$(python3 -c '
import json, sys
print(json.dumps({"tool_name": sys.argv[1], "tool_input": {"file_path": sys.argv[2]}}))' \
    "$1" "$2" | "$HOOK" 2>/dev/null)
  CODE=$?
}

decision() { python3 -c '
import json,sys
try: print(json.loads(sys.stdin.read())["hookSpecificOutput"]["permissionDecision"])
except Exception: print("none")' <<<"$OUT"; }

reason() { python3 -c '
import json,sys
try: print(json.loads(sys.stdin.read())["hookSpecificOutput"]["permissionDecisionReason"])
except Exception: print("")' <<<"$OUT"; }

# 1. An ordinary file is none of this hook's business
echo "hello" > "$TMP/notes.md"
run Read "$TMP/notes.md"
[[ $CODE == 0 && -z "$OUT" ]] \
  && pass "an ordinary file passes through" || fail "passthrough (code=$CODE out=$OUT)"

# 2. Neither is another tool
run Bash "$CLAUDE_FILE"
[[ $CODE == 0 && -z "$OUT" ]] \
  && pass "a non-Read tool passes through" || fail "non-Read (code=$CODE out=$OUT)"

# 3. A Claude transcript is denied, and the denial carries the speech
run Read "$CLAUDE_FILE"
R=$(reason)
[[ $CODE == 0 && "$(decision)" == "deny" ]] \
  && pass "a Claude transcript is denied" || fail "claude deny (code=$CODE dec=$(decision))"
[[ "$R" == *"what broke?"* && "$R" == *"the daemon died."* ]] \
  && pass "both sides' speech is present" || fail "speech missing"

# 4. Nothing machine-written survives — the whole point
noise=0
for n in "SECRET RULES" "THINKING NOISE" "TOOL NOISE" "RESULT NOISE" "META NOISE" \
         "SIDECHAIN NOISE" "SNAPSHOT NOISE"; do
  [[ "$R" == *"$n"* ]] && { fail "leaked: $n"; noise=1; }
done
[[ $noise == 0 ]] && pass "no tool call, result, thinking, meta or injection leaks"

# 5. The words around a stripped wrapper survive. A prompt is speech even when
#    the harness has written into the middle of it.
[[ "$R" == *"what broke?"* ]] \
  && pass "a wrapper is stripped without taking the prompt with it" || fail "prompt eaten by strip"

# 6. A Codex rollout — different record shape, same rendering. Named rollout-*
#    and carrying session_meta, so both recognition paths are exercised.
CODEX="$TMP/rollout-2026-09-22T10-00-00-$SID.jsonl"
python3 - "$CODEX" <<'PY'
import json, sys
rows = [
  {"type": "session_meta", "payload": {"id": "abc"}},
  {"type": "response_item", "timestamp": "2026-09-22T11:00:00Z",
   "payload": {"type": "message", "role": "user",
               "content": [{"type": "input_text", "text": "codex question"}]}},
  {"type": "response_item", "timestamp": "2026-09-22T11:00:01Z",
   "payload": {"type": "message", "role": "assistant",
               "content": [{"type": "output_text", "text": "codex answer"}]}},
  {"type": "response_item", "timestamp": "2026-09-22T11:00:02Z",
   "payload": {"type": "function_call", "name": "shell", "arguments": "CODEX TOOL NOISE"}},
  {"type": "response_item", "timestamp": "2026-09-22T11:00:03Z",
   "payload": {"type": "message", "role": "user",
               "content": [{"type": "input_text", "text": "<environment_context>ENV NOISE</environment_context>"}]}},
]
with open(sys.argv[1], "w") as fh:
    for r in rows:
        fh.write(json.dumps(r) + "\n")
PY
run Read "$CODEX"
R=$(reason)
[[ "$(decision)" == "deny" && "$R" == *"codex question"* && "$R" == *"codex answer"* ]] \
  && pass "a Codex rollout renders the same way" || fail "codex (dec=$(decision))"
[[ "$R" != *"CODEX TOOL NOISE"* && "$R" != *"ENV NOISE"* ]] \
  && pass "Codex tool calls and environment context are absent" || fail "codex noise leaked"

# 7. A .jsonl that is not a transcript is left alone. This hook owns session
#    files, not every line-delimited file on the machine.
printf '{"event":"deploy","ok":true}\n' > "$TMP/events.jsonl"
run Read "$TMP/events.jsonl"
[[ $CODE == 0 && -z "$OUT" ]] \
  && pass "an unrelated .jsonl passes through" || fail "unrelated jsonl (out=$OUT)"

# 8. Garbage never blocks a read. Failing open costs context; failing closed
#    costs the user a file they asked for.
printf 'not json at all\n' > "$PROJ/bbbbbbbb-1111-2222-3333-444444444444.jsonl"
run Read "$PROJ/bbbbbbbb-1111-2222-3333-444444444444.jsonl"
[[ $CODE == 0 && -z "$OUT" ]] \
  && pass "an unparseable transcript falls through to the real read" || fail "garbage (out=$OUT)"

[[ $fails == 0 ]] && echo "all pass" || echo "$fails failed" >&2
exit $(( fails > 0 ))
