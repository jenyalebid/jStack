#!/usr/bin/env bash
# Hermetic test for bin/task-create — stubbed file-issue and gh, a fake HOME
# with a fake transcript; no network, no real issue, no real assignment.
#
# What must never regress, in order of what it would cost:
#   - a task filed WITHOUT its creator marker when the session was resolvable.
#     The marker is the creator leg: without it every comment on the issue
#     stays on GitHub and the CLI that filed the task never hears back.
#   - a filed-but-unassigned task read as success. Assignment IS the spawn;
#     exit 6 + assigned=none is the contract that makes the failure loud.
#   - the marker resolution breaking the filing. No session is a degradation
#     (file loudly without the marker), never a refusal.
#   - --no-assign assigning anyway — it is the service-call kill switch.
set -uo pipefail
unset CODEX_THREAD_ID

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

TMP="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$TMP"' EXIT

fails=0
ok()  { echo "  ok   $1"; }
bad() { echo "  FAIL $1 — $2"; fails=$((fails+1)); }

# ── a bin dir holding the unit under test + real msg + stubbed file-issue ────
# task-create resolves its collaborators from its own dirname, so the stub for
# file-issue has to live beside a copy of it, not on PATH.
STUB="$TMP/bin"; mkdir -p "$STUB"
cp "$PLUGIN_ROOT/bin/task-create" "$PLUGIN_ROOT/bin/msg" "$STUB/"
cp "$PLUGIN_ROOT/session_runtime.py" "$TMP/"

export FI_CALLS="$TMP/file-issue.log" GH_CALLS="$TMP/gh.log"
cat > "$STUB/file-issue" <<'STUBEOF'
#!/usr/bin/env bash
printf '%s\n' "file-issue $*" >> "$FI_CALLS"
# the body is what the marker rides in — capture it whole
prev=""
for a in "$@"; do
  [[ "$prev" == "--body" ]] && printf '%s' "$a" > "${FI_CALLS%.log}.body"
  prev="$a"
done
[[ "${FI_FAIL:-0}" != "0" ]] && exit "${FI_FAIL}"
case " $* " in *" --dry-run "*)
  echo "DRY RUN — would file"
  echo "ISSUE <dry-run> board=none type=bug"; exit 0 ;;
esac
echo "created label 'bug'" >&2
echo "ISSUE https://github.com/O/R/issues/77 board=Todo type=bug"
STUBEOF
chmod +x "$STUB/file-issue"

PATHSTUB="$TMP/pathbin"; mkdir -p "$PATHSTUB"
cat > "$PATHSTUB/gh" <<'STUBEOF'
#!/usr/bin/env bash
echo "gh $*" >> "$GH_CALLS"
[[ "${GH_FAIL:-0}" == "1" ]] && exit 1
exit 0
STUBEOF
chmod +x "$PATHSTUB/gh"
export PATH="$PATHSTUB:$PATH"

BIN="$STUB/task-create"

# ── a fake HOME with one seat and one transcript ─────────────────────────────
export HOME="$TMP/home"
export JSTACK_TASK_REPO="Acme/home" JSTACK_TASK_ASSIGNEE="acme-agent"
SID="aaaa1111-2222-3333-4444-555566667777"
SEAT_DIR="$HOME/Agents/Testa/chat"
mkdir -p "$SEAT_DIR" "$HOME/.claude/projects/x"
touch "$HOME/Agents/Testa/CLAUDE.md" "$SEAT_DIR/CLAUDE.md"
printf '{"cwd": "%s"}\n' "$SEAT_DIR" > "$HOME/.claude/projects/x/$SID.jsonl"

reset() { rm -f "$FI_CALLS" "${FI_CALLS%.log}.body" "$GH_CALLS"; }

# ── the marker rides the body when the session resolves ──────────────────────
reset
OUT=$(CLAUDE_CODE_SESSION_ID="$SID" "$BIN" --title T --body "the brief" --label bug 2>&1)
BODY="$(cat "${FI_CALLS%.log}.body" 2>/dev/null || true)"
if [[ "$BODY" == *"the brief"* && \
      "$BODY" == *"<!-- jj:task creator-session=$SID creator-seat=testa/chat -->"* ]]; then
  ok "creator marker embedded in the filed body"
else
  bad "creator marker embedded in the filed body" "body was: $BODY"
fi
if [[ "$OUT" == *"TASK https://github.com/O/R/issues/77 board=Todo type=bug assigned=acme-agent creator=testa/chat"* ]]; then
  ok "receipt names url, assignee, and creator seat"
else
  bad "receipt names url, assignee, and creator seat" "$OUT"
fi
if grep -q "issue edit 77 --repo Acme/home --add-assignee acme-agent" "$GH_CALLS"; then
  ok "assignment is the spawn — executor assigned on the default repo"
else
  bad "assignment is the spawn — executor assigned on the default repo" "$(cat "$GH_CALLS" 2>/dev/null)"
fi

