#!/usr/bin/env bash
# jStack live test — hooks/stop-inbox-guard.py, "a task handed to THIS session
# gets its answer".
#
# Hermetic: temp timeline dir, temp agent tree, temp review state. Verifies:
#   - a session holding an unanswered task IS blocked, once, and stops clean
#     as soon as it replies
#   - THE anti-ambush rule: updates never block, and a task belonging to
#     another session is invisible — opening a chat cannot drag in whatever
#     was lying around
#   - a spawned run is bound by the [inbox:N] in its own prompt, and to
#     nothing else
#   - stop_hook_active / non-agent cwd / kill switches → allow, silently
#   - the creator leg: an injected GitHub comment blocks the session that
#     created the issue, names the issue as the answer path (never msg reply),
#     and is consumed by being shown
#
# Exit 0 = all pass, exit 1 = any fail.

set -u
unset CODEX_THREAD_ID

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$PLUGIN_ROOT/hooks/stop-inbox-guard.py"
MSG="$PLUGIN_ROOT/bin/msg"

TMP=$(mktemp -d /tmp/jstack-inbox-guard-test.XXXXXX)
trap 'rm -rf "$TMP"' EXIT
export JSTACK_TIMELINE_DIR="$TMP/timeline"
export JSTACK_REVIEW_STATE="$TMP/state"
# A session is resumable only if it has a transcript, so the test owns a HOME
# and places them itself rather than reading (or writing) the real one.
export HOME="$TMP/home"; mkdir -p "$HOME"
export SCHEDULER_HOME="$TMP/sched"
transcript() {   # $1 = session id, $2 = the cwd it started in
  local d="$HOME/.claude/projects/proj-$1"; mkdir -p "$d"
  printf '{"cwd":"%s","type":"user"}\n' "$2" > "$d/$1.jsonl"
}

fails=0
fail() { echo "FAIL: $1" >&2; fails=$((fails+1)); }
pass() { echo "ok: $1"; }

[[ -x "$HOOK" ]] || { echo "FAIL: $HOOK not executable" >&2; exit 1; }

AR="$TMP/agents"
for seat in Alice Alice/chat Bob Bob/chat; do
  mkdir -p "$AR/$seat"; echo "# $seat" > "$AR/$seat/CLAUDE.md"
done
export JSTACK_REVIEW_CONFIG="$TMP/review.json"
echo "{\"agent_root\": \"$AR\"}" > "$JSTACK_REVIEW_CONFIG"

# transcripts: a typed user session, an unrelated headless run, a wake
printf '%s\n' '{"type":"user","promptSource":"typed","message":{"content":"hi"}}' > "$TMP/user.jsonl"
printf '%s\n' '{"type":"user","message":{"content":"/social_compose account=x"}}' > "$TMP/cron.jsonl"

# a stub interpreter so the task's wake books hermetically — a task always
# gets its own spawned session, never a running one
cat > "$TMP/fakepy" <<EOS
#!/bin/sh
exit 0
EOS
chmod +x "$TMP/fakepy"
cat > "$JSTACK_REVIEW_CONFIG" <<EOF2
{"agent_root": "$AR", "mail": {"python": "$TMP/fakepy", "scheduler_home": "$TMP"}}
EOF2

cd "$AR/Alice/chat" || exit 1
"$MSG" send @bob "First item" --wake >/dev/null
MID=$(python3 -c "import sqlite3;print(sqlite3.connect('$TMP/timeline/timeline.db').execute(\"SELECT MAX(id) FROM messages\").fetchone()[0])")
printf '%s\n' "{\"type\":\"user\",\"message\":{\"content\":\"[inbox:$MID] Message from alice/chat — handle it\"}}" > "$TMP/wake.jsonl"

# $1 = session id, $2 = transcript, $3 = cwd, $4... = extra json fields
run() {
  local sid="$1" tr="$2" cwd="$3" extra="${4:-}"
  echo "{\"session_id\":\"$sid\",\"transcript_path\":\"$tr\",\"cwd\":\"$cwd\"$extra}" | "$HOOK"
}
decision() { python3 -c "import json,sys; raw=sys.stdin.read().strip(); print(json.loads(raw)['decision'] if raw else 'allow')"; }


BOB="$AR/Bob/chat"

