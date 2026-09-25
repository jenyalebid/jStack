#!/bin/bash
# WHAT: a real plan-mode session becomes stage rows, and the gate refuses the stage it gated
# TIME: ~18m
# GUEST: derived from $JSTACK_VERIFY_AUTHED_BASE (a guest with Claude Code signed in)
. "$(dirname "$0")/../../lib/common.sh"

guest_from "${JSTACK_VERIFY_AUTHED_BASE:-jstack-base-authed}" vfy-plugin-plan-gate

# The ref is baked in rather than read in the guest: `vm term` carries none of
# this shell's environment, and the work harness is not on the repo's default
# branch — a clone of `main` would run this whole journey against a machine
# that has no gate and report the gate working.
cat > "$RECEIPTS/payload.sh" <<EOF
#!/bin/bash
REF='${JSTACK_VERIFY_REF:-dev}'
EOF

cat >> "$RECEIPTS/payload.sh" <<'EOF'
set -u
say() { printf '%s\n' "$*"; }
# A bail still prints the sentinel: finish_verdict reads a missing marker as
# "never reached" and stops before it prints the FAIL lines, so a run that
# died for a nameable reason would arrive nameless.
bail() { echo "FAIL $*"; echo DONE-PLUGIN-PLAN-GATE; exit 0; }

# One store for both halves of the harness. The hooks resolve it through
# _env.host_environment(), the CLI through cli._adopt(), and an explicit export
# wins over both (adopt_installed_environment applies the plist BENEATH the
# shell) — left implicit, a guest that ever ran an installer reads a different
# store than the hooks write.
export JREMOTE_STATE_DIR="$HOME/.local/state/jremote"
# All three plan hooks share one kill switch. `claude` below inherits this
# shell's environment, so unsetting here is the whole guarantee: a journey run
# with either of these set proves the opposite of what it claims.
unset JSTACK_PLAN_GATE_DISABLED JSTACK_ENV_INJECT_DISABLED
export PATH="$HOME/.local/bin:$PATH"
PY="$(command -v python3.12 || command -v python3.11 || command -v python3)"

echo "== the checkout under test: ref $REF =="
# A base image that already carries a checkout gets moved onto the ref rather
# than left where it was: `[ -d ] || clone` would skip the clone and run the
# whole journey against whatever that image happened to hold.
if [ -d ~/jStack/.git ]; then
    git -C ~/jStack fetch -q --depth 1 origin "$REF" \
        && git -C ~/jStack checkout -q FETCH_HEAD
else
    git clone -q --depth 1 --branch "$REF" \
        https://github.com/jenyalebid/jStack.git ~/jStack
fi
[ -f ~/jStack/host/jstack_host/plans.py ] && [ -f ~/jStack/plugins/jstack/hooks/plan-exit.py ] \
    || bail "ref $REF carries no work harness — nothing here would be under test"
(cd ~/jStack && git log -1 --format='  head: %h %s')

echo "== install the plugin =="
claude plugin marketplace add ~/jStack >/dev/null 2>&1
claude plugin install jstack@jStack --config "agent_root=$HOME/Agents" >/dev/null 2>&1
claude plugin list 2>/dev/null | grep -q jstack || bail "plugin jstack@jStack not installed"
say "OK plugin installed"

# _env._host_importable() inserts <hook>/../../../host, so the hooks reach the
# writer through the repo they ship inside. plan-exit.py fails OPEN on its own
# exception: a plugin installed away from its host tree would let every plan
# through silently, which is the one failure this journey must not read as a
# pass. Diagnostic, not a verdict — the rows below are the verdict.
find "$HOME/.claude/plugins" "$HOME/jStack" -name plan-exit.py 2>/dev/null | while read -r h; do
    root="$(cd "$(dirname "$h")/../../.." 2>/dev/null && pwd)"
    if [ -n "$root" ] && [ -f "$root/host/jstack_host/plans.py" ]; then
        say "  note: hook $h reaches the writer at $root/host"
    else
        say "  note: hook $h has NO writer above it (root: ${root:-unresolved})"
    fi
done

echo "== the CLI the refusal tells a session to run =="
if ! command -v jstack-host >/dev/null 2>&1; then
    # cli.py imports `server` at module level, so the console script needs the
    # host's own dependencies — a --no-deps install yields a jstack-host that
    # cannot start. No LaunchAgent and no app: this is the package's CLI only.
    "$PY" -m venv "$HOME/.jstack-host-venv" >/dev/null 2>&1
    "$HOME/.jstack-host-venv/bin/pip" install -q ~/jStack/host 2>&1 | tail -3
    mkdir -p "$HOME/.local/bin"
    ln -sf "$HOME/.jstack-host-venv/bin/jstack-host" "$HOME/.local/bin/jstack-host"
