#!/usr/bin/env bash
# jStack live test — hooks/turn-budget.py (the ceiling on an unattended run).
#
# A turn re-reads the whole session, so a run costs turns x accumulated context.
# Builds transcripts in both dialects and pipes real PreToolUse JSON through the
# real hook. What it pins:
#   - only a session nobody is typing into is policed. A person watching their own
#     run does not need a hook to tell them it is long, and a denied tool call in
#     front of them is an obstacle rather than a saving.
#   - the discriminator is the shipped one: a typed prompt, a mode row, a CLI-sourced
#     rollout. Anything else is unattended.
#   - three states, not two: silent under soft, counting between, denying over hard.
#   - narration is counted separately from tool turns, because narration between
#     calls is what the ceiling was written for — the run behind it spent 43% of
#     its turns saying nothing and paid the whole conversation for each.
#   - the deny names the one exit that strands nothing, and refuses the obvious
#     wrong answer (asking to continue).
#   - both dialects count against one ceiling, and a broken budget file fails OPEN.
#
# Exit 0 = all pass, exit 1 = any fail.

set -u

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$PLUGIN_ROOT/hooks/turn-budget.py"

[[ -x "$HOOK" ]] || { echo "FAIL: $HOOK not executable" >&2; exit 1; }

TMP=$(mktemp -d /tmp/jstack-turnbudget-test.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

python3 - "$HOOK" "$TMP" <<'PY'
import json
import os
import subprocess
import sys
from pathlib import Path

HOOK, TMP = sys.argv[1], Path(sys.argv[2])
SOFT, HARD = 110, 160            # the shipped default

fails = []
def check(name, cond):
    print(("ok" if cond else "FAIL") + f": {name}")
    if not cond:
        fails.append(name)

BASE = os.environ.copy()
BASE.pop("JSTACK_TURN_BUDGETS", None)
BASE["JSTACK_TURN_BUDGETS"] = str(TMP / "no-such-budgets.json")
BASE["JSTACK_REVIEW_STATE"] = str(TMP / "review-state")

def claude(name, tools=0, idle=0, opening="[cron:nightly Wake] /publish", typed=False,
           mode=False):
    rows = [{"type": "user", "message": {"content": opening}}]
    if typed:
        rows.append({"type": "user", "promptSource": "typed",
                     "message": {"content": "carry on"}})
    if mode:
        rows.append({"type": "mode", "mode": "bypassPermissions"})
    for i in range(tools):
        rows.append({"type": "assistant", "message": {"id": f"t{i}", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}})
    for i in range(idle):
        rows.append({"type": "assistant", "message": {"id": f"n{i}", "content": [
            {"type": "text", "text": "Now I will consider the next step."}]}})
    path = TMP / name
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path

def codex(name, tools=0, idle=0, source="exec"):
    rows = [{"type": "session_meta", "payload": {"id": "abc", "source": source}}]
    for i in range(tools):
        rows.append({"type": "response_item", "payload": {
            "type": "function_call", "name": "shell",
            "arguments": "{\"command\":[\"ls\"]}"}})
    for i in range(idle):
        rows.append({"type": "response_item", "payload": {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "Considering."}]}})
    path = TMP / name
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path

def run(path, env_extra=None):
    payload = {"session_id": "sid-1", "hook_event_name": "PreToolUse",
               "transcript_path": str(path) if path else None,
               "tool_name": "Bash", "tool_input": {"command": "ls"}}
    env = BASE.copy()
    env.update(env_extra or {})
    r = subprocess.run([HOOK], input=json.dumps(payload), env=env,
                       capture_output=True, text=True, timeout=25)
    out = r.stdout.strip()
    return r.returncode, (json.loads(out)["hookSpecificOutput"] if out else {})

# --- under soft: silent -----------------------------------------------------
code, d = run(claude("quiet.jsonl", tools=SOFT - 1, idle=40))
check("exits 0 under the soft cut", code == 0)
check("under the soft cut it says nothing", d == {})

# --- between soft and hard: the count comes back on every call -------------
code, d = run(claude("soft.jsonl", tools=SOFT + 5, idle=60))
note = d.get("additionalContext", "")
check("over the soft cut it speaks", bool(note))
check("it is a note, not a denial", "permissionDecision" not in d)
check("the note reports the count against the cut", f"{SOFT + 5}/{SOFT}" in note)
check("the note names it an unattended run", "unattended run" in note)
# The 43% is the whole reason the hook exists; a count that hid it would be a
# turn counter, not a budget.
check("the note reports the narration share",
      f"60 of {SOFT + 65} turns so far carried no tool call" in note)
check("the note names what to do instead", "Batch independent calls" in note)
check("the note forbids sleeping to wait", "never sleep to wait" in note)
check("the note names the hard stop", f"Hard stop at {HARD}" in note)

