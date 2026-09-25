#!/bin/bash
# USER: User sets how a session works
# WHAT: a setting POSTed to the Hub reaches a real session — session, agent, precedence, a mid-session flip, the markers
# TIME: ~25m
# GUEST: derived from $JSTACK_VERIFY_AUTHED_BASE, signed in with the lab token (#140)
#
# The environment is three typed settings the hooks inject as text. So the
# proof of "it reached the session" is the session saying back a line only the
# injected text holds — values drawn at random per run, so no reply can be
# right by habit — and each hook's own file on disk. Reading an env route back
# after writing to it proves only the store; no assertion here does that.
#
# The ref under test is baked in (JSTACK_VERIFY_REF, default dev): `vm term`
# carries none of this shell's environment. The positive half of
# `GET /sessions/{sid}/work` — a session that did plan work — is in
# plugin/plan-gate, the one journey whose session does any.
. "$(dirname "$0")/../../lib/common.sh"

guest_from "${JSTACK_VERIFY_AUTHED_BASE:-jstack-base-authed}" vfy-hub-work-env

LIB="$(dirname "$0")/../../lib"
vm cp "$GUEST" "$LIB/work_probe.py" /Users/admin/work_probe.py >/dev/null
vm cp "$GUEST" "$LIB/work_guest.sh" /Users/admin/work_guest.sh >/dev/null
guest_signin

cat > "$RECEIPTS/payload.sh" <<EOF
#!/bin/bash
REF='${JSTACK_VERIFY_REF:-dev}'
EOF

cat >> "$RECEIPTS/payload.sh" <<'EOF'
DONE=DONE-HUB-WORK-ENV
. /Users/admin/work_guest.sh

hub_at_ref
hook_host_check

# The layout agents actually run in: a chat seat under the agent. It is what
# makes /agents serve the scoped id `alpha-chat` while every reader of the
# agent layer asks for `alpha` — the shape an agent write once went missing in.
CHAT="$SEAT/chat"
mkdir -p "$CHAT" && printf '# Alpha · chat\n' > "$CHAT/CLAUDE.md"
AGENT="$(probe api GET /agents | probe py 'next(a["agent_id"] for a in d["agents"] if a.get("base") == "alpha")' 2>/dev/null)"
[ "$AGENT" = "alpha-chat" ] \
    || bail "GET /agents serves Alpha as '${AGENT:-nothing}', not alpha-chat — the scoped-id write this journey exists for is not the one it would make"
say "OK /agents serves Alpha as '$AGENT' — the id every agent write below uses"

# Three distinct non-default delivery methods, drawn per run.
read -r VS VA VA2 < <(python3 -c 'import random; v = ["distribute", "testflight", "build", "sim_demo"]; random.shuffle(v); print(*v[:3])')
say "  this run: session=$VS agent=$VA agent-after-flip=$VA2"

ENV_ASK='Before this message you may have been given a line that begins with "SESSION ENVIRONMENT:". Reply with that line copied exactly, character for character, and nothing else. If you were given no such line, reply with exactly NO-ENVIRONMENT-LINE.'
DELTA_ASK='Just before this message you may have been given a line saying a setting SWITCHED or TURNED. Reply with that line copied exactly, character for character, and nothing else. If there was no such line, reply with exactly NO-CHANGE-LINE.'
uuid() { uuidgen | tr 'A-Z' 'a-z'; }
set_env() {  # set_env sessions|agents <id> <key> <value>
    probe api POST "/$1/$2/env" "{\"key\": \"$3\", \"value\": \"$4\"}" >/dev/null \
        || bail "POST /$1/$2/env $3=$4 was refused"
}
# The engine ran under the id this script keyed everything on, or nothing
# below is about this session.
same_sid() { [ "$R_SID" = "$1" ] || bail "the engine ran as '${R_SID:-nothing}', not $1 — every row and marker below would be under an id this script is not reading"; }
says() {  # says <label> <want>
    if [ "$(norm "$R_TEXT")" = "$2" ]; then say "OK $1: the session said '$2'"
    else echo "FAIL $1: wanted '$2', the session said '$(norm "$R_TEXT" | head -3 | tr '\n' ' ')'"; fi
}

echo "== control: nothing set, nothing said =="
# A model asked for a line it was never given must say so. Without this, the
# echoes below could be the model inventing plausible settings.
S0="$(uuid)"
run_claude control "$CHAT" --session-id "$S0" -p "$ENV_ASK"
same_sid "$S0"
says "untouched environment" "NO-ENVIRONMENT-LINE"
# env-delta writes its snapshot on every prompt, moved or not, so its absence
# is the hook not running — and "{}" is the only right content here.
[ "$(cat "$MARKS/$S0/environment.json" 2>/dev/null)" = "{}" ] \
    && say "OK env-delta ran and snapshotted nothing moved" \
    || echo "FAIL env-delta's snapshot for an untouched session is '$(cat "$MARKS/$S0/environment.json" 2>&1)'"

