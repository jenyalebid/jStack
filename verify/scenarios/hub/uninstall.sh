#!/bin/bash
# WHAT: README uninstall takes services and app off but keeps state, token and root
# TIME: ~12m
# GUEST: pristine
. "$(dirname "$0")/../../lib/common.sh"

guest_fresh vfy-hub-uninstall

cat > "$RECEIPTS/payload.sh" <<'EOF'
#!/bin/bash
set -u
export JSTACK_ROOT=/Users/admin/Desktop/Alpine
mkdir -p "$JSTACK_ROOT"

echo "== install first =="
curl -fsSL https://raw.githubusercontent.com/jenyalebid/jStack/main/install.sh \
    | bash -s -- --agent Jarvis
state_before="$(ls ~/.local/state/jremote 2>/dev/null | wc -l | tr -d ' ')"

echo "== README uninstall (keep state) =="
curl -fsSL https://raw.githubusercontent.com/jenyalebid/jStack/main/install.sh \
    | bash -s -- --uninstall --keep-state

echo "== verdict =="
launchctl list | grep -qi jstack && echo "FAIL a jstack launchd job survived" \
    || echo "OK no jstack launchd jobs left"
pgrep -f JStackHostBar >/dev/null && echo "FAIL menu bar still running" || echo "OK menu bar gone"
[ -d "/Applications/jStack Hub.app" ] && echo "FAIL Hub.app still installed" || echo "OK Hub.app gone"
[ -d "$JSTACK_ROOT" ] && echo "OK root tree kept ($JSTACK_ROOT)" || echo "FAIL root tree deleted"
state_after="$(ls ~/.local/state/jremote 2>/dev/null | wc -l | tr -d ' ')"
[ "$state_after" = "$state_before" ] && echo "OK host state kept ($state_after entries)" \
    || echo "FAIL host state changed ($state_before -> $state_after)"
echo DONE-HUB-UNINSTALL
EOF

p="$(guest_payload "$RECEIPTS/payload.sh")"
guest_term bash "$p"
finish_verdict "$RECEIPTS/term.log" DONE-HUB-UNINSTALL
