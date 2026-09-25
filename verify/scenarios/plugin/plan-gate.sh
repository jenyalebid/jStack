#!/bin/bash
# WHAT: a real session plans the demo project, the gate refuses what the work has not earned, closes on what it has, and every plan hook leaves its mark
# TIME: ~40m
# GUEST: derived from $JSTACK_VERIFY_AUTHED_BASE, signed in with the lab token (#140)
#
# The project is verify/fixtures/plan-demo: two stages whose Verify: lines run
# its tests, stubs that fail them, and a stage 2 gate that also re-runs stage
# 1's. So the gate is graded on work — the untouched project must be refused at
# both stages, and only the session's real edits can turn either green.
#
# The ref under test is baked in (JSTACK_VERIFY_REF, default dev): `vm term`
# carries none of this shell's environment, and a clone of `main` carries no
# harness. The Hub is installed from that ref, so the CLI, the API and the
# hooks all read the one store that Hub serves. The two plan-mode sessions are
# interactive (work_guest.sh plan_session): the print CLI has no plan-mode tools.
. "$(dirname "$0")/../../lib/common.sh"

guest_from "${JSTACK_VERIFY_AUTHED_BASE:-jstack-base-authed}" vfy-plugin-plan-gate

LIB="$(dirname "$0")/../../lib"
vm cp "$GUEST" "$LIB/work_probe.py" /Users/admin/work_probe.py >/dev/null
vm cp "$GUEST" "$LIB/work_guest.sh" /Users/admin/work_guest.sh >/dev/null
vm cp "$GUEST" "$(dirname "$0")/../../fixtures/plan-demo" /Users/admin/plan-demo >/dev/null
guest_signin

cat > "$RECEIPTS/payload.sh" <<EOF
#!/bin/bash
REF='${JSTACK_VERIFY_REF:-dev}'
EOF

cat >> "$RECEIPTS/payload.sh" <<'EOF'
DONE=DONE-PLUGIN-PLAN-GATE
. /Users/admin/work_guest.sh

hub_at_ref
hook_host_check

FIXTURE=/Users/admin/plan-demo
PROJ="$HOME/plan-journey"
[ -f "$FIXTURE/plan.md" ] && [ -f "$FIXTURE/checks/stage1.sh" ] \
    || bail "the demo project did not reach the guest at $FIXTURE"
rm -rf "$PROJ" && cp -R "$FIXTURE" "$PROJ"
# The tightened plan: the same stages, stage 2's gate moved to the script that
# also re-runs stage 1. The reconcile keys on (plan_id, ordinal), so only the
# Verify: line may differ or stage 2 would be compared against another row.
sed 's|^Verify: command · python3 -m unittest -q tests.test_report$|Verify: command · sh checks/stage2.sh|' \
    "$PROJ/plan.md" > "$HOME/plan-v2.md"
grep -q '^Verify: command · sh checks/stage2.sh$' "$HOME/plan-v2.md" \
    || bail "the tightened plan did not take its new gate"
# What the gates grade must be what the fixture shipped: a session that
# rewrote a test into a pass would otherwise read as a session that did the work.
sums() { (cd "$PROJ" && find tests checks -type f | sort | xargs shasum) | shasum | cut -d' ' -f1; }
SUMS0="$(sums)"