# ── no session: filed loudly without the marker, never refused ───────────────
reset
OUT=$(env -u CLAUDE_CODE_SESSION_ID "$BIN" --title T --body B --label bug 2>&1); rc=$?
BODY="$(cat "${FI_CALLS%.log}.body" 2>/dev/null || true)"
if [[ $rc -eq 0 && "$BODY" != *"jj:task"* && "$OUT" == *"WITHOUT the marker"* \
      && "$OUT" == *"creator=none"* ]]; then
  ok "no session degrades to marker-less filing, loudly"
else
  bad "no session degrades to marker-less filing, loudly" "rc=$rc out=$OUT"
fi

# ── failed assignment: the issue exists and the exit says spawnless ──────────
reset
OUT=$(CLAUDE_CODE_SESSION_ID="$SID" GH_FAIL=1 "$BIN" --title T --body B --label bug 2>&1); rc=$?
if [[ $rc -eq 6 && "$OUT" == *"assigned=none"* && "$OUT" == *"nothing spawned"* ]]; then
  ok "failed assignment exits 6 with assigned=none"
else
  bad "failed assignment exits 6 with assigned=none" "rc=$rc out=$OUT"
fi

# ── --no-assign: the kill-switch path never touches gh ───────────────────────
reset
OUT=$(CLAUDE_CODE_SESSION_ID="$SID" "$BIN" --title T --body B --label bug --no-assign 2>&1); rc=$?
if [[ $rc -eq 0 && "$OUT" == *"assigned=none"* && ! -s "$GH_CALLS" ]]; then
  ok "--no-assign files without spawning"
else
  bad "--no-assign files without spawning" "rc=$rc gh=$(cat "$GH_CALLS" 2>/dev/null)"
fi

# ── a file-issue refusal propagates untouched ────────────────────────────────
reset
OUT=$(CLAUDE_CODE_SESSION_ID="$SID" FI_FAIL=64 "$BIN" --title T --body B --label bug 2>&1); rc=$?
if [[ $rc -eq 64 && ! -s "$GH_CALLS" ]]; then
  ok "file-issue refusal propagates, nothing assigned"
else
  bad "file-issue refusal propagates, nothing assigned" "rc=$rc out=$OUT"
fi

# ── dry-run: nothing filed, nothing assigned, receipt still whole ────────────
reset
OUT=$(CLAUDE_CODE_SESSION_ID="$SID" "$BIN" --title T --body B --label bug --dry-run 2>&1); rc=$?
if [[ $rc -eq 0 && "$OUT" == *"TASK <dry-run> board=none type=bug assigned=acme-agent creator=testa/chat"* \
      && ! -s "$GH_CALLS" ]]; then
  ok "dry-run previews the assignment without making it"
else
  bad "dry-run previews the assignment without making it" "rc=$rc out=$OUT"
fi

# ── the repo flag overrides the default ──────────────────────────────────────
reset
CLAUDE_CODE_SESSION_ID="$SID" "$BIN" --title T --body B --label bug --repo Other/place >/dev/null 2>&1
if grep -q -- "--repo Other/place" "$FI_CALLS" \
   && grep -q "issue edit 77 --repo Other/place" "$GH_CALLS"; then
  ok "--repo overrides the default for filing and assignment"
else
  bad "--repo overrides the default for filing and assignment" "$(cat "$FI_CALLS" "$GH_CALLS" 2>/dev/null)"
fi

# ── missing essentials refuse before anything happens ────────────────────────
reset
"$BIN" --body B --label bug >/dev/null 2>&1; rc1=$?
"$BIN" --title T --label bug >/dev/null 2>&1; rc2=$?
if [[ $rc1 -eq 64 && $rc2 -eq 64 && ! -s "$FI_CALLS" && ! -s "$GH_CALLS" ]]; then
  ok "no title / no body refuse with nothing filed"
else
  bad "no title / no body refuse with nothing filed" "rc1=$rc1 rc2=$rc2"
fi

# ── no configured repo or executor: refuse loudly, name the knob ─────────────
reset
OUT=$(env -u JSTACK_TASK_REPO "$BIN" --title T --body B --label bug 2>&1); rc1=$?
OUT2=$(env -u JSTACK_TASK_ASSIGNEE "$BIN" --title T --body B --label bug 2>&1); rc2=$?
env -u JSTACK_TASK_ASSIGNEE "$BIN" --title T --body B --label bug --no-assign >/dev/null 2>&1; rc3=$?
if [[ $rc1 -eq 64 && "$OUT" == *"JSTACK_TASK_REPO"* \
      && $rc2 -eq 64 && "$OUT2" == *"JSTACK_TASK_ASSIGNEE"* && $rc3 -eq 0 ]]; then
  ok "unset env refuses with the knob named; --no-assign needs no executor"
else
  bad "unset env refuses with the knob named; --no-assign needs no executor" "rc=$rc1/$rc2/$rc3 out=$OUT | $OUT2"
fi

echo ""
if [[ $fails -gt 0 ]]; then
  echo "$fails test(s) failed" >&2
  exit 1
fi
echo "ALL PASS — task-create"
