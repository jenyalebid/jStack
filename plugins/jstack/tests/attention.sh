#!/usr/bin/env bash
# jStack live test — hooks/attention.py (the waiting dot's source).
#
# Pipes real hook JSON through the real hook and reads the marker off disk.
# What it pins:
#   - only a dialog sets the marker: the permission notification and the two
#     dialog tools. "Waiting for your input" is an idle session, not a dialog.
#   - any resolution clears it, so a dot that went up always comes down.
#   - THE MARKER LANDS WHERE THE HUB SERVES FROM. On a machine whose Hub is
#     mounted into another server, the state directory is declared in the embed
#     marker and nowhere else; a hook that guessed its own path would write a
#     marker the board never reads. This is the one fact no unit test covers,
#     because it is the whole of "works without configuration".
#   - the turn clock opens on UserPromptSubmit and closes on Stop/SessionEnd.
#   - a session id that is a path is refused, and nothing a hook is handed can
#     make it fail: it runs beside every tool call of every session.
#
# Exit 0 = all pass, exit 1 = any fail.

set -u

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$PLUGIN_ROOT/hooks/attention.py"

[[ -x "$HOOK" ]] || { echo "FAIL: $HOOK not executable" >&2; exit 1; }

TMP=$(mktemp -d /tmp/jstack-attention-test.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

python3 - "$HOOK" "$TMP" <<'PY'
import json
import os
import subprocess
import sys
from pathlib import Path

HOOK, TMP = sys.argv[1], Path(sys.argv[2])

fails = []
def check(name, cond):
    print(("ok" if cond else "FAIL") + f": {name}")
    if not cond:
        fails.append(name)

# A Hub mounted somewhere this checkout cannot see, declared the only way a
# process that knows nothing can find it.
served = TMP / "somewhere-else/state"
served.mkdir(parents=True)
marker_file = TMP / "embedded.json"
marker_file.write_text(json.dumps({"state_dir": str(served),
                                   "root": str(TMP / "somewhere-else"),
                                   "profile_module": "nowhere.profile"}))

BASE = os.environ.copy()
BASE.pop("JREMOTE_STATE_DIR", None)
BASE.pop("JREMOTE_ATTENTION_DIR", None)
BASE.pop("JREMOTE_TURN_DIR", None)
BASE["JREMOTE_EMBED_MARKER"] = str(marker_file)

def run(mode, payload, env_extra=None):
    env = BASE.copy()
    env.update(env_extra or {})
    return subprocess.run([HOOK, mode], input=json.dumps(payload), env=env,
                          capture_output=True, text=True, timeout=20)

def dot(sid, root=None):
    return (Path(root) if root else served) / "jremote_attention" / sid

def turn(sid, root=None):
    return (Path(root) if root else served) / "jremote_turn" / sid

def kind(sid, root=None):
    try:
        return json.loads(dot(sid, root).read_text()).get("kind")
    except (OSError, ValueError):
        return None

# --- a dialog tool: PreToolUse IS the dialog appearing -----------------------
r = run("set", {"session_id": "sid-question", "hook_event_name": "PreToolUse",
                "tool_name": "AskUserQuestion"})
check("hook exits 0 on a dialog tool", r.returncode == 0)
check("AskUserQuestion raises the dot", kind("sid-question") == "question")
check("marker lands in the state dir the embed marker declares",
      dot("sid-question").is_file())

run("set", {"session_id": "sid-plan", "hook_event_name": "PreToolUse",
            "tool_name": "ExitPlanMode"})
check("ExitPlanMode raises the dot as a plan", kind("sid-plan") == "plan")

# An ordinary tool is not a dialog, whatever mode the hook is invoked in.
run("set", {"session_id": "sid-bash", "hook_event_name": "PreToolUse",
            "tool_name": "Bash"})
check("an ordinary tool raises nothing", not dot("sid-bash").exists())

# --- the permission notification, and the idle one that looks like it -------
run("set", {"session_id": "sid-perm", "hook_event_name": "Notification",
            "message": "Claude needs your permission to use Bash"})
check("permission notification raises the dot", kind("sid-perm") == "permission")

run("set", {"session_id": "sid-idle", "hook_event_name": "Notification",
            "message": "Claude is waiting for your input"})
check("an idle notification raises nothing", not dot("sid-idle").exists())

# --- every resolution lowers it ---------------------------------------------
for event in ("PostToolUse", "UserPromptSubmit", "Stop", "SessionEnd"):
    sid = "sid-clear-" + event.lower()
    run("set", {"session_id": sid, "hook_event_name": "PreToolUse",
                "tool_name": "AskUserQuestion"})
    raised = dot(sid).is_file()
    run("clear", {"session_id": sid, "hook_event_name": event})
    check(f"{event} lowers the dot", raised and not dot(sid).exists())

# Clearing a session that never raised one is not an error.
r = run("clear", {"session_id": "sid-never", "hook_event_name": "Stop"})
check("clear on a session with no dot exits 0", r.returncode == 0)

# --- the turn clock, on the same marker pass --------------------------------
run("clear", {"session_id": "sid-turn", "hook_event_name": "UserPromptSubmit"})
check("UserPromptSubmit opens the turn", turn("sid-turn").is_file())
check("an open turn says open", turn("sid-turn").read_text() == "open")
run("clear", {"session_id": "sid-turn", "hook_event_name": "Stop"})
check("Stop closes the turn", not turn("sid-turn").exists())
run("clear", {"session_id": "sid-turn2", "hook_event_name": "UserPromptSubmit"})
run("clear", {"session_id": "sid-turn2", "hook_event_name": "SessionEnd"})
check("SessionEnd closes the turn", not turn("sid-turn2").exists())

# --- an explicit state dir outranks the declaration ------------------------
override = TMP / "explicit"
run("set", {"session_id": "sid-override", "hook_event_name": "PreToolUse",
            "tool_name": "AskUserQuestion"},
    env_extra={"JREMOTE_STATE_DIR": str(override)})
check("JREMOTE_STATE_DIR outranks the embed marker",
      dot("sid-override", override).is_file() and not dot("sid-override").exists())

# --- nothing a hook is handed may make it fail -----------------------------
bad_marker = TMP / "broken.json"
bad_marker.write_text("{not json")
r = run("set", {"session_id": "sid-broken", "hook_event_name": "PreToolUse",
                "tool_name": "AskUserQuestion"},
        env_extra={"JREMOTE_EMBED_MARKER": str(bad_marker)})
check("a corrupt embed marker still exits 0", r.returncode == 0)
r = run("set", {"session_id": "sid-absent", "hook_event_name": "PreToolUse",
                "tool_name": "AskUserQuestion"},
        env_extra={"JREMOTE_EMBED_MARKER": str(TMP / "nothing-here.json")})
check("a missing embed marker still exits 0", r.returncode == 0)

env = BASE.copy()
r = subprocess.run([HOOK, "set"], input="not json at all", env=env,
                   capture_output=True, text=True, timeout=20)
check("garbage stdin exits 0", r.returncode == 0)
r = run("set", {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion"})
check("no session id exits 0 and writes nothing", r.returncode == 0)
r = run("", {"session_id": "sid-nomode", "hook_event_name": "PreToolUse",
             "tool_name": "AskUserQuestion"})
check("no mode writes nothing", r.returncode == 0 and not dot("sid-nomode").exists())

# A session id is a filename here. One carrying a separator would write outside
# the directory the board reads, so it is refused rather than sanitised.
run("set", {"session_id": "../escape", "hook_event_name": "PreToolUse",
            "tool_name": "AskUserQuestion"})
check("a session id with a path separator is refused",
      not (served.parent / "escape").exists())

# The hook must never print: its stdout is the hook protocol's, and a stray
# line on a PreToolUse hook is parsed as a decision.
r = run("set", {"session_id": "sid-quiet", "hook_event_name": "PreToolUse",
                "tool_name": "AskUserQuestion"})
check("the hook is silent on stdout", r.stdout == "")

print()
if fails:
    print(f"attention: {len(fails)} FAILED", file=sys.stderr)
    sys.exit(1)
print("attention: all pass")
PY
