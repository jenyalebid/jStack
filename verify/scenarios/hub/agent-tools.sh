#!/bin/bash
# USER: User's agents reach every tool on a fresh install
# WHAT: installed Mac, nothing hand-wired — every agent tool fires for a session that configured nothing
# TIME: ~14m
# GUEST: derived from $JSTACK_VERIFY_AUTHED_BASE (a guest with Claude Code signed in)
#
# The agent tools — the shared pad, the compaction carry, the memory ceiling, the
# waiting dot, the load meter, the turn budget, the delivery compaction — used to
# be wired by hand in one machine's settings.json against scripts in a tree no
# install carried. Every other machine ran agents with none of them and nothing
# said so.
#
# So this scenario asks the question that hand-wiring can never answer: on a Mac
# that ran the install and nothing else, do they fire? Three legs:
#   1. the manifest the engine reads declares every one of them, and the machine's
#      own settings.json declares NONE of them. The stack is the configuration.
#   2. each hook's decision contract holds on the INSTALLED copy — the shipped
#      contract tests, run in the guest against the files the installer put there.
#   3. a REAL Claude Code session, started by the engine in the seat, leaves the
#      effects behind: its private scratchpad is the seat's pad, the Hub's state
#      directory holds the turn marker, the Stop hook logged its decision, and a
#      PreToolUse denial actually stopped a tool call.
#
# Leg 3 is the one that cannot be faked: no hook here is invoked by this script.
. "$(dirname "$0")/../../lib/common.sh"

guest_from "${JSTACK_VERIFY_AUTHED_BASE:-jstack-base-authed}" vfy-hub-agent-tools

cat > "$RECEIPTS/payload.sh" <<'EOF'
#!/bin/bash
set -u
export JSTACK_ROOT=/Users/admin/Desktop/Alpine
mkdir -p "$JSTACK_ROOT"
SEAT="$JSTACK_ROOT/Agents/Alpha"

echo "== install, from the CDN, one flag =="
curl -fsSL https://raw.githubusercontent.com/jenyalebid/jStack/main/install.sh \
    | bash -s -- --yes --agent Alpha 2>&1 | tail -25

PLUGIN=~/jStack/plugins/jstack
TOOLS="session-start-pad precompact-carry memory-ceiling attention context-ceiling turn-budget stop-compact-delivery"

echo "== 1. the stack is the configuration =="
for t in $TOOLS; do
    if grep -q "hooks/$t" "$PLUGIN/hooks/hooks.json" 2>/dev/null; then
        echo "OK manifest declares $t"
    else
        echo "FAIL manifest does not declare $t"
    fi
done
# The whole point: nothing on this machine was wired by a person.
hand=0
for t in $TOOLS; do
    if grep -q "$t" ~/.claude/settings.json 2>/dev/null; then
        echo "FAIL $t is hand-wired in settings.json"; hand=1
    fi
done
[ "$hand" = 0 ] && echo "OK no agent tool is hand-wired on this machine"
# The plugin has to be the copy the engine loads, not just a checkout on disk.
claude plugin list 2>/dev/null | grep -q jstack \
    && echo "OK the engine loads the jstack plugin" || echo "FAIL plugin not loaded"

echo "== 2. the decision contracts, on the installed copy =="
for t in attention memory-ceiling precompact-carry context-ceiling turn-budget session-start-pad; do
    if [ ! -x "$PLUGIN/tests/$t.sh" ]; then
        echo "FAIL $t.sh was not installed"; continue
    fi
    out=$(bash "$PLUGIN/tests/$t.sh" 2>&1)
    if printf '%s' "$out" | grep -q "^$t: all pass"; then
        echo "OK $t contract holds on the installed copy"
    else
        echo "FAIL $t contract broken on the installed copy"
        printf '%s\n' "$out" | grep '^FAIL' | sed 's/^/     /'
    fi
done

echo "== 3. a real session, started by the engine =="
STATE=$(python3 -c 'import json,pathlib;print(json.load(open(pathlib.Path.home()/".local/state/jremote/embedded.json"))["state_dir"])' 2>/dev/null)
[ -n "$STATE" ] && echo "OK the Hub declares its state directory: $STATE" \
                || echo "FAIL no embed marker — nothing can find the Hub's state"

# The turn budget, driven to zero, so the very first tool call of an unattended
# run is refused. A hook that cannot stop a tool call is decoration.
mkdir -p ~/.claude/jstack
echo '{"default": [0, 0]}' > ~/.claude/jstack/turn-budgets.json
cd "$SEAT" || exit 1
claude -p 'Use the Bash tool to run: ls /. Then stop.' \
    --dangerously-skip-permissions > /Users/admin/budget-run.txt 2>&1
rm -f ~/.claude/jstack/turn-budgets.json
if grep -q "TURN BUDGET EXCEEDED" /Users/admin/budget-run.txt; then
    echo "OK the turn budget refused a real tool call"
elif grep -qi "turn budget" /Users/admin/budget-run.txt; then
    echo "OK the turn budget spoke into a real session"
else
    echo "FAIL the turn budget never reached a real session"
    tail -5 /Users/admin/budget-run.txt | sed 's/^/     /'
fi

# A second session with no budget in the way, for the effects the others leave.
claude -p 'Reply with the single word: ready.' \
    --dangerously-skip-permissions > /Users/admin/plain-run.txt 2>&1
sleep 12   # the Stop hook's decision is made by a detached child

# The pad: the private directory the engine hardcoded into its own prompt is now
# the seat's shared folder, so a session cannot put output where nobody can see it.
link=$(find "/private/tmp/claude-$(id -u)" -maxdepth 3 -name scratchpad -type l 2>/dev/null | head -1)
if [ -n "$link" ] && [ "$(readlink "$link")" = "$SEAT/pad" ]; then
    echo "OK a real session's scratchpad is the seat's pad ($link)"
else
    echo "FAIL no real session's scratchpad was redirected to $SEAT/pad"
    find "/private/tmp/claude-$(id -u)" -maxdepth 3 -name scratchpad 2>/dev/null | sed 's/^/     /'
fi

# The waiting dot's plumbing: the turn clock wrote into the directory the embed
# marker declared, which is the only way a hook can know where the Hub serves from.
if [ -d "$STATE/jremote_turn" ]; then
    echo "OK the attention hook wrote into the Hub's declared state directory"
else
    echo "FAIL nothing was written to $STATE/jremote_turn"
fi

# Delivery compaction: the Stop hook logged a decision for the session that just ended.
LOG="$STATE/compact-delivery.jsonl"
if [ -s "$LOG" ]; then
    echo "OK the Stop hook logged its decision ($(wc -l < "$LOG" | tr -d ' ') rows)"
    tail -2 "$LOG" | sed 's/^/     /'
else
    echo "FAIL no delivery-compaction decision was logged at $LOG"
fi

echo DONE-HUB-AGENT-TOOLS
EOF

p="$(guest_payload "$RECEIPTS/payload.sh")"
guest_term bash "$p"
guest_fetch /Users/admin/budget-run.txt
guest_fetch /Users/admin/plain-run.txt
finish_verdict "$RECEIPTS/term.log" DONE-HUB-AGENT-TOOLS