# --- over hard: denied -----------------------------------------------------
code, d = run(claude("hard.jsonl", tools=HARD + 1, idle=100))
check("exits 0 when it denies", code == 0)
check("over the hard cut the call is denied", d.get("permissionDecision") == "deny")
reason = d.get("permissionDecisionReason", "")
check("the deny states the count and the ceiling",
      str(HARD + 1) in reason and f"ceiling {HARD}" in reason)
# A deny with no exit strands whatever the run was holding.
check("the deny names the exit that strands nothing",
      "commit and push" in reason and "book the wake" in reason)
check("the deny refuses the obvious wrong answer",
      "do not ask to continue" in reason.lower())
check("the deny says a fresh session is cheaper than this one's next turn",
      "fraction" in reason)
code, d = run(claude("exactly.jsonl", tools=HARD, idle=0))
check("exactly at the hard cut is denied", d.get("permissionDecision") == "deny")

# --- who is policed --------------------------------------------------------
code, d = run(claude("typed.jsonl", tools=HARD + 200, idle=200, typed=True))
check("a session with a typed prompt is never policed", d == {})
code, d = run(claude("mode.jsonl", tools=HARD + 200, idle=200, mode=True))
check("a session with a mode row is never policed", d == {})
code, d = run(codex("cli.jsonl", tools=HARD + 200, source="cli"))
check("a CLI-sourced rollout is never policed", d == {})

# --- one ceiling, both dialects -------------------------------------------
code, d = run(codex("codex-soft.jsonl", tools=SOFT + 5, idle=30))
check("a rollout's function calls count as tool turns",
      f"{SOFT + 5}/{SOFT}" in d.get("additionalContext", ""))
code, d = run(codex("codex-hard.jsonl", tools=HARD + 1, idle=10))
check("a rollout is denied at the same ceiling",
      d.get("permissionDecision") == "deny")
# An assistant message in a rollout is narration; a shell call is work.
code, d = run(codex("codex-idle.jsonl", tools=SOFT + 1, idle=99))
check("a rollout's assistant messages count as narration",
      f"99 of {SOFT + 100} turns" in d.get("additionalContext", ""))

# --- a named run kind may have its own ceiling ----------------------------
budgets = TMP / "budgets.json"
budgets.write_text(json.dumps({"default": [110, 160],
                               "kinds": {"nightly": [4, 6]}}))
code, d = run(claude("kind.jsonl", tools=5, idle=1), {"JSTACK_TURN_BUDGETS": str(budgets)})
check("a named kind uses its own soft cut", "5/4" in d.get("additionalContext", ""))
check("the note names the kind", "nightly run" in d.get("additionalContext", ""))
code, d = run(claude("kind-hard.jsonl", tools=7, idle=1), {"JSTACK_TURN_BUDGETS": str(budgets)})
check("a named kind uses its own hard cut", d.get("permissionDecision") == "deny")
# The kind is read off the opening turn, so a run that does not name itself gets
# the shipped default rather than another kind's ceiling.
code, d = run(claude("unkind.jsonl", tools=7, idle=1, opening="[cron:weekly Wake] /other"),
              {"JSTACK_TURN_BUDGETS": str(budgets)})
check("a run of no named kind keeps the default", d == {})

# --- a budget that fails closed is an outage, not a budget ---------------
broken = TMP / "broken.json"
broken.write_text("{not json")
code, d = run(claude("broken.jsonl", tools=SOFT + 1, idle=1), {"JSTACK_TURN_BUDGETS": str(broken)})
check("a corrupt budget file falls back to the shipped default",
      f"{SOFT + 1}/{SOFT}" in d.get("additionalContext", ""))
odd = TMP / "odd.json"
odd.write_text(json.dumps({"default": ["lots", "more"], "kinds": {"x": [1]}}))
code, d = run(claude("odd.jsonl", tools=SOFT + 1, idle=1), {"JSTACK_TURN_BUDGETS": str(odd)})
check("an unusable default falls back to the shipped one",
      f"{SOFT + 1}/{SOFT}" in d.get("additionalContext", ""))

# --- nothing it is handed may make it fail ------------------------------
code, d = run(None)
check("no transcript path exits 0 silently", code == 0 and d == {})
code, d = run(TMP / "absent.jsonl")
check("a missing transcript exits 0 silently", code == 0 and d == {})
junk = TMP / "junk.jsonl"
junk.write_text("not json\n{\n")
code, d = run(junk)
# An unreadable transcript answers ENGAGED, because interrupting a person on a
# failed read is the expensive way to be wrong.
check("an unparseable transcript is left alone", code == 0 and d == {})
r = subprocess.run([HOOK], input="not json", env=BASE, capture_output=True,
                   text=True, timeout=25)
check("garbage stdin exits 0", r.returncode == 0 and r.stdout.strip() == "")

print()
if fails:
    print(f"turn-budget: {len(fails)} FAILED", file=sys.stderr)
    sys.exit(1)
print("turn-budget: all pass")
PY
