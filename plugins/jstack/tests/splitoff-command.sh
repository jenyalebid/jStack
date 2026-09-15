#!/usr/bin/env bash
# jStack live test — hooks/splitoff-command.py (`/splitoff` forks, no turn).
#
# Pipes fixture UserPromptSubmit JSON through the real hook with the REAL dubber
# against a temp project tree, and a faked terminal adapter — opening a window
# is the one step a test cannot assert on a shared desktop. Exit 2 IS the
# feature: a branch that answers on exit 0 has spent the turn it exists to save.
#
#   - a prompt that is not /splitoff passes through untouched (exit 0, silent)
#   - the dub is a real copy under a NEW id, and the source is byte-identical
#     afterwards — a fork that mutates the original is not a fork
#   - every sessionId inside the copy is rewritten, so the copy is consistent
#   - the terminal is opened on the payload's cwd with --resume, never
#     --session-id (which forces a fresh empty session and errors)
#   - words become the copy's picker title, never a scope narrowing
#   - a transcript the payload does not name, or that is not on disk, refuses
#     BEFORE dubbing — no half-fork
#   - a terminal that will not open still reports the id and the resume line:
#     the fork exists whether or not a window does
#   - /jstack:splitoff and the JSTACK_SPLITOFF_CMD stub expansion are one command
#
# Exit 0 = all pass, exit 1 = any fail.

set -u
unset CODEX_THREAD_ID

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$PLUGIN_ROOT/hooks/splitoff-command.py"
DUB="$PLUGIN_ROOT/bin/dub-session"

[[ -x "$HOOK" ]] || { echo "FAIL: $HOOK not executable" >&2; exit 1; }
[[ -x "$DUB"  ]] || { echo "FAIL: $DUB not executable" >&2; exit 1; }

TMP=$(cd "$(mktemp -d /tmp/jstack-splitoff-cmd-test.XXXXXX)" && pwd -P)
trap 'rm -rf "$TMP"' EXIT

PROJ="$TMP/projects/-Users-someone-seat"; mkdir -p "$PROJ"
SEAT="$TMP/seat"; mkdir -p "$SEAT"
BIN="$TMP/bin"; mkdir -p "$BIN"

SRC_ID="aaaaaaaa-1111-2222-3333-444444444444"
SRC="$PROJ/$SRC_ID.jsonl"

fails=0
pass() { echo "ok: $1"; }
fail() { echo "FAIL: $1" >&2; fails=$((fails+1)); }

# A fixture transcript: two message lines and a title, which is every field the
# dub rewrites.
fixture() {
  python3 - "$SRC" "$SRC_ID" <<'PY'
import json, sys
path, sid = sys.argv[1], sys.argv[2]
with open(path, "w") as fh:
    for line in ({"type": "user", "sessionId": sid, "text": "hello"},
                 {"type": "ai-title", "sessionId": sid, "aiTitle": "the original"},
                 {"type": "assistant", "sessionId": sid, "text": "hi"}):
        fh.write(json.dumps(line) + "\n")
PY
}
fixture
SRC_SUM_BEFORE="$(shasum "$SRC" | cut -d' ' -f1)"

# The terminal adapter, faked: records its argv. Never the real one — a test
# that opens windows on a shared desktop is a test nobody runs twice.
terminal() {   # terminal <exit-code>
  printf '%s\n' '#!/bin/bash' "echo \"\$*\" >> \"$TMP/opened\"" "exit $1" \
    > "$BIN/open-terminal-here"
  chmod +x "$BIN/open-terminal-here"
}
terminal 0
export JSTACK_TERMINAL_BIN="$BIN/open-terminal-here"

