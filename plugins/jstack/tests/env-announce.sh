#!/usr/bin/env bash
# jStack live test — the session environment's three moments.
#
# Drives the three real hook scripts with the real JSON stdin contract against
# a store built for this run, and pins the one property the mechanism lives or
# dies by: SILENCE. A default says nothing at entry, an unchanged environment
# says nothing on a prompt, and a value already announced says nothing on the
# hundredth matching tool call. Everything else here is a way of proving that
# the silence is not simply the hooks failing shut — every hook exits 0 and
# prints nothing when it cannot read its store, so a broken import would make
# most of these assertions pass for the wrong reason. `precheck` below exists
# for exactly that, and it must stay first.
#
# ORDINALS ARE THE CONTRACT IN hooks.json. Codex keys hook trust by position —
# "…:hooks.json:<event>:<group>:<hook>" with a persisted hash — so inserting a
# group or a hook ABOVE an existing one shifts every later ordinal and silently
# stops those hooks running: no error, no warning, exit 0. New hooks go at the
# END of an existing group's array; new groups go at the END of the event's.
# Never insert, never reorder. This test asserts each env hook is last in its
# group, because that is the edit a future change will get wrong.
#
# Exit 0 = all pass. Exit 1 = any fail. Hermetic: its own store, its own
# markers, no ~/.claude writes, nothing of the machine's touched.

set -u

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO_ROOT="$(cd "$PLUGIN_ROOT/../.." && pwd)"
HOST="$REPO_ROOT/host"
ENTRY="$PLUGIN_ROOT/hooks/env-entry.py"
DELTA="$PLUGIN_ROOT/hooks/env-delta.py"
ANNOUNCE="$PLUGIN_ROOT/hooks/env-announce.py"

# The host package must be importable by whatever runs the hooks; a venv with
# it installed is named here instead of the PATH python.
PY="${JSTACK_TEST_PYTHON:-python3}"

for f in "$ENTRY" "$DELTA" "$ANNOUNCE"; do
  [[ -f "$f" ]] || { echo "FAIL: hook not found at $f" >&2; exit 1; }
done
command -v "$PY" >/dev/null 2>&1 || { echo "FAIL: $PY not runnable" >&2; exit 1; }

TMP="$(mktemp -d "${TMPDIR:-/tmp}/jstack-envtest.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

STATE="$TMP/state"
CACHE="$TMP/cache"
TRANSCRIPT="$TMP/transcript.jsonl"
mkdir -p "$STATE" "$CACHE"
echo '{"role":"user","content":"seed"}' > "$TRANSCRIPT"

export JREMOTE_STATE_DIR="$STATE"
export JSTACK_CACHE_ROOT="$CACHE"
# Far above anything this test writes: re-injection on transcript growth is the
# path-rule mechanism and is covered there. Here it must never fire by accident,
# or "stays silent" would be untestable.
export JSTACK_RULE_REINJECT_BYTES=10000000

fail() { echo "FAIL [$1]: $2" >&2; exit 1; }
pass() { echo "PASS [$1]"; }

precheck() {
  local out
  out="$("$PY" - "$HOST" <<'EOF' 2>&1
import sys
sys.path.insert(0, sys.argv[1])
from jstack_host import environment as env
assert env.announce("sim_verify", "off"), "registry has no sentence for sim_verify=off"
print("ok")
EOF
)"
  [[ "$out" == "ok" ]] || fail "precheck" "host not importable by $PY — every
  silence below would be a lie: $out"
  pass "precheck"
}

seed() {  # seed <session_id> <key> <value|-->
  "$PY" - "$HOST" "$1" "$2" "$3" <<'EOF' || fail "seed" "store write failed"
import sys
sys.path.insert(0, sys.argv[1])
from jstack_host import environment as env
env.set_value(sys.argv[3], None if sys.argv[4] == "--" else sys.argv[4],
              session_id=sys.argv[2])
EOF
}