# 1. the run woken FOR the task is blocked, once. A task arrives one way — a
#    wake carrying [inbox:N] in its own first prompt — so that marker, not a
#    session id handed out by a delivery rung, is what holds a run to its work.
out=$(run woken-run "$TMP/wake.jsonl" "$BOB")
[[ "$(printf '%s' "$out" | decision)" == "block" ]] && pass "the run woken for a task is blocked" || fail "the run woken for a task is blocked"
[[ "$out" == *"First item"* ]] && pass "block names the task" || fail "block names the task"
[[ "$out" == *"msg reply"* ]] && pass "block carries the reply command" || fail "block carries the reply command"
[[ "$out" == *"waiting"* ]] && pass "block says the sender is waiting" || fail "block says the sender is waiting"
[[ -z "$(run woken-run "$TMP/wake.jsonl" "$BOB")" ]] && pass "same session not blocked twice" || fail "same session not blocked twice"

# 2. THE anti-ambush rule — nobody else can be dragged into it
[[ -z "$(run some-other-session "$TMP/user.jsonl" "$BOB")" ]] \
  && pass "another user session is never ambushed" || fail "another user session is never ambushed"
[[ -z "$(run cron-worker "$TMP/cron.jsonl" "$BOB")" ]] \
  && pass "an unrelated headless run is never ambushed" || fail "an unrelated headless run is never ambushed"

# 3. updates never block anyone, ever
"$MSG" send @bob "Just a note" >/dev/null
"$MSG" send @bob "Another note" >/dev/null
[[ -z "$(run fresh-session "$TMP/user.jsonl" "$BOB")" ]] \
  && pass "updates never block a session" || fail "updates never block a session"

# 4. and only the id in its own prompt — a marker naming no live task is inert
printf '%s\n' '{"type":"user","message":{"content":"[inbox:99999] nothing"}}' > "$TMP/badwake.jsonl"
[[ -z "$(run bad-wake "$TMP/badwake.jsonl" "$BOB")" ]] \
  && pass "a marker naming no live task allows" || fail "a marker naming no live task allows"

# 5. answering releases the session
JSTACK_MAIL_FROM=bob/chat "$MSG" reply "$MID" "Done, here it is." >/dev/null
[[ -z "$(run plain-session-2 "$TMP/user.jsonl" "$BOB")" ]] \
  && pass "an answered task blocks nobody" || fail "an answered task blocks nobody"
[[ -z "$(run woken-run-2 "$TMP/wake.jsonl" "$BOB")" ]] \
  && pass "an answered task releases its wake too" || fail "an answered task releases its wake too"

# 6. loop guards and switches. Each runs against a transcript that provably
#    DOES block without it (the control below), so a switch that quietly
#    stopped working could not pass this section by allowing everything.
"$MSG" send @bob "Second task" --wake >/dev/null
MID2=$(python3 -c "import sqlite3;print(sqlite3.connect('$TMP/timeline/timeline.db').execute(\"SELECT MAX(id) FROM messages\").fetchone()[0])")
printf '%s\n' "{\"type\":\"user\",\"message\":{\"content\":\"[inbox:$MID2] Message from alice/chat — handle it\"}}" > "$TMP/wake2.jsonl"
[[ "$(run w6-control "$TMP/wake2.jsonl" "$BOB" | decision)" == "block" ]] \
  && pass "control: this transcript does block" || fail "control: this transcript does block"
[[ -z "$(run w6a "$TMP/wake2.jsonl" "$BOB" ',"stop_hook_active":true')" ]] \
  && pass "stop_hook_active never blocks" || fail "stop_hook_active never blocks"
[[ -z "$(run s6 "$TMP/wake2.jsonl" "/tmp")" ]] && pass "non-agent cwd allowed" || fail "non-agent cwd allowed"
[[ -z "$(JSTACK_INBOX_GUARD_DISABLED=1 run w6b "$TMP/wake2.jsonl" "$BOB")" ]] && pass "kill switch honored" || fail "kill switch honored"
[[ -z "$(SKIP_SESSION_HOOK=1 run w6c "$TMP/wake2.jsonl" "$BOB")" ]] && pass "SKIP_SESSION_HOOK honored" || fail "SKIP_SESSION_HOOK honored"
[[ -z "$(echo 'not json' | "$HOOK")" ]] && pass "malformed input allowed" || fail "malformed input allowed"