plan_json() { jstack-host plan show "$1" --json; }
sfield() {  # <plan-id> <ordinal> <field>
    plan_json "$1" | python3 -c 'import json,sys
d = json.load(sys.stdin)
o = int(sys.argv[1])
print(next((str(s.get(sys.argv[2], "")) for s in d["stages"] if s["ordinal"] == o), ""))' "$2" "$3"
}
last_proof() {  # <plan-id> <ordinal> <field> — of the newest proof on that stage
    plan_json "$1" | python3 -c 'import json,sys
d = json.load(sys.stdin)
s = next((x for x in d["stages"] if x["ordinal"] == int(sys.argv[1])), {})
p = (s.get("proofs") or [{}])[-1]
print(str(p.get(sys.argv[2], "")))' "$2" "$3"
}
plan_ids() { jstack-host plan list --json | python3 -c 'import json,sys; print(" ".join(sorted(p["id"] for p in json.load(sys.stdin))))'; }
new_ids() {  # the plan rows that did not exist before this journey
    python3 -c 'import sys; b = set(sys.argv[1].split()); print(" ".join(x for x in sys.argv[2].split() if x not in b))' "$BEFORE" "$(plan_ids)"
}
prompt_for() {
    printf '%s\n\n%s\n' \
        "Put the markdown below into your plan file exactly as written — every line, nothing added, removed or reworded — then call the ExitPlanMode tool. Do not read the project, run commands, or ask questions. Once the plan is approved, do nothing further: do not implement anything, reply only PLAN APPROVED." \
        "$(cat "$1")"
}
set_env() {  # set_env <sid> <key> <value|"">
    probe api POST "/sessions/$1/env" "{\"key\": \"$2\", \"value\": \"$3\"}" >/dev/null \
        || bail "POST /sessions/$1/env $2=$3 was refused"
}
same_sid() { [ "$R_SID" = "$SID" ] || bail "the engine ran as '${R_SID:-nothing}', not $SID — every row and marker below would be under an id this script is not reading"; }
snapshot() { cat "$MARKS/$SID/environment.json" 2>/dev/null; }
refused_as_missing() {  # <stage-id> <label>
    out="$(jstack-host plan done "$1" 2>&1)"; rc=$?
    say "  refusal: $out"
    [ "$rc" != "0" ] || { echo "FAIL $2: plan done closed a stage with no passing proof"; return; }
    case "$out" in
        *"verify_kind='command'"*"has no passing proof"*"it stays open"*"jstack-host plan verify $1"*)
            say "OK $2: refused, naming the gate, the missing proof and the command that would satisfy it" ;;
        *)  echo "FAIL $2: the refusal does not name gate, proof, consequence and remedy" ;;
    esac
}

SID="$(uuidgen | tr 'A-Z' 'a-z')"
BEFORE="$(plan_ids)"
# Two settings on this session before it exists, each triggered at a plan
# moment: use_subagents at PreToolUse ExitPlanMode, delivery_method at Stop.
# Without them env-announce has nothing to say during a plan and cannot be
# seen firing at all.
V="$(python3 -c 'import random; print(random.choice(["distribute", "testflight", "build", "sim_demo"]))')"
set_env "$SID" use_subagents on
set_env "$SID" delivery_method "$V"

echo "== a real plan-mode session authors the demo plan and approves it =="
plan_session plan "$PROJ" "$SID" "$PROJ/plan.md"
same_sid