# The injected text, or "" when the hook said nothing. A hook that printed
# something other than the context envelope is a failure, not an empty string.
ctx() {
  local raw="$1"
  [[ -z "$raw" ]] && { printf ''; return; }
  printf '%s' "$raw" | "$PY" -c '
import json, sys
d = json.load(sys.stdin)
sys.stdout.write(d["hookSpecificOutput"]["additionalContext"])
' || fail "ctx" "hook output is not a context envelope: $raw"
}

entry()  { printf '{"hook_event_name":"SessionStart","session_id":"%s","source":"startup","transcript_path":"%s"}' "$1" "$TRANSCRIPT" | "$PY" "$ENTRY"; }
prompt() { printf '{"hook_event_name":"UserPromptSubmit","session_id":"%s","prompt":"go on","transcript_path":"%s"}' "$1" "$TRANSCRIPT" | "$PY" "$DELTA"; }
edited() { printf '{"hook_event_name":"PostToolUse","session_id":"%s","tool_name":"Edit","tool_input":{"file_path":"%s"},"transcript_path":"%s"}' "$1" "$2" "$TRANSCRIPT" | "$PY" "$ANNOUNCE"; }
ran()    { printf '{"hook_event_name":"PreToolUse","session_id":"%s","tool_name":"Bash","tool_input":{"command":"%s"},"transcript_path":"%s"}' "$1" "$2" "$TRANSCRIPT" | "$PY" "$ANNOUNCE"; }
stopped(){ printf '{"hook_event_name":"Stop","session_id":"%s","transcript_path":"%s"}' "$1" "$TRANSCRIPT" | "$PY" "$ANNOUNCE"; }

precheck

# (1) An untouched environment is silent at all three moments. Nothing is
#     seeded for this session at all — the state a machine nobody configured
#     is in, and the one this mechanism may never degrade.
S=quiet-$$
for probe in "$(entry $S)" "$(prompt $S)" "$(prompt $S)" \
             "$(edited $S /work/ui/View.swift)" "$(ran $S 'xcodebuild -scheme App')" \
             "$(stopped $S)"; do
  [[ -z "$probe" ]] || fail "default-silence" "expected no output, got: $probe"
done
pass "default-silence"

# (2) Entry carries the moved values and only those, in one labelled block.
S=entry-$$
seed "$S" sim_verify off
seed "$S" use_subagents on
got="$(ctx "$(entry $S)")"
want='<jstack-environment>
SESSION ENVIRONMENT: sim_verify=off · use_subagents=on
</jstack-environment>'
[[ "$got" == "$want" ]] || fail "entry-block" "got:
$got
want:
$want"
[[ "$got" != *delivery_method* ]] || fail "entry-block" "a default was announced"
pass "entry-block"

# (3) A flip between prompts is one line; the prompt after it is silent. The
#     first prompt of a session never deltas — entry has just said it.
S=delta-$$
[[ -z "$(prompt $S)" ]] || fail "delta-first-prompt" "the first prompt deltaed"
seed "$S" sim_verify off
got="$(ctx "$(prompt $S)")"
[[ "$got" == "SIM VERIFY TURNED OFF" ]] || fail "delta-flip" "got: $got"
[[ -z "$(prompt $S)" ]] || fail "delta-settled" "an unchanged prompt spoke"
pass "delta-one-line"

# (4) Announced once, then silent however long the session runs.
S=once-$$
seed "$S" sim_verify off
got="$(ctx "$(edited $S /work/ui/View.swift)")"
[[ "$got" == "SESSION ENVIRONMENT — sim_verify=off: "* ]] || fail "announce-once" "got: $got"
[[ "$(printf '%s' "$got" | wc -l | tr -d ' ')" == "0" ]] || fail "announce-once" "not one line: $got"
for i in $(seq 1 10); do
  again="$(edited $S /work/ui/Other$i.swift)"
  [[ -z "$again" ]] || fail "announce-dedup" "call $i spoke again: $again"
done
pass "announce-once"