# 7. THE RETURN LEG — the guard serves both ends of a channel. It makes a
#    receiver answer; it also hands a sender what came back, because the
#    session that asked is the only one the answer means anything to. A stop is
#    the one moment a running session can be given something without touching
#    what a person is part-way through typing.
A1=aaaa1111-aaaa-1111-aaaa-111111111111
B1=bbbb2222-bbbb-2222-bbbb-222222222222
ALICE="$AR/Alice/chat"
transcript "$A1" "$ALICE"
cd "$ALICE" || exit 1
CLAUDE_CODE_SESSION_ID="$A1" "$MSG" send @bob "Need the device log" --wake >/dev/null
TID=$(python3 -c "import sqlite3;print(sqlite3.connect('$TMP/timeline/timeline.db').execute(\"SELECT MAX(id) FROM messages\").fetchone()[0])")
CLAUDE_CODE_SESSION_ID="$B1" JSTACK_MAIL_FROM=bob/chat \
  "$MSG" reply "$TID" "Here it is: the crash is OOM" >/dev/null

# nobody else in that seat is handed it — an answer is not seat news
[[ -z "$(run a-different-alice-session "$TMP/user.jsonl" "$ALICE")" ]] \
  && pass "another session in the seat is not handed the answer" \
  || fail "another session in the seat is not handed the answer"

out=$(run "$A1" "$TMP/user.jsonl" "$ALICE")
[[ "$(printf '%s' "$out" | decision)" == "block" ]] \
  && pass "the session that asked is handed its answer" || fail "the session that asked is handed its answer"
[[ "$out" == *"the crash is OOM"* ]] && pass "the block names the answer" || fail "the block names the answer"
[[ "$out" == *"answer"* && "$out" == *"asked for"* ]] \
  && pass "the block reads as an answer, not a demand" || fail "the block reads as an answer, not a demand"
[[ "$out" != *"no answer yet"* ]] \
  && pass "and does not tell the asker to answer itself" || fail "and does not tell the asker to answer itself"

# being shown IS its lifecycle — nothing is owed back, so it is consumed here
"$MSG" pending-for alice/chat --session "$A1" >/dev/null; [[ $? -eq 1 ]] \
  && pass "an answer is consumed by being shown" || fail "an answer is consumed by being shown"

# 8. THE CREATOR LEG — a GitHub comment on an issue this session created is
#    handed to it the same way, but the answer path it is given is the issue
#    itself: `msg reply` aims at a seat, and github is not one.
A2=aaaa3333-aaaa-3333-aaaa-333333333333
transcript "$A2" "$ALICE"
"$MSG" inject "$A2" "Tests are red on CI, need a call.
SECOND-LINE-SENTINEL and a great deal more prose besides." \
  --issue "Acme/widgets#12" --author acme-agent >/dev/null
out=$(run "$A2" "$TMP/user.jsonl" "$ALICE")
[[ "$(printf '%s' "$out" | decision)" == "block" ]] \
  && pass "the creator session is handed the comment" || fail "the creator session is handed the comment"

# 8b. THE BLOCK IS A NOTIFICATION, NOT THE MESSAGE. A Stop block is rendered
#     into the terminal a person is reading; a body pasted there is the mail
#     system shouting over them. The row names the message and hands over the
#     command that fetches it — and nothing else.
[[ "$out" != *"SECOND-LINE-SENTINEL"* ]] \
  && pass "the body is NOT in the block" || fail "the body is NOT in the block"
[[ "$out" == *"msg read"* ]] \
  && pass "the block hands over the read command instead" \
  || fail "the block hands over the read command instead"
[[ "$out" == *"gh issue comment"* ]] \
  && pass "the block's answer path is the issue" || fail "the block's answer path is the issue"
[[ "$out" == *"Acme/widgets#12"* ]] \
  && pass "the block names the issue" || fail "the block names the issue"
"$MSG" pending-for alice/chat --session "$A2" >/dev/null; [[ $? -eq 1 ]] \
  && pass "an injected comment is consumed by being shown" \
  || fail "an injected comment is consumed by being shown"

echo
if [[ $fails -eq 0 ]]; then echo "ALL PASS"; exit 0; else echo "$fails FAILED"; exit 1; fi
