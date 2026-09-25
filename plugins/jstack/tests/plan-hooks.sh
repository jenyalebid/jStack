#!/usr/bin/env bash
# jStack live test — plan mode captured as rows, on both engines.
#
# Drives the three real hook scripts with the real JSON stdin contract against a
# store built for this run. The property that matters most here is not that the
# gate catches a bad plan — it is CONTAINMENT: `plan-exit.py` stands in front of
# `ExitPlanMode` for every session on this machine, so every shape that must NOT
# be blocked is pinned as hard as the one shape that must be, and a hook that
# throws is pinned to let the tool through.
#
# ORDINALS ARE THE CONTRACT IN hooks.json. Codex keys hook trust by position —
# "…:hooks.json:<event>:<group>:<hook>" with a persisted hash — so inserting a
# group or a hook ABOVE an existing one shifts every later ordinal and silently
# stops those hooks running: no error, no warning, exit 0. New hooks go at the
# END of an existing group's array; new groups go at the END of the event's.
# Section (10) writes every plan ordinal out literally.
#
# Exit 0 = all pass. Exit 1 = any fail. Hermetic: its own store, its own markers,
# its own tasks directory, nothing of the machine's touched.

set -u

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO_ROOT="$(cd "$PLUGIN_ROOT/../.." && pwd)"
HOST="$REPO_ROOT/host"
WATCH="$PLUGIN_ROOT/hooks/plan-mode-watch.py"
EXIT_HOOK="$PLUGIN_ROOT/hooks/plan-exit.py"
TASKS_HOOK="$PLUGIN_ROOT/hooks/plan-tasks.py"

# The host package must be importable by whatever runs the hooks; a venv with it
# installed is named here instead of the PATH python.
PY="${JSTACK_TEST_PYTHON:-python3}"

for f in "$WATCH" "$EXIT_HOOK" "$TASKS_HOOK"; do
  [[ -f "$f" ]] || { echo "FAIL: hook not found at $f" >&2; exit 1; }
done
command -v "$PY" >/dev/null 2>&1 || { echo "FAIL: $PY not runnable" >&2; exit 1; }

TMP="$(mktemp -d "${TMPDIR:-/tmp}/jstack-plantest.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

STATE="$TMP/state"
CACHE="$TMP/cache"
TASKS="$TMP/tasks"
CLAUDE_T="$TMP/claude.jsonl"
CODEX_T="$TMP/rollout-codex.jsonl"
mkdir -p "$STATE" "$CACHE" "$TASKS"
echo '{"role":"user","content":"seed"}' > "$CLAUDE_T"
echo '{"type":"session_meta","payload":{"id":"native"}}' > "$CODEX_T"

export JREMOTE_STATE_DIR="$STATE"
export JSTACK_CACHE_ROOT="$CACHE"
export JSTACK_TASKS_DIR="$TASKS"
unset JSTACK_PLAN_GATE_DISABLED

fail() { echo "FAIL [$1]: $2" >&2; exit 1; }
pass() { echo "PASS [$1]"; }

precheck() {
  local out
  out="$("$PY" - "$HOST" <<'EOF' 2>&1
import sys
sys.path.insert(0, sys.argv[1])
from jstack_host import plan_parse, plans
assert plan_parse.KINDS[0] == "command"
assert callable(plans.open_plan_for_session)
print("ok")
EOF
)"
  [[ "$out" == "ok" ]] || fail "precheck" "host not importable by $PY — every
  silence below would be a lie: $out"
  pass "precheck"
}

# `query <session_id> <expression>` — the store's own answer, printed. `p` is the
# session's open plan (or None), `st` its stages, `plans` the module.
query() {
  "$PY" - "$HOST" "$1" "$2" <<'EOF'
import sys
sys.path.insert(0, sys.argv[1])
from jstack_host import plans
sid = sys.argv[2]
p = plans.open_plan_for_session(sid)
st = plans.stages(p["id"]) if p else []
tk = [t for s in st for t in plans.tasks(s["id"])]
all_plans = plans.plans_for_session(sid)
print(eval(sys.argv[3]))
EOF
}