echo "== the session layer =="
SA="$(uuid)"
set_env sessions "$SA" delivery_method "$VS"
run_claude session "$CHAT" --session-id "$SA" -p "$ENV_ASK"
same_sid "$SA"
says "a session value reached the session (env-entry)" "SESSION ENVIRONMENT: delivery_method=$VS"
# delivery_method's registry trigger is Stop, so the turn's own end must have
# left the value-keyed marker behind.
[ -f "$MARKS/$SA/env-delivery_method-$VS.marker" ] \
    && say "OK env-announce wrote env-delivery_method-$VS.marker at the session's Stop" \
    || echo "FAIL no env-delivery_method-$VS.marker under $MARKS/$SA: $(ls "$MARKS/$SA" 2>&1 | tr '\n' ' ')"

echo "== the agent layer, under the id /agents serves =="
set_env agents "$AGENT" delivery_method "$VA"
set_env agents "$AGENT" sim_verify off
SB="$(uuid)"
run_claude agent "$CHAT" --session-id "$SB" -p "$ENV_ASK"
same_sid "$SB"
# Registry order, non-defaults only, joined with " · " — environment.state_line.
says "an agent value reached a session that set nothing" "SESSION ENVIRONMENT: delivery_method=$VA · sim_verify=off"

echo "== precedence: session over agent, per setting =="
# The rule is environment.resolve's: each key walks session -> agent ->
# default on its own. So the session's delivery_method shadows the agent's
# while the agent's sim_verify still stands — a per-layer rule would drop it.
SC="$(uuid)"
set_env sessions "$SC" delivery_method "$VS"
run_claude precedence "$CHAT" --session-id "$SC" -p "$ENV_ASK"
same_sid "$SC"
says "the session's value won and the agent's other value stood" "SESSION ENVIRONMENT: delivery_method=$VS · sim_verify=off"

echo "== a running session hears its agent's value move =="
# SB set nothing of its own, so the flip is the agent layer's. env-delta
# resolves the agent from the session's cwd like entry does; resolved by the
# session row alone, a session the index had not reached yet heard nothing.
set_env agents "$AGENT" delivery_method "$VA2"
run_claude agent-flip "$CHAT" --resume "$SB" -p "$DELTA_ASK"
same_sid "$SB"
says "env-delta said the flip" "DELIVERY METHOD SWITCHED: $(upper "$VA2")"
snap="$(cat "$MARKS/$SB/environment.json" 2>/dev/null)"
[ "$(printf '%s' "$snap" | probe py 'd.get("delivery_method", "")' 2>/dev/null)" = "$VA2" ] \
    && say "OK env-delta's snapshot moved to $VA2 ($snap)" \
    || echo "FAIL env-delta's snapshot does not hold the flip: ${snap:-absent}"

# The same question to a session nothing moved under: a model that answers the
# flip line regardless would pass the check above without any hook.
run_claude still "$CHAT" --resume "$SC" -p "$DELTA_ASK"
same_sid "$SC"
says "no flip, no line" "NO-CHANGE-LINE"

echo "== the markers, and the host reading them =="
for v in "$VA" "$VA2"; do
    [ -f "$MARKS/$SB/env-delivery_method-$v.marker" ] \
        && say "OK env-announce spoke delivery_method=$v once and marked it" \
        || echo "FAIL no marker for delivery_method=$v — the value in force at a Stop was not announced"
done
# sim_verify=off is in force for SB too, but its triggers are xcodebuild and
# .swift edits and this session did neither: a marker here is announce firing
# off its trigger.
[ ! -e "$MARKS/$SB/env-sim_verify-off.marker" ] \
    && say "OK sim_verify=off was not announced — none of its triggers happened" \
    || echo "FAIL sim_verify=off was announced with no xcodebuild and no .swift edit"
envrows="$(probe api GET "/sessions/$SB/env")"
ann="$(printf '%s' "$envrows" | probe py '{r["key"]: [r["value"], r["source"], r["announced"]] for r in d["env"]}')"
say "  GET /sessions/$SB/env: $ann"
printf '%s' "$envrows" | probe py 'next(r for r in d["env"] if r["key"] == "delivery_method")["announced"]' | grep -qx true \
    && say "OK the host reports delivery_method announced — read off the hook's marker, not off a POST" \
    || echo "FAIL the host does not see the marker env-announce wrote"

echo "== the Work view for a session that did no plan work =="
work="$(probe api GET "/sessions/$SB/work")"
[ "$(printf '%s' "$work" | probe py 'd["mode"]')" = "none" ] \
    && say "OK /sessions/{sid}/work says mode none — this session opened no plan" \
    || echo "FAIL /work reports plan work this session never did: $(printf '%s' "$work" | head -c 300)"
[ "$(printf '%s' "$work" | probe py 'sorted((r["key"], r["value"], r["source"]) for r in d["env"])')" \
  = "$(printf '%s' "$envrows" | probe py 'sorted((r["key"], r["value"], r["source"]) for r in d["env"])')" ] \
    && say "OK the Work view carries the same environment the env route serves" \
    || echo "FAIL the Work view's environment disagrees with /sessions/{sid}/env"

echo "$DONE"
EOF

p="$(guest_payload "$RECEIPTS/payload.sh")"
guest_term bash "$p"
for f in control session agent precedence agent-flip still; do guest_fetch "/Users/admin/$f.json"; done
finish_verdict "$RECEIPTS/term.log" DONE-HUB-WORK-ENV