# (5) The marker is keyed on the VALUE, so a mid-session flip re-arms it. A
#     marker keyed on the setting alone would swallow the new directive and
#     leave the session acting on the old one for the rest of its life.
S=rearm-$$
seed "$S" delivery_method distribute
got="$(ctx "$(stopped $S)")"
[[ "$got" == *"delivery_method=distribute"* ]] || fail "rearm" "first value not announced: $got"
[[ -z "$(stopped $S)" ]] || fail "rearm" "same value announced twice"
seed "$S" delivery_method build
got="$(ctx "$(stopped $S)")"
[[ "$got" == *"delivery_method=build"* ]] || fail "rearm" "flip did not re-arm: $got"
pass "rearm-on-value"

# (6) The case a session-type gate gets wrong: an infra session that grows one
#     UI file. Nothing about the session says simulator until this edit does.
S=infra-$$
seed "$S" sim_verify off
[[ -z "$(edited $S /work/infra/daemon.py)" ]] || fail "swift-in-infra" "a .py edit announced"
got="$(ctx "$(edited $S /work/app/Sources/Detail.swift)")"
[[ "$got" == *"sim_verify=off"* ]] || fail "swift-in-infra" "the .swift edit did not fire: $got"
pass "swift-in-infra"

# (7) Commands are matched on the command line, not on being Bash.
S=bash-$$
seed "$S" sim_verify off
[[ -z "$(ran $S 'ls -la /work')" ]] || fail "bash-trigger" "a non-matching command announced"
got="$(ctx "$(ran $S 'xcodebuild -scheme App -destination generic/platform=iOS')")"
[[ "$got" == *"sim_verify=off"* ]] || fail "bash-trigger" "xcodebuild did not fire: $got"
pass "bash-trigger"

# (8) The ordinals, pinned — see the note at the top of this file. Written out
#     rather than derived, so that a group inserted above one of these fails
#     here instead of silently un-trusting whatever moved down.
"$PY" - "$PLUGIN_ROOT/hooks/hooks.json" <<'EOF' || fail "append-only" "an env hook moved — read the ordinal note in this file"
import json, sys
manifest = json.load(open(sys.argv[1]))["hooks"]
for event, group, index, name in (
        ("SessionStart", 0, 4, "env-entry.py"),
        ("UserPromptSubmit", 0, 6, "env-delta.py"),
        ("PreToolUse", 4, 0, "env-announce.py"),
        ("PostToolUse", 2, 0, "env-announce.py"),
        ("Stop", 0, 4, "env-announce.py")):
    hooks = manifest[event][group]["hooks"]
    assert hooks[index]["command"].endswith("/" + name), \
        f"{event}:{group}:{index} is not {name}"
    assert index == len(hooks) - 1, f"{name} is no longer last in {event}[{group}]"
EOF
pass "append-only"

# (9) The tool matchers cover every tool the registry names, and the registry
#     is where a setting declares its trigger points. A matcher in hooks.json
#     cannot read the registry, so this is the parity that keeps the two from
#     drifting: add a setting triggered on a tool nobody listed and the
#     reinforcement would never fire, with nothing saying so.
"$PY" - "$HOST" "$PLUGIN_ROOT/hooks/hooks.json" <<'EOF' || fail "matcher-parity" "a registry trigger names a tool no matcher selects"
import json, sys
sys.path.insert(0, sys.argv[1])
from jstack_host import environment as env
manifest = json.load(open(sys.argv[2]))["hooks"]
selected = {}
for event, groups in manifest.items():
    for group in groups:
        if not any(h["command"].endswith("/env-announce.py") for h in group["hooks"]):
            continue
        selected[event] = (set(group["matcher"].split("|"))
                           if group.get("matcher") else None)
for setting in env.SETTINGS:
    for trigger in setting.triggers:
        assert trigger.event in selected, \
            f"{setting.key}: nothing is registered on {trigger.event}"
        matcher = selected[trigger.event]
        if matcher is None:
            continue   # the group takes every tool on that event
        assert trigger.tool, \
            (f"{setting.key}: an any-tool trigger on {trigger.event}, whose "
             "group has a matcher — it would never fire")
        missing = set(trigger.tool.split("|")) - matcher
        assert not missing, f"{setting.key}: {sorted(missing)} not in the {trigger.event} matcher"
EOF
pass "matcher-parity"

echo ""
echo "ALL PASS — session environment verified live at entry, delta and action"