# The hook payloads, one per engine where the shape differs. `permission_mode`
# is the field that carries plan mode on BOTH engines, and is the whole point.
prompt() {  # prompt <sid> <mode> <transcript> [prompt text]
  printf '{"hook_event_name":"UserPromptSubmit","session_id":"%s","permission_mode":"%s","prompt":"%s","cwd":"/work/repo","transcript_path":"%s"}' \
    "$1" "$2" "${4:-build the harness}" "$3" | "$PY" "$WATCH"
}
stopped() {  # stopped <sid> <mode> <transcript>
  printf '{"hook_event_name":"Stop","session_id":"%s","permission_mode":"%s","transcript_path":"%s"}' \
    "$1" "$2" "$3" | "$PY" "$WATCH"
}
exiting() {  # exiting <sid> <plan-markdown-file>
  "$PY" - "$1" "$2" "$CLAUDE_T" <<'EOF' | "$PY" "$EXIT_HOOK"
import json, sys
print(json.dumps({"hook_event_name": "PreToolUse", "tool_name": "ExitPlanMode",
                  "session_id": sys.argv[1], "cwd": "/work/repo",
                  "permission_mode": "plan", "transcript_path": sys.argv[3],
                  "tool_input": {"plan": open(sys.argv[2]).read(),
                                 "planFilePath": sys.argv[2]}}))
EOF
}
# The deny reason, or "" when the hook let the tool through. Anything that is
# neither is a failure, not an empty string.
reason() {
  local raw="$1"
  [[ -z "$raw" ]] && { printf ''; return; }
  printf '%s' "$raw" | "$PY" -c '
import json, sys
out = json.load(sys.stdin)["hookSpecificOutput"]
assert out["permissionDecision"] == "deny", out
sys.stdout.write(out["permissionDecisionReason"])
' || fail "reason" "hook output is not a deny envelope: $raw"
}

cat > "$TMP/good.md" <<'EOF'
# The work harness

## Stage 1 — the parser that reads the format
Prose about the parser.
Verify: command · ./verify/parse.sh

## Stage 2 — the rule the format is written in
Verify: artifact · plugins/jstack/rules-stage/execution-gates.md
EOF

cat > "$TMP/bad.md" <<'EOF'
# The work harness

## Stage 1 — the parser
Verify: comand · ./verify/parse.sh

## Stage 2 — the CLI
No proof line anywhere in this stage.

## Stage 9 — the gate
Verify: command
EOF

cat > "$TMP/one.md" <<'EOF'
# One stage only

## Stage 1 — the parser
No proof line at all, and it is still not blocked.
EOF

cat > "$TMP/none.md" <<'EOF'
# A plan in the old wording

## Phase 1 — do the thing
## Phase 2 — do the other thing
EOF

cat > "$TMP/cosmetic.md" <<'EOF'
# Numbered by hand

## Stage 3 — the parser
Verify: command · ./verify/parse.sh

## Stage 4 — the rule
Verify: none

## Verification
1. The merge command refuses on a red receipt.
EOF

precheck

# (1) `permission_mode: "plan"` mints the planning row. Both engines, because
#     the field is the only signal Codex gives that a session is planning.
S=claude-$$
[[ -z "$(prompt $S plan "$CLAUDE_T")" ]] || fail "claude-planning" "entry spoke"
[[ "$(query $S 'p and p["status"]')" == "planning" ]] || fail "claude-planning" "no planning row"
[[ "$(query $S 'p and p["engine"]')" == "claude" ]] || fail "claude-planning" "engine misread"
[[ "$(query $S 'p and p["title"]')" == "build the harness" ]] || fail "claude-planning" "no title"
C=codex-$$
[[ -z "$(prompt $C plan "$CODEX_T")" ]] || fail "codex-planning" "entry spoke"
[[ "$(query $C 'p and p["status"]')" == "planning" ]] || fail "codex-planning" "no planning row"
[[ "$(query $C 'p and p["engine"]')" == "codex" ]] || fail "codex-planning" "engine misread"
pass "planning-row-both-engines"

# (2) The row is minted once. A hook that re-mints on every prompt would bury
#     the Work tab under one plan per turn of a planning session.
prompt $S plan "$CLAUDE_T" >/dev/null
prompt $S plan "$CLAUDE_T" >/dev/null
[[ "$(query $S 'len(all_plans)')" == "1" ]] || fail "mint-once" "a second plan was minted"
pass "mint-once"

