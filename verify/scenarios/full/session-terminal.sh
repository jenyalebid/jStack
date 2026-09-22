#!/bin/bash
# WHAT: open a real session and read the terminal the APP renders — it must show a live shell, never flood "can't find terminfo database"
# TIME: ~12m
# GUEST: pristine
#
# The 2026-09-22 screenshot: a jRemote session window filling with
# "can't find terminfo database" over and over, stuck on "Reconnecting…".
# /api/health returns 200 straight through that — which is why an API probe is
# a lie here. This scenario reads the one surface that tells the truth: the PTY
# WebSocket the app attaches to. It spawns a session with the product's own
# spawner, attaches that socket exactly as the app does (same endpoint, same
# bearer, same server-side _spawn_attach), and fails if the bytes the user
# would see carry the terminfo flood or no live terminal at all.
. "$(dirname "$0")/../../lib/common.sh"

guest_fresh vfy-full-session-terminal

# The app-faithful PTY reader rides along into the guest.
vm cp "$GUEST" "$(dirname "$0")/../../lib/ws_read.py" /Users/admin/ws_read.py >/dev/null

cat > "$RECEIPTS/payload.sh" <<'EOF'
#!/bin/bash
set -u
export JSTACK_ROOT=/Users/admin/Desktop/Alpine
mkdir -p "$JSTACK_ROOT"
H="$HOME/.local/bin/jstack-host"
PKG="/Applications/jStack Hub.app/Contents/Resources/packages"
PY="/Applications/jStack Hub.app/Contents/MacOS/JStackPython"

echo "== README install, from the CDN =="
curl -fsSL https://raw.githubusercontent.com/jenyalebid/jStack/main/install.sh \
    | bash -s -- --agent Jarvis
sleep 5

TOKEN="$("$H" token 2>/dev/null)"
[ -n "$TOKEN" ] && echo "OK got host bearer token" || echo "FAIL no host token"

echo "== spawn a REAL managed session (the product's own spawner) =="
SID="$(uuidgen | tr 'A-Z' 'a-z')"
CWD="$JSTACK_ROOT/Agents/Jarvis"
SP="$(PYTHONPATH="$PKG" "$PY" -m jstack_host.spawn --cwd "$CWD" --sid "$SID" 2>&1)"
echo "$SP" | sed 's/^/  spawn: /'
if echo "$SP" | grep -q "$SID"; then echo "OK managed session $SID came up alive"; else echo "FAIL spawner never brought a session up"; fi

echo "== read the PTY WebSocket — the exact stream the app paints =="
"$PY" /Users/admin/ws_read.py "$SID" "$TOKEN" 6 > /tmp/pty.out 2>/tmp/pty.err || true
echo "  --- first bytes the terminal showed ---"
head -c 800 /tmp/pty.out | tr -d '\000' | sed 's/^/  pty| /'
[ -s /tmp/pty.err ] && sed 's/^/  pty-err: /' /tmp/pty.err

echo "== verdict: the terminal the user sees =="
if grep -qa "can't find terminfo database" /tmp/pty.out; then
    echo "FAIL the terminal floods 'can't find terminfo database' — the screenshot bug"
else
    echo "OK no terminfo flood in the terminal stream"
fi
if [ -s /tmp/pty.out ]; then
    echo "OK the PTY stream delivered bytes (a live terminal, not a blank reconnect)"
else
    echo "FAIL the PTY stream was empty — the app would sit on 'Reconnecting…'"
fi
# A live session shows tmux/shell or the agent CLI — some printable content
if LC_ALL=C grep -qa '[[:print:]]' /tmp/pty.out && ! grep -qa "can't find terminfo" /tmp/pty.out; then
    echo "OK the terminal rendered real content"
fi
echo DONE-SESSION-TERMINAL
EOF

p="$(guest_payload "$RECEIPTS/payload.sh")"
guest_term bash "$p"
finish_verdict "$RECEIPTS/term.log" DONE-SESSION-TERMINAL
