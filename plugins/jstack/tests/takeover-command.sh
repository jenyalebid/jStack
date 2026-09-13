#!/usr/bin/env bash
# jStack live test — hooks/takeover-command.py (`/takeover` spawns, no turn).
#
# Pipes fixture UserPromptSubmit JSON through the real hook against a temp agent
# tree, with a faked terminal adapter — opening a window is the one step a test
# cannot assert on a shared desktop. Exit 2 IS the feature: an answer on exit 0
# has spent the turn the hook exists to save.
#
# What a takeover has to get right, and what breaks if it does not:
#
#   - the briefing carries a POINTER and a mandate, never a summary. A summary
#     is what handoff does; writing one here would put the outgoing session's
#     account of itself back into the payload built to exclude it.
#   - the user-spine recipe selects queue-operation/enqueue as well as user.
#     A message typed mid-turn is not filed as a `user` record, and those are
#     the corrections — miss them and the transcript reads as though the user
#     never pushed back.
#   - --first-prompt is what makes the spawn a task. Without it the window
#     opens and waits, which is a staged context, not a takeover — so an
#     adapter that cannot take the flag must be DETECTED, and the degradation
#     said out loud rather than passed off as a spawn.
#   - the briefing is staged outside every workspace: it is a one-shot payload,
#     not a file somebody commits.
#   - an unknown @agent lists the agents and spawns nothing. Joining
#     {root}/{name} blind fails later and further away than the typo.
#
# Exit 0 = all pass, exit 1 = any fail.

set -u

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$PLUGIN_ROOT/hooks/takeover-command.py"
PY="${JSTACK_PYTHON:-python3}"
# The hook imports root.py for @agent resolution; without the pin a worktree
# run would be answering about the main checkout's copy of it.
. "$PLUGIN_ROOT/tests/lib/pin-plugin-root.sh"

[[ -x "$HOOK" ]] || { echo "FAIL: $HOOK not executable" >&2; exit 1; }

TMP=$(cd "$(mktemp -d /tmp/jstack-takeover-cmd-test.XXXXXX)" && pwd -P)
trap 'rm -rf "$TMP"' EXIT

AGENTS="$TMP/agents"
mkdir -p "$AGENTS/Alpha/chat" "$AGENTS/Alpha/meta" "$AGENTS/Bravo"
for d in "$AGENTS/Alpha" "$AGENTS/Alpha/chat" "$AGENTS/Alpha/meta" "$AGENTS/Bravo"; do
  echo "# agent" > "$d/CLAUDE.md"
done
export JSTACK_AGENTS_DIR="$AGENTS"

PROJ="$TMP/projects"; mkdir -p "$PROJ"
BIN="$TMP/bin"; mkdir -p "$BIN"
SEAT="$AGENTS/Alpha/chat"

SID="aaaaaaaa-1111-2222-3333-444444444444"
SRC="$PROJ/$SID.jsonl"
printf '%s\n' '{"type":"user","sessionId":"'"$SID"'"}' > "$SRC"

fails=0
pass() { echo "ok: $1"; }
fail() { echo "FAIL: $1" >&2; fails=$((fails+1)); }

# The terminal adapter, faked: records its argv one-per-line (--name and
# --first-prompt hold spaces, so a flattened "$*" could not be asserted on) and
# answers the capability probe the way the argument says. Never the real one —
# a test that opens windows on a shared desktop is a test nobody runs twice.
terminal() {   # terminal <exit-code> <takes-first-prompt: 0|1>
  local usage="usage: open-terminal-here <cwd> [--prompt-file <p>] [--name <t>]"
  [[ "$2" == 1 ]] && usage="$usage [--first-prompt <text>]"
  {
    echo '#!/bin/bash'
    echo 'if [ "$#" -eq 0 ]; then'
    echo "  echo \"$usage\""
    echo '  exit 64'
    echo 'fi'
    echo "printf '%s\\n' \"\$@\" > \"$TMP/opened\""
    echo "exit $1"
  } > "$BIN/open-terminal-here"
  chmod +x "$BIN/open-terminal-here"
}
terminal 0 1
export JSTACK_TERMINAL_BIN="$BIN/open-terminal-here"