# (2b) THE CODEX EXIT, AND WHAT IT DOES NOT DO. Codex has no `ExitPlanMode`, so
#      its exit IS the plan → not-plan flip, seen here. No markdown arrives with
#      it and none is invented: the row stays `planning` with no stages, and the
#      one party holding the text is told, once, how to file them.
X=codexexit-$$
prompt $X plan "$CODEX_T" >/dev/null
out="$(stopped $X default "$CODEX_T")"
[[ -n "$out" ]] || fail "codex-exit" "the exit transition said nothing"
said="$(printf '%s' "$out" | "$PY" -c 'import json,sys; sys.stdout.write(json.load(sys.stdin)["hookSpecificOutput"]["additionalContext"])')"
[[ "$said" == *"jstack-host plan stages"* ]] || fail "codex-exit" "no remedy in: $said"
[[ "$said" == *"$(query $X 'p and p["id"]')"* ]] || fail "codex-exit" "the plan id is not in the nudge"
[[ "$(query $X 'p and p["status"]')" == "planning" ]] || fail "codex-exit" "a stageless plan was activated"
[[ -z "$(stopped $X default "$CODEX_T")" ]] || fail "codex-exit" "the nudge repeated"
[[ -z "$(prompt $X default "$CODEX_T")" ]] || fail "codex-exit" "the nudge repeated on the next prompt"
pass "codex-exit-honest-partial"

# (3) A well-formed plan: stages written with their kinds and specs, the plan
#     flipped to active, and the tool NOT blocked.
out="$(exiting $S "$TMP/good.md")"
[[ -z "$out" ]] || fail "good-plan" "a valid plan was blocked: $out"
[[ "$(query $S 'p and p["status"]')" == "active" ]] || fail "good-plan" "plan not activated"
[[ "$(query $S '[(s["ordinal"], s["verify_kind"], s["verify_spec"]) for s in st]')" \
   == "[(1, 'command', './verify/parse.sh'), (2, 'artifact', 'plugins/jstack/rules-stage/execution-gates.md')]" ]] \
   || fail "good-plan" "stages wrong: $(query $S 'st')"
pass "good-plan-recorded"

# (3b) The row was minted at the first plan-mode PROMPT, so it carries that
#      prompt as its title and no file at all. Approval is the only moment the
#      authored title and the real path exist — and `plan_file` is the column
#      the document route reads, so a plan that misses it here has a document
#      nobody can open for the rest of its life.
[[ "$(query $S 'p and p["title"]')" == "The work harness" ]] \
   || fail "authored-meta" "title still the prompt's: $(query $S 'p and p["title"]')"
[[ "$(query $S 'p and p["plan_file"]')" == "$TMP/good.md" ]] \
   || fail "authored-meta" "plan_file not recorded: $(query $S 'p and p["plan_file"]')"
pass "authored-title-and-file"

# (4) The one shape that blocks, and what the author is handed. They have almost
#     certainly never seen `rules-stage/execution-gates.md` — plan mode does not
#     author through Edit, so the path-scoped rule never injected — which is why
#     the contract has to be IN the message.
B=blocked-$$
prompt $B plan "$CLAUDE_T" >/dev/null
text="$(reason "$(exiting $B "$TMP/bad.md")")"
[[ -n "$text" ]] || fail "malformed-blocked" "a malformed 2-stage plan sailed through"
for want in \
    'Stage 1 "the parser" declares `Verify: comand`' \
    'Stage 2 "the CLI" has no `Verify:` line' \
    'Verify: command · ./verify/parse.sh' \
    'Verify: comand · ./verify/parse.sh' \
    'Stage 3 "the gate" declares `Verify: command` with nothing to run' \
    'command, commit, artifact, manual, none'; do
  [[ "$text" == *"$want"* ]] || fail "malformed-blocked" "block text lacks: $want
--- text ---
$text"
done
# One sentence per refused stage. Stage 3 is refused for its empty spec AND
# numbered 9; the numbering belongs under ALSO NOTED, or the author reads
# renumbering their headings as a way to get through the gate.
[[ "${text%%ALSO NOTED*}" != *"is numbered 9"* ]] || fail "malformed-blocked" \
  "a cosmetic complaint was filed as a reason for the refusal:
$text"
[[ "${text##*ALSO NOTED}" == *"is numbered 9"* ]] || fail "malformed-blocked" \
  "the numbering complaint was not reported at all:
$text"
[[ "$(query $B 'p and p["status"]')" == "planning" ]] || fail "malformed-blocked" "a refused plan was activated"
[[ "$(query $B 'len(st)')" == "0" ]] || fail "malformed-blocked" "a refused plan wrote stages"
pass "malformed-blocked"

# (5) THE CONTAINMENT. Ten of the twelve plans on this machine parse to zero
#     stages and one to a single stage; none of them may ever be blocked, no
#     matter how malformed, because their authors never opted into the format.
for shape in one none; do
  S1="cont-$shape-$$"
  prompt $S1 plan "$CLAUDE_T" >/dev/null
  out="$(exiting $S1 "$TMP/$shape.md")"
  [[ -z "$out" ]] || fail "containment" "the $shape-stage plan was blocked: $out"
done
pass "containment-0-and-1-stage"

# (6) Cosmetic complaints are not refusals. An ordinal that disagrees with
#     document order and an orphan `## Verification` are both reported by the
#     parser and neither is a reason to stand in someone's way — blocking on one
#     is how a safety mechanism gets switched off by the first person it annoys.
K=cosmetic-$$
prompt $K plan "$CLAUDE_T" >/dev/null
out="$(exiting $K "$TMP/cosmetic.md")"
[[ -z "$out" ]] || fail "cosmetic-allowed" "a cosmetic complaint blocked the tool: $out"
[[ "$(query $K 'len(st)')" == "2" ]] || fail "cosmetic-allowed" "stages not written"
pass "cosmetic-allowed"

# (7) Our own bug must cost the session nothing. A store that cannot be opened
#     stands in for any exception raised inside the hook: the tool goes through,
#     nothing is printed, exit 0.
printf 'not a directory' > "$TMP/brokenstate"
G=broken-$$
out="$(JREMOTE_STATE_DIR="$TMP/brokenstate" bash -c "$(declare -f exiting); PY='$PY'; EXIT_HOOK='$EXIT_HOOK'; CLAUDE_T='$CLAUDE_T'; exiting $G '$TMP/good.md'"; echo "rc=$?")"
[[ "$out" == "rc=0" ]] || fail "fail-open" "a broken store did not let the tool through: $out"
out="$(JREMOTE_STATE_DIR="$TMP/brokenstate" bash -c "$(declare -f prompt); PY='$PY'; WATCH='$WATCH'; prompt $G plan '$CLAUDE_T'"; echo "rc=$?")"
[[ "$out" == "rc=0" ]] || fail "fail-open" "the watch hook did not survive a broken store: $out"
pass "fail-open-on-own-error"

# (8) The native task lists, mirrored onto the stage being worked. Claude's own
#     rows are richer than ours; id, subject and status come across and the
#     stage id goes back into `metadata` so the link reads from both sides.
mkdir -p "$TASKS/$S"
cat > "$TASKS/$S/1.json" <<'EOF'
{"id": "1", "subject": "write the parser", "description": "…", "activeForm": "Writing",
 "status": "completed", "blocks": [], "blockedBy": [], "metadata": {"note": "kept"}}
EOF
cat > "$TASKS/$S/2.json" <<'EOF'
{"id": "2", "subject": "write the gate", "status": "in_progress", "metadata": {}}
EOF
printf '{"hook_event_name":"PostToolUse","session_id":"%s","tool_name":"TaskUpdate","tool_input":{},"transcript_path":"%s"}' \
  "$S" "$CLAUDE_T" | "$PY" "$TASKS_HOOK" || fail "claude-tasks" "hook exited nonzero"
[[ "$(query $S '[(t["native_id"], t["subject"], t["status"]) for t in tk]')" \
   == "[('1', 'write the parser', 'completed'), ('2', 'write the gate', 'in_progress')]" ]] \
   || fail "claude-tasks" "rows wrong: $(query $S 'tk')"
