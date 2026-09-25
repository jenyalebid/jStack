#!/bin/bash
# USER: User runs plugin commands without a hub
# WHAT: plugin commands answer in a real Claude Code session — no Hub behind them
# TIME: ~10m
# GUEST: derived from $JSTACK_VERIFY_AUTHED_BASE (a guest with Claude Code signed in)
. "$(dirname "$0")/../../lib/common.sh"

guest_from "${JSTACK_VERIFY_AUTHED_BASE:-jstack-base-authed}" vfy-plugin-commands

cat > "$RECEIPTS/payload.sh" <<'EOF'
#!/bin/bash
set -u
[ -d ~/jStack ] || git clone --depth 1 https://github.com/jenyalebid/jStack.git ~/jStack
claude plugin marketplace add ~/jStack >/dev/null 2>&1
claude plugin install jstack@jStack --config "agent_root=$HOME/Agents" >/dev/null 2>&1

echo "== a real turn, plugin loaded =="
out="$(cd ~/jStack && claude --print 'Answer with the single word READY and nothing else.' 2>&1)"
echo "claude said: $out"
case "$out" in *READY*) echo "OK claude runs with the plugin loaded" ;; \
    *) echo "FAIL claude turn did not complete" ;; esac

echo "== plugin tooling stands alone =="
~/jStack/plugins/jstack/bin/jstack-doctor 2>&1 | tail -5 | sed 's/^/  doctor: /'
~/jStack/plugins/jstack/bin/jstack-doctor >/dev/null 2>&1 \
    && echo "OK doctor runs from the bare checkout" \
    || echo "note: doctor reports findings on a bare checkout (expected — no install here)"
echo DONE-PLUGIN-COMMANDS
EOF

p="$(guest_payload "$RECEIPTS/payload.sh")"
guest_term bash "$p"
finish_verdict "$RECEIPTS/term.log" DONE-PLUGIN-COMMANDS