set -- $(new_ids)
[ $# -eq 1 ] || bail "expected one new plan row from this session, got $#: $*"
P="$1"
say "OK the plan-mode session minted one plan row ($P)"
work_plan="$(probe api GET "/sessions/$SID/work" | probe py '(d.get("plan") or {}).get("id", "")')"
[ "$work_plan" = "$P" ] \
    && say "OK /sessions/{sid}/work names that row as this session's plan" \
    || echo "FAIL /sessions/$SID/work names '${work_plan:-no plan}', not $P"

[ -f "$MARKS/$SID/plan-mode.json" ] \
    && say "OK plan-mode-watch saw permission_mode=plan and left its marker: $(cat "$MARKS/$SID/plan-mode.json")" \
    || echo "FAIL plan-mode-watch left no plan-mode.json under $MARKS/$SID"
[ "$(sfield "$P" 1 verify_spec)" = "sh checks/stage1.sh" ] \
    && [ "$(sfield "$P" 2 verify_spec)" = "python3 -m unittest -q tests.test_report" ] \
    && [ "$(sfield "$P" 2 verify_kind)" = "command" ] \
    && say "OK plan-exit parsed both Verify: lines into stage rows" \
    || bail "the stage rows do not carry the plan's gates: '$(sfield "$P" 1 verify_spec)' / '$(sfield "$P" 2 verify_spec)'"
pmeta="$(plan_json "$P" | probe py '[d["plan"]["status"], d["plan"]["title"], d["plan"]["repo"], d["plan"]["plan_file"]]')"
say "  plan row: $pmeta"
[ "$(plan_json "$P" | probe py 'd["plan"]["status"]')" = "active" ] \
    && say "OK approval flipped the plan to active" \
    || echo "FAIL the plan is not active after approval"
[ "$(plan_json "$P" | probe py 'd["plan"]["repo"]')" = "$PROJ" ] \
    && say "OK the plan's repo is the directory it was authored in" \
    || echo "FAIL the plan's repo is not $PROJ — its gates would run somewhere else"
for m in "use_subagents-on" "delivery_method-$V"; do
    [ -f "$MARKS/$SID/env-$m.marker" ] \
        && say "OK env-announce marked env-$m during the plan session" \
        || echo "FAIL no env-$m.marker — env-announce did not speak at its trigger"
done
[ "$(snapshot | probe py 'sorted(d.items())')" = "$(printf '{"delivery_method": "%s", "use_subagents": "on"}' "$V" | probe py 'sorted(d.items())')" ] \
    && say "OK env-delta snapshotted this session's settings: $(snapshot)" \
    || echo "FAIL env-delta's snapshot is '$(snapshot)'"

pf="$(plan_json "$P" | probe py 'd["plan"]["plan_file"]')"
if [ -z "$pf" ]; then
    echo "GAP plan document: ExitPlanMode carried no planFilePath in this engine run, so GET /plans/{id}/document has no file to serve"
elif probe api GET "/plans/$P/document" | grep -q 'Stage 1 — count words'; then
    say "OK GET /plans/{id}/document serves the approved markdown from $pf"
else
    echo "FAIL the document route does not serve $pf"
fi

S1="$(sfield "$P" 1 id)"; S2="$(sfield "$P" 2 id)"

echo "== the untouched project: both gates must refuse =="
# From $HOME, not the project: `plan verify` is graded in the plan's repo.
cd "$HOME"
jstack-host plan verify "$S1" >/dev/null 2>&1 \
    && echo "FAIL stage 1's gate passed a project nobody touched" \
    || say "OK stage 1's gate fails on the untouched project"
# The failure must be the test failing, not the check not being found — a
# gate run in the wrong directory fails too, for no reason worth anything.
last_proof "$P" 1 output | grep -q NotImplementedError \
    && say "OK it failed on count_words' NotImplementedError — graded in the plan's repo" \
    || echo "FAIL stage 1's failing proof is not the test's failure: $(last_proof "$P" 1 output | tail -2)"
refused_as_missing "$S1" "stage 1, untouched"
[ "$(sfield "$P" 1 status)" != "done" ] || echo "FAIL a refused close moved stage 1 to done"
jstack-host plan verify "$S2" >/dev/null 2>&1 \
    && echo "FAIL stage 2's gate passed a project nobody touched" \
    || say "OK stage 2's gate fails on the untouched project"
refused_as_missing "$S2" "stage 2, untouched"

echo "== a green proof filed against a gate that then tightens =="
jstack-host plan proof "$S2" --kind command --ok \
    --detail 'hand-filed against the v1 gate' >/dev/null 2>&1 \
    || echo "FAIL could not file a proof by hand"
plan_session replan "$PROJ" "$SID" "$HOME/plan-v2.md" resume
same_sid
set -- $(new_ids)
[ $# -eq 1 ] && [ "$1" = "$P" ] \
    && say "OK the re-approval reconciled this session's plan row, it did not mint a second" \
    || echo "FAIL re-approval left these new plan rows: $*"
[ "$(sfield "$P" 2 id)" = "$S2" ] && [ "$(sfield "$P" 2 verify_spec)" = "sh checks/stage2.sh" ] \
    && say "OK stage 2 kept its row and its gate moved to sh checks/stage2.sh" \
    || echo "FAIL stage 2 is '$(sfield "$P" 2 id)' declaring '$(sfield "$P" 2 verify_spec)'"
out="$(jstack-host plan done "$S2" 2>&1)"; rc=$?
if [ "$rc" = "0" ]; then
    echo "FAIL the hand-filed proof closed the tightened stage 2"
else
    case "$out" in
        *"proves the gate this stage used to have"*)
            say "OK the refusal says the green proof belongs to the old gate" ;;
        *)  echo "FAIL the refusal does not tell a retired proof from no proof: $out" ;;
    esac
fi

