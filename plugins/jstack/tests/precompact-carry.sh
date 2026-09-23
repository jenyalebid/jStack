#!/usr/bin/env bash
# jStack live test — hooks/precompact-carry.sh (what survives a compaction).
#
# The boundary cannot be deferred: the client decides before this hook runs and
# a blocked PreCompact hook's output is discarded. So the only thing under test
# is the content of stdout, which the client uses verbatim as the summarizer's
# custom instructions. What it pins:
#   - all six operational headings are present. Each one exists because losing it
#     cost something: uncommitted work in a shared tree, a wake booked twice, a
#     ruling re-litigated.
#   - the user's own instructions lead, are marked as theirs, and are marked as
#     outranking the checklist. stdout REPLACES what they typed after /compact,
#     so a hook that dropped them would be overwriting the user with itself.
#   - it stays short. The client echoes the same string to the terminal, so every
#     line here is a line the user reads at every compaction.
#   - it fires on both triggers and cannot fail: the compaction happens either way.
#
# Exit 0 = all pass, exit 1 = any fail.

set -u

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$PLUGIN_ROOT/hooks/precompact-carry.sh"

[[ -x "$HOOK" ]] || { echo "FAIL: $HOOK not executable" >&2; exit 1; }

python3 - "$HOOK" <<'PY'
import json
import subprocess
import sys

HOOK = sys.argv[1]

fails = []
def check(name, cond):
    print(("ok" if cond else "FAIL") + f": {name}")
    if not cond:
        fails.append(name)

def run(payload):
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    r = subprocess.run([HOOK], input=raw, capture_output=True, text=True, timeout=20)
    return r.returncode, r.stdout

code, out = run({"session_id": "sid-1", "trigger": "auto",
                 "transcript_path": "/nowhere/t.jsonl"})
check("exits 0 on an auto compaction", code == 0)
check("says what kind of session this is", "Agent session" in out)

# Each heading is a loss somebody paid for; naming them one at a time is what
# makes a dropped one a red test rather than a quieter summary.
for n, word in ((1, "UNCOMMITTED WORK"), (2, "THE TASK"),
                (3, "WHAT WAS RULED OUT"), (4, "VERIFICATION DONE"),
                (5, "BOOKED WAKES AND SENT MESSAGES"), (6, "OPEN DEBTS")):
    check(f"carries heading {n}: {word}", word in out)

# A summary of descriptions is the failure mode: "edited the config" cannot be
# acted on by the next session, an absolute path can.
check("demands absolute paths over descriptions",
      "absolute paths" in out and "never descriptions" in out)
check("names the shared tree as the reason work can be lost",
      "several sessions share" in out or "shared" in out)
check("asks for in-flight work to be named as in-flight", "in-flight" in out)
check("asks for the reason a thing was ruled out, not the verdict",
      "not just the verdict" in out)
check("asks for what is unverified to be flagged", "unverified" in out)

# The user reads this at every compaction.
check("stays under 25 lines", len(out.strip().splitlines()) <= 25)
check("stays under 1800 characters", len(out) <= 1800)

# --- the user's instructions lead and outrank -------------------------------
code, out = run({"session_id": "sid-2", "trigger": "manual",
                 "custom_instructions": "keep the wireguard findings verbatim"})
check("exits 0 on a manual compaction", code == 0)
check("the user's instructions are carried", "keep the wireguard findings verbatim" in out)
check("the user's instructions come before the checklist",
      out.index("keep the wireguard findings verbatim") < out.index("UNCOMMITTED WORK"))
check("they are marked as the user's", "The user asked for this compaction" in out)
check("they are marked as outranking the checklist", "take priority" in out)
check("the checklist is still carried under them", "OPEN DEBTS" in out)

code, out = run({"session_id": "sid-3", "trigger": "auto"})
check("no header when the user asked for nothing",
      "The user asked for this compaction" not in out)
code, out = run({"session_id": "sid-4", "custom_instructions": ""})
check("an empty instruction string is not a header",
      "The user asked for this compaction" not in out)

# Whatever they typed is theirs, passed through as written.
odd = 'drop nothing about "hub/leaf" — and $PATH, `backticks`, 100% of it'
code, out = run({"session_id": "sid-5", "custom_instructions": odd})
check("the user's text is passed through verbatim", odd in out)

# --- the compaction happens whether or not this hook can read its payload ---
code, out = run("not json at all")
check("garbage stdin still exits 0", code == 0)
check("garbage stdin still carries the checklist", "UNCOMMITTED WORK" in out)
code, out = run("")
check("empty stdin still exits 0 with the checklist",
      code == 0 and "OPEN DEBTS" in out)

print()
if fails:
    print(f"precompact-carry: {len(fails)} FAILED", file=sys.stderr)
    sys.exit(1)
print("precompact-carry: all pass")
PY