fi
jstack-host plan list >/dev/null 2>&1 \
    || bail "jstack-host cannot read a plan store — the CLI half of the gate is absent"
say "OK jstack-host answers ($(command -v jstack-host))"

plan_json() { jstack-host plan show "$1" --json; }
sfield() {  # <plan-id> <ordinal> <field>
    plan_json "$1" | "$PY" -c 'import json,sys
d = json.load(sys.stdin)
o = int(sys.argv[1])
print(next((str(s.get(sys.argv[2], "")) for s in d["stages"] if s["ordinal"] == o), ""))' "$2" "$3"
}
scount() { plan_json "$1" | "$PY" -c 'import json,sys; print(len(json.load(sys.stdin)["stages"]))'; }
pstatus() { plan_json "$1" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["plan"]["status"])'; }
plan_ids() { jstack-host plan list --json \
    | "$PY" -c 'import json,sys; print(" ".join(p["id"] for p in json.load(sys.stdin)))'; }

cat > ~/plan-blocked.md <<'IN'
# Plan gate journey

## Stage 1 — name the deliverable
Verify: none
A stage that declares `none` closes on assertion. It is here so the refusal
below reads as this stage's gate and not as a plan nothing can close.

## Stage 2 — prove the gate refuses
Verify: command · false
This gate cannot pass. That is the point of it.
IN

# Same plan, one line moved: the reconcile keys on (plan_id, ordinal), so the
# titles must not drift or stage 2 would be compared against a different row.
sed 's/^Verify: command · false$/Verify: command · true/' ~/plan-blocked.md > ~/plan-fixed.md
grep -q 'command · true' ~/plan-fixed.md || bail "the corrected plan did not take the new gate"

prompt_for() {
    printf '%s\n\n%s\n' \
        "Call the ExitPlanMode tool now, and nothing else: do not read files, do not run commands, do not ask questions. Its plan argument must be exactly the markdown below, verbatim — every line, nothing added, removed or reworded." \
        "$(cat "$1")"
}

SID="$("$PY" -c 'import uuid; print(uuid.uuid4())')"
mkdir -p ~/plan-journey

echo "== a real plan-mode session authors the plan and approves it =="
(cd ~/plan-journey && claude --session-id "$SID" --permission-mode plan \
    --allowedTools ExitPlanMode -p "$(prompt_for ~/plan-blocked.md)" 2>&1) \
    | tail -15 | sed 's/^/  claude: /'

ids="$(plan_ids)"
set -- $ids
[ $# -eq 1 ] || bail "expected one plan row from that session, got $#: ${ids:-none}"
P="$1"
say "OK the plan-mode session minted one plan row ($P)"

n="$(scount "$P")"
[ "$n" = "2" ] || bail "plan show reports ${n:-0} stages, not 2 — ExitPlanMode never reached the writer, or the hook fell open"
say "OK plan show reports two stages"

k="$(sfield "$P" 2 verify_kind)"; s="$(sfield "$P" 2 verify_spec)"
if [ "$k" = "command" ] && [ "$s" = "false" ]; then
    say "OK stage 2 carries the gate its Verify: line declared (command · false)"
else
    bail "stage 2 declares '$k' · '$s', not command · false"
fi
if [ "$(pstatus "$P")" = "active" ]; then
    say "OK approval flipped the plan out of planning and into active"
else
    echo "FAIL plan status $(pstatus "$P") after approval, not active"
fi

S1="$(sfield "$P" 1 id)"; S2="$(sfield "$P" 2 id)"

echo "== the gate: closing stage 2 without a proof =="
# Taken first, the way a session works a stage. The status before the refused
# close is recorded rather than assumed: the assertion is that a refusal moves
# nothing, and pinning the literal 'running' here would fail on `plan start`
# rather than on the gate.
jstack-host plan start "$S2" >/dev/null 2>&1 || true
before="$(sfield "$P" 2 status)"
out="$(jstack-host plan done "$S2" 2>&1)"; rc=$?
say "  refusal: $out"
[ "$rc" = "0" ] && echo "FAIL plan done closed a stage that had no proof"
case "$out" in
    *"verify_kind='command'"*"has no passing proof"*"it stays open"*)
        say "OK the refusal names the gate, the missing proof and the consequence" ;;
    *)  echo "FAIL the refusal did not read as one — no gate/proof/consequence in it" ;;
esac
case "$out" in
    *"jstack-host plan verify $S2"*)
        say "OK the refusal hands back the one command that would satisfy it" ;;
    *)  echo "FAIL the refusal names no remedy its reader can run" ;;