"$PY" -c '
import json, sys
row = json.load(open(sys.argv[1]))
assert row["metadata"]["note"] == "kept", "the stamp clobbered the native metadata"
assert row["metadata"]["stage_id"], "no stage_id written back"
' "$TASKS/$S/1.json" || fail "claude-tasks" "metadata not stamped"
pass "claude-tasks-mirrored"

# (9) Codex sends the whole flat list on every call, so the last call is the
#     current plan. No task directory exists there, and a Codex stage with no
#     tasks at all is normal — the tool is gated by `[tools] update_plan`.
prompt $C plan "$CODEX_T" >/dev/null
exiting $C "$TMP/good.md" >/dev/null
printf '{"hook_event_name":"PostToolUse","session_id":"%s","tool_name":"update_plan","transcript_path":"%s","tool_input":{"plan":[{"step":"read the format","status":"completed"},{"step":"write the hook","status":"in_progress"}]}}' \
  "$C" "$CODEX_T" | "$PY" "$TASKS_HOOK" || fail "codex-tasks" "hook exited nonzero"
[[ "$(query $C '[(t["subject"], t["status"]) for t in tk]')" \
   == "[('read the format', 'completed'), ('write the hook', 'in_progress')]" ]] \
   || fail "codex-tasks" "rows wrong: $(query $C 'tk')"
pass "codex-tasks-mirrored"

# (10) The ordinals, pinned — see the note at the top of this file. Written out
#      rather than derived, so that a group inserted above one of these fails
#      here instead of silently un-trusting whatever moved down.
"$PY" - "$PLUGIN_ROOT/hooks/hooks.json" <<'EOF' || fail "append-only" "a plan hook moved — read the ordinal note in this file"
import json, sys
manifest = json.load(open(sys.argv[1]))["hooks"]
for event, group, index, name, matcher in (
        ("UserPromptSubmit", 1, 0, "plan-mode-watch.py", None),
        ("Stop", 1, 0, "plan-mode-watch.py", None),
        ("PreToolUse", 5, 0, "plan-exit.py", "ExitPlanMode"),
        ("PostToolUse", 3, 0, "plan-tasks.py", "TaskCreate|TaskUpdate|update_plan")):
    groups = manifest[event]
    assert group == len(groups) - 1, f"{name}'s group is no longer last in {event}"
    hooks = groups[group]["hooks"]
    assert hooks[index]["command"].endswith("/" + name), \
        f"{event}:{group}:{index} is not {name}"
    assert index == len(hooks) - 1, f"{name} is no longer last in {event}[{group}]"
    assert groups[group].get("matcher") == matcher, f"{name}'s matcher changed"
EOF
pass "append-only"

# (11) A Codex matcher does not name a tool Codex will never send. It widened
#      the group to nothing: `Bash|ExitPlanMode|Agent` selected two tools and
#      claimed three. A group that still names a tool Codex has keeps it and
#      loses the rest; a group naming nothing else cannot fire at all and is
#      left out of the file entirely, reported rather than written — a dead
#      line in an operator-owned config answers "is the gate wired on Codex"
#      with a yes.
"$PY" - "$HOST" "$PLUGIN_ROOT" <<'EOF' || fail "codex-matchers" "a Codex matcher lost or kept the wrong tool"
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from jstack_host import codex_hooks
body, _ = codex_hooks.managed_config(Path(sys.argv[2]))
assert 'matcher = "Bash|Agent"' in body, "the absent tool was not stripped from a mixed matcher"
assert 'matcher = "Bash|ExitPlanMode|Agent"' not in body, "the absent tool survived"
assert 'matcher = "Edit|Write"' in body, "a matcher with nothing absent in it was rewritten"
assert 'matcher = "ExitPlanMode"' not in body, "a group Codex could never fire was registered"
assert "plan-exit.py" not in body, "the Claude-only plan gate was written into Codex's config"
_, dropped = codex_hooks.managed_hooks(
    __import__("json").loads((Path(sys.argv[2]) / "hooks/hooks.json").read_text()),
    Path(sys.argv[2]))
assert "PreToolUse[ExitPlanMode]" in dropped, f"dropped silently: {dropped}"
EOF
pass "codex-matchers"

echo ""
echo "ALL PASS — plan mode captured as rows, and the gate contained"