echo "== stage 1, worked by the session =="
# Cleared between prompts, so the next prompt is a flip env-delta must record.
set_env "$SID" use_subagents ""
jstack-host plan start "$S1" --session "$SID" >/dev/null 2>&1 || echo "FAIL plan start refused stage 1"
before_tasks="$(probe api GET "/sessions/$SID/work" | probe py "len(d['tasks'].get('$S1', []))")"
[ "$before_tasks" = "0" ] || echo "FAIL stage 1 already had $before_tasks tasks before any session work"
run_claude stage1 "$PROJ" --resume "$SID" --permission-mode acceptEdits \
    --allowedTools "Read Edit Write Bash TaskCreate TaskUpdate TaskList" \
    -p 'You are working stage 1 ("count words") of the approved plan, in this directory. First use the TaskCreate tool to create exactly two tasks: "implement count_words" and "run the stage 1 tests". Then implement count_words in demo/wordcount.py as its docstring says, and run `python3 -m unittest -q tests.test_wordcount` until it passes. Do not change anything under tests/ or checks/, and do not touch demo/report.py. Do the work yourself in this session. Mark both tasks completed with TaskUpdate, then reply DONE.'
same_sid
[ "$(sums)" = "$SUMS0" ] || bail "the session changed tests/ or checks/ — every gate below would grade a rewritten test"
[ "$(snapshot | probe py 'sorted(d.items())')" = "$(printf '{"delivery_method": "%s"}' "$V" | probe py 'sorted(d.items())')" ] \
    && say "OK env-delta recorded use_subagents leaving between prompts: $(snapshot)" \
    || echo "FAIL env-delta's snapshot after the flip is '$(snapshot)'"

tasks="$(probe api GET "/sessions/$SID/work" | probe py "sorted((t['subject'], t['status']) for t in d['tasks'].get('$S1', []))")"
say "  stage 1 tasks on the Hub: $tasks"
case "$tasks" in
    *"implement count_words"*"run the stage 1 tests"*|*"run the stage 1 tests"*"implement count_words"*)
        say "OK plan-tasks mirrored the session's TaskCreate calls onto the running stage" ;;
    *)  echo "FAIL the session's tasks did not reach stage 1 — ~/.claude/tasks/$SID holds: $(ls "$HOME/.claude/tasks/$SID" 2>&1 | head -3 | tr '\n' ' ')" ;;
