#!/usr/bin/env bash
# jStack live test — hooks/memory-ceiling.sh (the ceiling on the auto-memory index).
#
# MEMORY.md auto-loads into every session on the machine, so its length is a tax
# on all of them. Pipes real PreToolUse JSON through the real hook and reads the
# permission decision back. What it pins:
#   - a Write over the ceiling is denied, and the reason names where the content
#     belongs instead — a deny with no destination is a dead end, not a rule.
#   - a Write under the ceiling passes, silently.
#   - Edit is judged on the file ON DISK, not on the patch: an edit adds lines,
#     so a file already at the ceiling cannot be edited up.
#   - nothing else on the machine is touched. The matcher is broad (every Edit
#     and Write of every session), so anything that is not the memory index has
#     to leave with no output at all.
#
# Exit 0 = all pass, exit 1 = any fail.

set -u

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$PLUGIN_ROOT/hooks/memory-ceiling.sh"

[[ -x "$HOOK" ]] || { echo "FAIL: $HOOK not executable" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "FAIL: jq not on PATH" >&2; exit 1; }

TMP=$(mktemp -d /tmp/jstack-memceiling-test.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

python3 - "$HOOK" "$TMP" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

HOOK, TMP = sys.argv[1], Path(sys.argv[2])
CEILING = 20

fails = []
def check(name, cond):
    print(("ok" if cond else "FAIL") + f": {name}")
    if not cond:
        fails.append(name)

mem = TMP / "projects/-some-seat/memory/MEMORY.md"
mem.parent.mkdir(parents=True)

def run(tool, path, content=None):
    payload = {"hook_event_name": "PreToolUse", "tool_name": tool,
               "tool_input": {"file_path": str(path)}}
    if content is not None:
        payload["tool_input"]["content"] = content
    r = subprocess.run([HOOK], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=20)
    out = r.stdout.strip()
    decided = json.loads(out)["hookSpecificOutput"] if out else {}
    return r.returncode, decided

def index(lines):
    return "\n".join(f"- [item {i}](f{i}.md) — hook" for i in range(lines)) + "\n"

# --- Write, over and under -------------------------------------------------
code, d = run("Write", mem, index(CEILING + 5))
check("an over-ceiling Write exits 0", code == 0)
check("an over-ceiling Write is denied", d.get("permissionDecision") == "deny")
check("the deny is a PreToolUse decision", d.get("hookEventName") == "PreToolUse")
reason = d.get("permissionDecisionReason", "")
check("the reason states the ceiling and the actual count",
      f"{CEILING} lines" in reason and str(CEILING + 5) in reason)
# A ceiling that only says no makes the next session try the same write again.
check("the reason names where a rule belongs instead",
      "CLAUDE.md" in reason and "~/.claude/rules/" in reason)
check("the reason names where a platform truth belongs", "~/Research/" in reason)
check("the reason says why it is a tax", "every agent session" in reason)

code, d = run("Write", mem, index(CEILING - 5))
check("an under-ceiling Write passes silently", code == 0 and d == {})
code, d = run("Write", mem, index(CEILING))
check("a Write exactly at the ceiling passes", code == 0 and d == {})

# --- Edit is judged on disk, because an edit only ever adds ----------------
mem.write_text(index(CEILING))
code, d = run("Edit", mem)
check("Edit at the ceiling is denied", d.get("permissionDecision") == "deny")
check("the Edit reason names the current length",
      f"{CEILING} lines" in d.get("permissionDecisionReason", ""))
check("the Edit reason routes through Write",
      "Write" in d.get("permissionDecisionReason", ""))
code, d = run("MultiEdit", mem)
check("MultiEdit at the ceiling is denied too", d.get("permissionDecision") == "deny")

mem.write_text(index(5))
code, d = run("Edit", mem)
check("Edit under the ceiling passes silently", code == 0 and d == {})

# An index that does not exist yet cannot be over its ceiling.
code, d = run("Edit", TMP / "projects/-other/memory/MEMORY.md")
check("Edit of an absent index passes", code == 0 and d == {})

# --- everything else on the machine is untouched --------------------------
for path in (TMP / "notes/MEMORY.md", TMP / "memory/OTHER.md",
             TMP / "memory/MEMORY.md.bak", TMP / "src/main.py"):
    code, d = run("Write", path, index(CEILING + 50))
    check(f"not the memory index, passes: {path.name}", code == 0 and d == {})

# The hook sees every Edit and Write of every session; a payload it cannot
# parse must cost that call nothing.
r = subprocess.run([HOOK], input="{}", capture_output=True, text=True, timeout=20)
check("an empty payload passes", r.returncode == 0 and r.stdout.strip() == "")
r = subprocess.run([HOOK], input="not json", capture_output=True, text=True, timeout=20)
check("garbage stdin exits 0", r.returncode == 0)

print()
if fails:
    print(f"memory-ceiling: {len(fails)} FAILED", file=sys.stderr)
    sys.exit(1)
print("memory-ceiling: all pass")
PY
