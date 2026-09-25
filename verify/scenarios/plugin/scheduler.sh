#!/bin/bash
# USER: User runs the plugin scheduler without a hub
# WHAT: the plugin's scheduler becomes a healthy launchd service on a machine with no Hub
# TIME: ~8m
# GUEST: derived from $JSTACK_VERIFY_AUTHED_BASE (has git and a modern python; still no Hub)
. "$(dirname "$0")/../../lib/common.sh"

guest_from "${JSTACK_VERIFY_AUTHED_BASE:-jstack-base-authed}" vfy-plugin-scheduler

cat > "$RECEIPTS/payload.sh" <<'EOF'
#!/bin/bash
set -u
export JSTACK_ROOT=/Users/admin/Desktop/Alpine
mkdir -p "$JSTACK_ROOT/Agents/Jarvis"
printf '# Jarvis\n' > "$JSTACK_ROOT/Agents/Jarvis/CLAUDE.md"
[ -d ~/jStack ] || git clone --depth 1 https://github.com/jenyalebid/jStack.git ~/jStack

PY="$(command -v python3.12 || command -v python3.11 || command -v python3)"
echo "== install the scheduler service (python: $PY) =="
"$PY" ~/jStack/plugins/jstack/bin/jstack-scheduler install --python "$PY"

echo "== verdict =="
sleep 5
"$PY" ~/jStack/plugins/jstack/bin/jstack-scheduler status \
    && echo "OK scheduler service healthy" || echo "FAIL scheduler status exit $?"
grep -q "Library/Logs/jstack-scheduler" ~/Library/LaunchAgents/com.jstack.scheduler.plist 2>/dev/null \
    && echo "OK plist uses safe log paths" || echo "FAIL plist points logs into the root"
curl -s -m 5 http://127.0.0.1:9091/ >/dev/null 2>&1 \
    && echo "OK scheduler api answering on 9091" || echo "FAIL scheduler api not answering"
echo DONE-PLUGIN-SCHEDULER
EOF

p="$(guest_payload "$RECEIPTS/payload.sh")"
guest_term bash "$p"
finish_verdict "$RECEIPTS/term.log" DONE-PLUGIN-SCHEDULER