run() {   # run <prompt> [transcript-path] [cwd]
  rm -f "$TMP/opened"
  local prompt="$1" tr="${2-$SRC}" cwd="${3-$SEAT}"
  OUT=$($PY -c '
import json, sys
print(json.dumps({"prompt": sys.argv[1], "transcript_path": sys.argv[2],
                  "cwd": sys.argv[3], "session_id": sys.argv[4]}))' \
        "$prompt" "$tr" "$cwd" "$SID" \
        | PATH="$BIN:/usr/bin:/bin" "$HOOK" 2>&1 >/dev/null)
  CODE=$?
}

# The value the fake adapter recorded for <flag>, or "" when absent.
opt() {   # opt <flag>
  [[ -f "$TMP/opened" ]] || return 0
  awk -v f="$1" 'p{print; exit} $0==f{p=1}' "$TMP/opened"
}
opened_cwd() { [[ -f "$TMP/opened" ]] && head -1 "$TMP/opened"; }
has_opt() { [[ -f "$TMP/opened" ]] && grep -Fxq -- "$1" "$TMP/opened"; }

# 1. Anything that is not /takeover is none of this hook's business
run "take over the deploy from Sam"
[[ $CODE == 0 && -z "$OUT" && ! -f "$TMP/opened" ]] \
  && pass "an ordinary prompt passes through" || fail "passthrough (code=$CODE out=$OUT)"

# 2. A bare /takeover spawns on this workspace, as a TASK
run "/takeover"
[[ $CODE == 2 && "$(opened_cwd)" == "$SEAT" ]] \
  && pass "the spawn lands on the session's own workspace" || fail "cwd ($(opened_cwd)) out=$OUT"
BRIEF="$(opt --prompt-file)"
[[ -n "$BRIEF" && -f "$BRIEF" ]] \
  && pass "a briefing is staged and handed over" || fail "no briefing ($BRIEF)"
[[ "$(opt --name)" == "TO · alpha/chat" ]] \
  && pass "titled from the source seat when no focus is given" || fail "title ($(opt --name))"
KICK="$(opt --first-prompt)"
[[ -n "$KICK" && "$KICK" == *"verify what it claims"* ]] \
  && pass "--first-prompt makes it a task, not a staged context" || fail "kick ($KICK)"

# 3. The briefing is a pointer and a mandate. Not a summary — that is the
#    whole reason this command is not handoff.
grep -q "$SRC" "$BRIEF" \
  && pass "the briefing names the transcript to read" || fail "no transcript path in brief"
grep -q "$SID" "$BRIEF" \
  && pass "the briefing names the source session" || fail "no session id in brief"
grep -q "handed no summary" "$BRIEF" \
  && pass "the briefing says outright that no summary was given" || fail "summary disclaimer missing"
grep -q "inherits state, never conclusions" "$BRIEF" \
  && pass "the mandate is verify-before-build" || fail "mandate missing"

# 4. The extraction recipes are the part a rewrite silently breaks
grep -q 'queue-operation' "$BRIEF" \
  && pass "the user spine includes mid-turn interjections" \
  || fail "queue-operation missing — the corrections would be invisible"
grep -q 'sort | uniq -c' "$BRIEF" \
  && pass "shape before extract, so the window survives the read" || fail "no shape probe"

# 5. Staged outside every workspace — a one-shot payload nobody commits
[[ "$BRIEF" != "$AGENTS"/* && "$BRIEF" != "$SEAT"/* ]] \
  && pass "the briefing is staged outside the agent tree" || fail "staged in a workspace ($BRIEF)"

# 6. A focus scopes the work and names the window
run "/takeover cellular not working"
[[ $CODE == 2 && "$(opt --name)" == "TO · cellular not working" ]] \
  && pass "the focus names the window" || fail "focus title ($(opt --name))"
BRIEF="$(opt --prompt-file)"
grep -q "Your scope is: \*\*cellular not working\*\*" "$BRIEF" \
  && pass "the focus is stated as an explicit narrowing" || fail "focus not scoped in brief"
[[ "$(opt --first-prompt)" == *"focus: cellular not working."* ]] \
  && pass "the kick carries the focus" || fail "kick focus ($(opt --first-prompt))"

# 7. @agent retargets the workspace — chat/ when it exists, the root when not
run "/takeover @alpha the reply engine"
[[ $CODE == 2 && "$(opened_cwd)" == "$AGENTS/Alpha/chat" ]] \
  && pass "@agent lands in the agent's chat seat" || fail "@alpha ($(opened_cwd))"
[[ "$(opt --name)" == "TO→Alpha · the reply engine" ]] \
  && pass "a retargeted takeover is titled for its agent" || fail "agent title ($(opt --name))"
run "/takeover @bravo"
[[ $CODE == 2 && "$(opened_cwd)" == "$AGENTS/Bravo" ]] \
  && pass "an agent with no chat/ lands at its root" || fail "@bravo ($(opened_cwd))"
run "/takeover @ALPHA-meta look again"
[[ $CODE == 2 && "$(opened_cwd)" == "$AGENTS/Alpha/meta" ]] \
  && pass "@agent-seat names a seat, case-blind" || fail "@alpha-meta ($(opened_cwd))"

# The slash was this command's own spelling for one release and no other
# command's. It must name the mistake, not report the token as a bad agent.
run "/takeover @alpha/meta look again"
[[ $CODE == 2 && "$OUT" == *"hyphen, not a slash"* && "$OUT" == *"@alpha-meta"* && ! -f "$TMP/opened" ]] \
  && pass "a slashed seat is corrected to the hyphen form" || fail "slash form ($OUT)"

# 8. A name that resolves to nothing says so — it never joins a path blind
run "/takeover @nobody fix it"
[[ $CODE == 2 && "$OUT" == *"unknown agent"* && "$OUT" == *"alpha"* && ! -f "$TMP/opened" ]] \
  && pass "an unknown agent lists the agents and spawns nothing" || fail "unknown agent ($OUT)"
run "/takeover @alpha-nosuch"
[[ $CODE == 2 && "$OUT" == *"no seat"* && ! -f "$TMP/opened" ]] \
  && pass "an unknown seat names the seats and spawns nothing" || fail "unknown seat ($OUT)"

# 9. Nothing to read refuses before spawning — a takeover with no source is
#    a fresh session wearing a takeover's title
run "/takeover" ""
[[ $CODE == 2 && "$OUT" == *"no transcript path"* && ! -f "$TMP/opened" ]] \
  && pass "no transcript path refuses, and spawns nothing" || fail "pathless ($OUT)"
run "/takeover" "$PROJ/gone.jsonl"
[[ $CODE == 2 && "$OUT" == *"no transcript on disk"* && ! -f "$TMP/opened" ]] \
  && pass "a missing transcript refuses, and spawns nothing" || fail "missing ($OUT)"

# 10. An adapter that cannot take --first-prompt is detected, not guessed at.
#     Passing the flag to one that does not parse it reaches `claude`, which
#     dies on an unknown option — the window closes before anyone reads it.
terminal 0 0
run "/takeover older adapter"
[[ $CODE == 2 ]] && ! has_opt "--first-prompt" \
  && pass "the flag is withheld from an adapter that cannot take it" \
  || fail "flag forced on an old adapter"
[[ "$OUT" == *"did NOT start"* && "$OUT" == *"$(opt --prompt-file)"* ]] \
  && pass "the degradation is said out loud, with the briefing path" || fail "silent degrade ($OUT)"
terminal 0 1

# 11. A window that will not open is a takeover that did not happen — say so,
#     and hand back everything needed to do it by hand
terminal 69 1
run "/takeover"
[[ $CODE == 2 && "$OUT" == *"would not open"* && "$OUT" == *"nothing was spawned"* \
   && "$OUT" == *"--append-system-prompt"* ]] \
  && pass "a failed window reports a failure, not a spawn" || fail "windowless ($OUT)"
terminal 0 1

# 12. All three spellings are one command — raw, namespaced, and the stub's
for spelling in "/jstack:takeover" "JSTACK_TAKEOVER_CMD"; do
  run "$spelling"
  [[ $CODE == 2 && -f "$TMP/opened" ]] \
    && pass "$spelling is the same command" || fail "$spelling (code=$CODE out=$OUT)"
done

# 13. Malformed stdin is the harness's problem, not the user's — stay out of it
OUT=$(printf 'not json' | "$HOOK" 2>&1); CODE=$?
[[ $CODE == 0 && -z "$OUT" ]] \
  && pass "unparseable payload passes through" || fail "bad stdin (code=$CODE out=$OUT)"

echo ""
if [[ $fails -gt 0 ]]; then
  echo "$fails check(s) failed" >&2
  exit 1
fi
echo "ALL PASS — /takeover spawns a session that reads the source itself"
