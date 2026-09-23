#!/usr/bin/env bash
# jStack live test — hooks/context-ceiling.py (the load meter, while stopping is cheap).
#
# Builds transcripts in both dialects and pipes real PreToolUse JSON through the
# real hook. What it pins:
#   - the bands are the load meter's, 160k and 200k, and the warning fires on the
#     CROSSING. A warning repeated on forty consecutive tool calls is a warning
#     switched off, so a session already inside a band gets nothing.
#   - the dialect is read off the FILE. Claude writes usage onto assistant rows;
#     Codex writes token_count events. A reader pointed at the wrong dialect finds
#     no readings and reports a healthy session it never read — the silent lie.
#   - a Claude session is told a summarizer can be steered; a Codex session is told
#     its assistant turns are dropped whatever it does. Different machine,
#     different instruction.
#   - the recovery note is measured from the session's own floor, and a session
#     whose weight is structural is told compacting is not the lever.
#   - one reading cannot show a crossing, and an unreadable transcript says nothing.
#
# Exit 0 = all pass, exit 1 = any fail.

set -u

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$PLUGIN_ROOT/hooks/context-ceiling.py"

[[ -x "$HOOK" ]] || { echo "FAIL: $HOOK not executable" >&2; exit 1; }

TMP=$(mktemp -d /tmp/jstack-ctxceiling-test.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

python3 - "$HOOK" "$TMP" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

HOOK, TMP = sys.argv[1], Path(sys.argv[2])
HEAVY, EXTREME = 160_000, 200_000

fails = []
def check(name, cond):
    print(("ok" if cond else "FAIL") + f": {name}")
    if not cond:
        fails.append(name)

def claude(name, readings):
    """A Claude transcript whose assistant messages weigh `readings`, in order."""
    rows = [{"type": "user", "message": {"content": "go"}}]
    for i, tokens in enumerate(readings):
        rows.append({"type": "assistant", "message": {
            "id": f"msg_{i}",
            "usage": {"input_tokens": 12, "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": tokens - 12},
            "content": [{"type": "text", "text": "working"}]}})
    path = TMP / name
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path

def codex(name, readings):
    """A rollout whose token_count events weigh `readings`, in order."""
    rows = [{"type": "session_meta", "payload": {"id": "abc", "source": "exec"}}]
    for tokens in readings:
        rows.append({"type": "event_msg", "payload": {
            "type": "token_count",
            "info": {"last_token_usage": {"input_tokens": tokens}}}})
    path = TMP / name
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path

def run(path, event="PreToolUse"):
    payload = {"session_id": "sid-1", "hook_event_name": event,
               "transcript_path": str(path) if path else None,
               "tool_name": "Bash", "tool_input": {"command": "ls"}}
    r = subprocess.run([HOOK], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=25)
    out = r.stdout.strip()
    note = json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""
    return r.returncode, note

# --- the crossing, and only the crossing ------------------------------------
code, note = run(claude("heavy.jsonl", [50_000, 150_000, 165_000]))
check("exits 0 when it speaks", code == 0)
check("crossing 160k speaks", bool(note))
check("the note reports the reading", "165,000" in note)
check("the note names the heavy band", "heavy band" in note)
check("the note asks for the turn to END", "END THE TURN" in note)
# The point of stopping is where the boundary lands, not that it is avoided.
check("the note says why ending is the move", "seam" in note)

code, note = run(claude("inband.jsonl", [165_000, 170_000]))
check("already inside the band says nothing", note == "")
code, note = run(claude("light.jsonl", [50_000, 90_000]))
check("a light session says nothing", note == "")
code, note = run(claude("falling.jsonl", [180_000, 60_000]))
check("a fall across a cut crosses nothing", note == "")

code, note = run(claude("extreme.jsonl", [50_000, 190_000, 205_000]))
check("crossing 200k speaks", bool(note))
check("the extreme note names the extreme cut", "extreme cut" in note)
check("the extreme note starts nothing new", "Nothing new starts now" in note)

# A turn that jumps both cuts takes the louder one; two warnings for one turn is
# a meter arguing with itself.
code, note = run(claude("double.jsonl", [50_000, 90_000, 210_000]))
check("a double jump takes the extreme band", "extreme cut" in note)

# One reading cannot show a crossing.
code, note = run(claude("one.jsonl", [170_000]))
check("a single reading says nothing", note == "")

# Several transcript lines carry the SAME message's usage. Counted separately the
# newest two are one turn twice, prev == cur, and nothing ever crosses.
dup = TMP / "dup.jsonl"
rows = [{"type": "user", "message": {"content": "go"}}]
for mid, tokens in (("msg_a", 150_000), ("msg_b", 165_000), ("msg_b", 165_000)):
    rows.append({"type": "assistant", "message": {
        "id": mid, "usage": {"input_tokens": 12, "cache_read_input_tokens": tokens - 12},
        "content": [{"type": "tool_use", "name": "Bash", "input": {}}]}})
dup.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
code, note = run(dup)
check("one message written twice is still one reading", "165,000" in note)

# --- the recovery note is measured, not assumed ----------------------------
code, note = run(claude("recover.jsonl", [50_000, 150_000, 165_000]))
check("the note names this session's own floor", "50,000-token floor" in note)
check("the note names what a compaction would free", "freeing about" in note)
check("the note says it is worth taking", "Worth taking" in note)

# A session already sitting on a heavy floor has nothing to give back, and is
# told so instead of being sent to compact for nothing.
code, note = run(claude("structural.jsonl", [140_000, 150_000, 165_000]))
check("a structural session is told compacting is not the lever",
      "structural" in note and "not the lever" in note)
# Handing off lands BELOW a compaction of the same session and pays the difference
# with the whole live thread, so no band at any size prescribes one. ("a fresh
# session's whole footprint" in the cost sentence is a unit of scale, not advice.)
check("a structural session is not told to hand off",
      not any(w in note.lower() for w in
              ("hand off", "handoff", "hand it off", "start a new session",
               "start a fresh session", "in a new session")))

# --- the dialect is read off the file --------------------------------------
code, note = run(codex("codex-heavy.jsonl", [26_000, 150_000, 165_000]))
check("a rollout's token_count events are read", bool(note))
check("the rollout reading is reported", "165,000" in note)
check("a Codex session is told its turns are dropped",
      "server-side" in note and "dropped" in note)
check("a Codex session is told a sha survives and a sentence does not",
      "a sha" in note)
# Nothing can steer a server-side summary, so promising a summarizer would be a lie.
check("a Codex session is not promised a summarizer",
      "summarizer" not in note.replace("summary you never see", ""))
code, note = run(claude("claude-declare.jsonl", [50_000, 150_000, 165_000]))
check("a Claude session is not told its turns are dropped", "server-side" not in note)

code, note = run(codex("codex-light.jsonl", [26_000, 90_000]))
check("a light rollout says nothing", note == "")

# Codex re-emits an unchanged figure when a turn ends without a request. A repeat
# landing on a cut would eat the one warning that cut ever gives.
code, note = run(codex("codex-repeat.jsonl", [26_000, 150_000, 150_000, 165_000]))
check("a repeated rollout reading does not eat the warning", "165,000" in note)

# --- nothing it is handed may make it fail ---------------------------------
code, note = run(None)
check("no transcript path exits 0 silently", code == 0 and note == "")
code, note = run(TMP / "does-not-exist.jsonl")
check("a missing transcript exits 0 silently", code == 0 and note == "")
junk = TMP / "junk.jsonl"
junk.write_text("not json\n{\n\n")
code, note = run(junk)
check("an unparseable transcript exits 0 silently", code == 0 and note == "")
r = subprocess.run([HOOK], input="not json", capture_output=True, text=True, timeout=25)
check("garbage stdin exits 0", r.returncode == 0 and r.stdout.strip() == "")

print()
if fails:
    print(f"context-ceiling: {len(fails)} FAILED", file=sys.stderr)
    sys.exit(1)
print("context-ceiling: all pass")
PY