esac
after="$(sfield "$P" 2 status)"
if [ "$after" = "$before" ] && [ "$after" != "done" ]; then
    say "OK stage 2 is still open ($after) — the refused close wrote nothing"
else
    echo "FAIL stage 2 went $before -> $after across a refused close"
fi

# The gate belongs to the stage that declared one. Without this, a writer that
# refused every close would read exactly like a working gate.
jstack-host plan done "$S1" >/dev/null 2>&1
if [ "$(sfield "$P" 1 status)" = "done" ]; then
    say "OK stage 1 (Verify: none) closes on assertion — the gate is the stage's, not the plan's"
else
    echo "FAIL stage 1 declared none and still did not close"
fi

jstack-host plan verify "$S2" >/dev/null 2>&1; rc=$?
[ "$rc" != "0" ] && say "OK plan verify ran \`false\` and reported the failure" \
    || echo "FAIL plan verify passed a stage gated on \`false\`"
out="$(jstack-host plan done "$S2" 2>&1)"; rc=$?
[ "$rc" != "0" ] && say "OK a FAILING proof on the record still does not close the stage" \
    || echo "FAIL a failing proof closed the stage"

# The cheap green receipt, filed by hand against the `false` gate. It is what
# the re-parse below has to retire; without it, step 5 would only prove that a
# stage with no proofs stays shut, which step 4 already proved.
jstack-host plan proof "$S2" --kind command --ok \
    --detail 'hand-filed against the false gate' >/dev/null 2>&1 \
    || echo "FAIL could not file a proof by hand"

echo "== the corrected gate, re-approved in the same session =="
(cd ~/plan-journey && claude --resume "$SID" --permission-mode plan \
    --allowedTools ExitPlanMode -p "$(prompt_for ~/plan-fixed.md)" 2>&1) \
    | tail -15 | sed 's/^/  claude: /'

ids="$(plan_ids)"
set -- $ids
[ $# -eq 1 ] \
    && say "OK the re-approval reconciled the session's own plan row, it did not mint a second" \
    || echo "FAIL re-approval left $# plan rows: ${ids:-none} — the reconcile needs this session's row"
[ "$(sfield "$P" 2 id)" = "$S2" ] \
    && say "OK stage 2 was updated in place — same row, keyed on (plan_id, ordinal)" \
    || echo "FAIL stage 2 is a different row after the re-parse"
[ "$(sfield "$P" 2 verify_spec)" = "true" ] \
    && say "OK stage 2's gate moved to command · true" \
    || echo "FAIL stage 2 still declares spec '$(sfield "$P" 2 verify_spec)'"

retired="$(plan_json "$P" | "$PY" -c 'import json,sys
d = json.load(sys.stdin)
s = next((x for x in d["stages"] if x["ordinal"] == 2), None)
green = [p for p in (s or {}).get("proofs", []) if p.get("ok")]
set_at = (s or {}).get("verify_set_at") or 0
print("yes" if green and max(p["created_at"] for p in green) < set_at else "no")')"
[ "$retired" = "yes" ] \
    && say "OK the hand-filed green proof now predates the declaration — verify_set_at retired it" \
    || echo "FAIL the proof filed against the old gate still answers for the new one"

out="$(jstack-host plan done "$S2" 2>&1)"; rc=$?
say "  refusal: $out"
if [ "$rc" = "0" ]; then
    echo "FAIL a retired proof closed the stage — the tightened gate closed on the cheap receipt"
else
    case "$out" in
        *"proves the gate this stage used to have"*)
            say "OK the refusal says which gate that green proof belongs to" ;;
        *)  echo "FAIL the refusal does not distinguish a retired proof from no proof" ;;
    esac
fi

echo "== the stage closes on evidence for what it declares now =="
jstack-host plan verify "$S2" >/dev/null 2>&1 \
    && say "OK plan verify passes against command · true" \
    || echo "FAIL plan verify did not pass against \`true\`"
out="$(jstack-host plan done "$S2" 2>&1)"; rc=$?
if [ "$rc" = "0" ] && [ "$(sfield "$P" 2 status)" = "done" ]; then
    say "OK stage 2 closed on its own passing proof, and plan show reports it done"
else
    echo "FAIL stage 2 did not close on a passing proof: $out"
fi

jstack-host plan show "$P" 2>&1 | sed 's/^/  show: /'
echo DONE-PLUGIN-PLAN-GATE
EOF

p="$(guest_payload "$RECEIPTS/payload.sh")"
guest_term bash "$p"
finish_verdict "$RECEIPTS/term.log" DONE-PLUGIN-PLAN-GATE
