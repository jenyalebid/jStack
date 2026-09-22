#!/bin/bash
# WHAT: plugin alone — marketplace add + install in Claude Code, no Hub, no app, no services
# TIME: ~8m
# GUEST: derived from $JSTACK_VERIFY_AUTHED_BASE (a guest with Claude Code signed in)
. "$(dirname "$0")/../../lib/common.sh"

guest_from "${JSTACK_VERIFY_AUTHED_BASE:-jstack-base-authed}" vfy-plugin-install

cat > "$RECEIPTS/payload.sh" <<'EOF'
#!/bin/bash
set -u
echo "== the independent plugin path: a checkout and two claude commands =="
[ -d ~/jStack ] || git clone --depth 1 https://github.com/jenyalebid/jStack.git ~/jStack
claude plugin marketplace add ~/jStack 2>&1 | tail -2
claude plugin install jstack@jStack --config "agent_root=$HOME/Agents" 2>&1 | tail -2

echo "== verdict =="
claude plugin list 2>/dev/null | grep -q jstack \
    && echo "OK plugin jstack@jStack installed" || echo "FAIL plugin not installed"
ls ~/jStack/plugins/jstack/skills >/dev/null 2>&1 \
    && echo "OK skills present in the checkout" || echo "FAIL no skills"
# Alone means alone: nothing but the plugin may exist after this path.
[ -d "/Applications/jStack Hub.app" ] && echo "FAIL a Hub appeared" || echo "OK no Hub installed"
[ -d "/Applications/jRemote.app" ] && echo "FAIL a client appeared" || echo "OK no client installed"
launchctl list | grep -qi jstack && echo "FAIL a jstack service appeared" || echo "OK no services registered"
echo DONE-PLUGIN-INSTALL
EOF

p="$(guest_payload "$RECEIPTS/payload.sh")"
guest_term bash "$p"
finish_verdict "$RECEIPTS/term.log" DONE-PLUGIN-INSTALL