run() {   # run <prompt> [transcript-path]
  local prompt="$1" tr="${2-$SRC}"
  OUT=$(python3 -c '
import json, sys
print(json.dumps({"prompt": sys.argv[1], "transcript_path": sys.argv[2],
                  "cwd": sys.argv[3], "session_id": "aaaaaaaa-1111-2222-3333-444444444444"}))' \
        "$prompt" "$tr" "$SEAT" \
        | PATH="$BIN:/usr/bin:/bin" "$HOOK" 2>&1 >/dev/null)
  CODE=$?
}

copies() { find "$PROJ" -name '*.jsonl' ! -name "$SRC_ID.jsonl"; }

# 1. Anything that is not /splitoff is none of this hook's business
run "split the config into two files"
[[ $CODE == 0 && -z "$OUT" ]] \
  && pass "an ordinary prompt passes through" || fail "passthrough (code=$CODE out=$OUT)"

# 2. A bare /splitoff dubs and opens
rm -f "$TMP/opened"
run "/splitoff"
COPY="$(copies)"
[[ $CODE == 2 && -f "$COPY" ]] \
  && pass "the fork lands beside the source" || fail "dub (code=$CODE out=$OUT)"
NEW_ID="$(basename "${COPY%.jsonl}")"
[[ "$NEW_ID" != "$SRC_ID" ]] \
  && pass "the copy carries a new id" || fail "id reused ($NEW_ID)"

# 3. The source is untouched — a fork that mutates the original is not a fork
[[ "$(shasum "$SRC" | cut -d' ' -f1)" == "$SRC_SUM_BEFORE" ]] \
  && pass "the source transcript is byte-identical" || fail "source mutated"

# 4. ...and the copy is self-consistent: no line still claims the old session
! grep -q "$SRC_ID" "$COPY" \
  && pass "every sessionId in the copy was rewritten" \
  || fail "stale ids in the copy ($(grep -c "$SRC_ID" "$COPY"))"

# 5. The window opens on the session's own workspace, with --resume
[[ "$(cat "$TMP/opened")" == "$SEAT --resume $NEW_ID" ]] \
  && pass "the terminal opens on the cwd with --resume" || fail "open ($(cat "$TMP/opened"))"
[[ "$(cat "$TMP/opened")" != *"--session-id"* ]] \
  && pass "never --session-id" || fail "--session-id would error on an existing file"
[[ "$OUT" == *"$NEW_ID"* ]] \
  && pass "the answer names the new id" || fail "answer ($OUT)"

# 6. Words name the copy, they do not narrow it. The whole transcript is copied
#    either way; only the picker title changes.
rm -f "$COPY" "$TMP/opened"
run "/splitoff try redis"
COPY="$(copies)"
[[ $CODE == 2 && "$(python3 -c '
import json,sys
print([json.loads(l)["aiTitle"] for l in open(sys.argv[1]) if json.loads(l).get("type")=="ai-title"][0])' "$COPY")" \
   == "the original - try redis" ]] \
  && pass "words become the copy title" || fail "title ($OUT)"
[[ "$(wc -l < "$COPY")" == "$(wc -l < "$SRC")" ]] \
  && pass "the copy is still the whole transcript" || fail "narrowed ($(wc -l < "$COPY"))"
[[ "$OUT" == *"try redis"* ]] \
  && pass "the answer names the copy" || fail "named answer ($OUT)"
rm -f "$COPY"

# 7. Nothing to fork refuses before dubbing — no half-fork left behind
run "/splitoff" ""
[[ $CODE == 2 && "$OUT" == *"no transcript path"* && -z "$(copies)" ]] \
  && pass "no transcript path refuses, and dubs nothing" || fail "pathless ($OUT)"
run "/splitoff" "$PROJ/gone.jsonl"
[[ $CODE == 2 && "$OUT" == *"no transcript on disk"* && -z "$(copies)" ]] \
  && pass "a missing transcript refuses, and dubs nothing" || fail "missing ($OUT)"

# 8. A terminal that will not open is a missing convenience, not a lost fork
terminal 69
rm -f "$TMP/opened"
run "/splitoff"
COPY="$(copies)"
[[ $CODE == 2 && -f "$COPY" && "$OUT" == *"claude --resume"* && "$OUT" == *"$SEAT"* ]] \
  && pass "no window still reports the fork and how to resume it" || fail "windowless ($OUT)"
rm -f "$COPY"
terminal 0

# 9. All three spellings are one command — raw, namespaced, and the stub's
for spelling in "/jstack:splitoff" '$jstack:splitoff' "JSTACK_SPLITOFF_CMD"; do
  run "$spelling"
  [[ $CODE == 2 && -n "$(copies)" ]] \
    && pass "$spelling is the same command" || fail "$spelling (code=$CODE out=$OUT)"
  rm -f $(copies)
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
echo "ALL PASS — /splitoff forks verbatim without a turn"