esac
stamped="$(python3 - "$HOME/.claude/tasks/$SID" "$S1" <<'PY'
import json, pathlib, sys
rows = [json.loads(p.read_text()) for p in pathlib.Path(sys.argv[1]).glob("*.json")]
print(f"{sum(1 for r in rows if (r.get('metadata') or {}).get('stage_id') == sys.argv[2])}/{len(rows)}")
PY
)"
case "$stamped" in
    0/*|"") echo "FAIL no native task under ~/.claude/tasks/$SID carries stage 1's id ($stamped)" ;;
    *)      say "OK plan-tasks wrote stage 1's id back into the native tasks ($stamped)" ;;
esac

jstack-host plan verify "$S1" >/dev/null 2>&1 \
    && say "OK stage 1's gate passes on the session's work" \
    || echo "FAIL stage 1's gate still fails: $(last_proof "$P" 1 output | tail -3)"
out="$(jstack-host plan done "$S1" 2>&1)" && [ "$(sfield "$P" 1 status)" = "done" ] \
    && say "OK stage 1 closed on its own passing proof" \
    || echo "FAIL stage 1 did not close: $out"
jstack-host plan verify "$S2" >/dev/null 2>&1 \
    && echo "FAIL stage 2's gate passed on stage 1's work alone" \
    || say "OK stage 2's gate still fails — stage 1's work does not earn it"

echo "== stage 2, worked by the session =="
jstack-host plan start "$S2" --session "$SID" >/dev/null 2>&1 || echo "FAIL plan start refused stage 2"
run_claude stage2 "$PROJ" --resume "$SID" --permission-mode acceptEdits \
    --allowedTools "Read Edit Write Bash TaskCreate TaskUpdate TaskList" \
    -p 'You are working stage 2 ("report the most common words") of the approved plan, in this directory. First use the TaskCreate tool to create one task: "implement top". Then implement top in demo/report.py as its docstring says, on top of count_words, and run `sh checks/stage2.sh` until it passes. Do not change anything under tests/ or checks/. Do the work yourself in this session. Mark the task completed with TaskUpdate, then reply DONE.'
same_sid
[ "$(sums)" = "$SUMS0" ] || bail "the session changed tests/ or checks/ — every gate below would grade a rewritten test"
tasks2="$(probe api GET "/sessions/$SID/work" | probe py "[t['subject'] for t in d['tasks'].get('$S2', [])]")"
case "$tasks2" in
    *"implement top"*) say "OK plan-tasks followed the running stage: stage 2 holds $tasks2" ;;
    *)                 echo "FAIL stage 2's task did not reach stage 2: $tasks2" ;;
esac
jstack-host plan verify "$S2" >/dev/null 2>&1 \
    && say "OK stage 2's tightened gate passes on the session's work" \
    || echo "FAIL stage 2's gate fails: $(last_proof "$P" 2 output | tail -3)"
out="$(jstack-host plan done "$S2" 2>&1)" && [ "$(sfield "$P" 2 status)" = "done" ] \
    && say "OK stage 2 closed on a proof filed against the gate it declares now" \
    || echo "FAIL stage 2 did not close: $out"

# Stage 2's gate re-runs stage 1's tests; put stage 1's stub back and it must
# go red. Run by hand, not through `plan verify`, so no proof is filed.
cp "$PROJ/demo/wordcount.py" "$HOME/wordcount.done"
cp "$FIXTURE/demo/wordcount.py" "$PROJ/demo/wordcount.py"
(cd "$PROJ" && sh checks/stage2.sh >/dev/null 2>&1) \
    && echo "FAIL stage 2's gate passes with stage 1 undone — it does not depend on stage 1" \
    || say "OK stage 2's gate fails with stage 1 undone — it depends on stage 1"
cp "$HOME/wordcount.done" "$PROJ/demo/wordcount.py"

echo "== what the Hub reports this session did =="
work="$(probe api GET "/sessions/$SID/work")"
printf '%s' "$work" | probe py '[d["mode"], d["plan"]["id"], [(s["ordinal"], s["status"]) for s in d["stages"]]]' | sed 's/^/  work: /'
[ "$(printf '%s' "$work" | probe py '[d["mode"], d["plan"]["id"], [s["status"] for s in d["stages"]]]')" \
  = "$(printf '["stages", "%s", ["done", "done"]]' "$P" | probe py 'd')" ] \
    && say "OK /work: this session's plan, both stages done" \
    || echo "FAIL /work does not report the plan this session finished"
printf '%s' "$work" | probe py "json.loads(next(s['env'] for s in d['stages'] if s['id'] == '$S1') or '{}').get('delivery_method', '')" | grep -qx "$V" \
    && say "OK stage 1's dispatch snapshot holds the session's delivery_method=$V" \
    || echo "FAIL stage 1's dispatch snapshot does not hold delivery_method=$V"
printf '%s' "$work" | probe py 'next(r for r in d["env"] if r["key"] == "delivery_method")' | sed 's/^/  env: /'
printf '%s' "$work" | probe py 'next(r for r in d["env"] if r["key"] == "delivery_method")["announced"]' | grep -qx true \
    && say "OK /work reports delivery_method announced — the host read env-announce's marker" \
    || echo "FAIL /work does not see the marker env-announce wrote"
detail="$(probe api GET "/plans/$P")"
printf '%s' "$detail" | probe py 'all(any(p["ok"] and p["created_at"] >= s["verify_set_at"] for p in d["proofs"][s["id"]]) for s in d["stages"])' | grep -qx true \
    && say "OK GET /plans/{id}: each stage holds a passing proof filed against its current gate" \
    || echo "FAIL GET /plans/{id} does not back both closes with a current passing proof"
probe api GET /plans | probe py "any(p['id'] == '$P' for p in d['plans'])" | grep -qx true \
    && say "OK GET /plans lists it" || echo "FAIL GET /plans does not list $P"

# What a plan-driven Claude session cannot show, said rather than skipped.
echo "GAP record-session-files: fires on this session's Edit/Write, and by design records only Codex apply_patch — on Claude it writes nothing to observe"
echo "GAP plan-mode-watch's Codex nudge and plan-tasks' update_plan branch: Codex paths; this journey drives Claude"

jstack-host plan show "$P" 2>&1 | sed 's/^/  show: /'
echo "$DONE"
EOF

p="$(guest_payload "$RECEIPTS/payload.sh")"
guest_term bash "$p"
for f in plan replan stage1 stage2; do guest_fetch "/Users/admin/$f.json"; done
finish_verdict "$RECEIPTS/term.log" DONE-PLUGIN-PLAN-GATE
