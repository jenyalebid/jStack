#!/usr/bin/env bash
# jStack live test — hooks/codex-carry.py (what a server-side compaction dropped).
#
# Pipes real hook JSON through the real hook and reads the injection back off
# stdout. Codex compacts on the server: the window is rebuilt from the user's
# own messages plus the developer prompts, and every assistant message and
# every tool result in it is gone. No compaction hook on that client carries
# `additionalContext`, so there is no summarizer to steer — the residue has to
# be read back off the rollout, which compaction does not touch.
#
# What it pins:
#   - it speaks at a compaction and nowhere else: startup and an unrecognised
#     payload are silent, because a session's own empty history teaches nothing.
#   - BOTH tool-call shapes are read. Code mode writes the command inside a
#     JavaScript string literal; the classic shell tool writes JSON arguments
#     whose argv list ends with the script. A hook that reads one shape injects
#     nothing on the other and looks exactly like a healthy one.
#   - the user's own message is not handed back: it survived the boundary
#     verbatim, so repeating it is budget spent on nothing.
#   - the budget goes to what cannot be re-run. A poll loop writes one line
#     forty times and eighty `ls` calls must not push a booked wake out.
#   - a Claude transcript is not this hook's business. One manifest serves both
#     clients, so this fires on a Claude compaction too, and finding out the
#     slow way costs a doubling scan of up to 8MB every time.
#
# Exit 0 = all pass, exit 1 = any fail.

set -u

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$PLUGIN_ROOT/hooks/codex-carry.py"

[[ -x "$HOOK" ]] || { echo "FAIL: $HOOK not executable" >&2; exit 1; }

TMP=$(mktemp -d /tmp/jstack-codex-carry-test.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

python3 - "$HOOK" "$TMP" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

HOOK, TMP = sys.argv[1], Path(sys.argv[2])

fails = []
def check(name, cond):
    print(("ok" if cond else "FAIL") + f": {name}")
    if not cond:
        fails.append(name)


def item(payload):
    return json.dumps({"type": "response_item", "payload": payload})


def code_mode(cmd):
    """How code mode writes a shell call: JavaScript, not JSON."""
    return item({"type": "custom_tool_call", "name": "exec",
                 "input": f'text(await tools.exec_command({{cmd:{json.dumps(cmd)}}}));\n'})


def function_call(cmd):
    """How the classic shell tool writes one: JSON arguments, argv list."""
    return item({"type": "function_call", "name": "shell",
                 "arguments": json.dumps({"command": ["bash", "-lc", cmd]})})


def said(text):
    return item({"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": text}]})


def typed(text):
    return item({"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": text}]})


def rollout(name, lines):
    path = TMP / name
    with path.open("w") as fh:
        fh.write(json.dumps({"type": "session_meta", "payload": {"id": "s"}}) + "\n")
        for line in lines:
            fh.write(line + "\n")
    return path


def fire(path, source="compact", payload=None):
    """The injected text, or None when the hook stayed silent."""
    if payload is None:
        payload = {"transcript_path": str(path), "source": source,
                   "hook_event_name": "SessionStart"}
    proc = subprocess.run([HOOK], input=json.dumps(payload),
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return f"EXIT {proc.returncode}: {proc.stderr}"
    if not proc.stdout.strip():
        return None
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    check("the injection declares the event it answers",
          out["hookEventName"] == "SessionStart")
    return out["additionalContext"]


# ── it speaks at a compaction and nowhere else ───────────────────────────────
path = rollout("startup.jsonl", [code_mode("git push"), said("hi")])
check("startup carries nothing", fire(path, source="startup") is None)

path = rollout("nosource.jsonl", [code_mode("git push")])
check("a payload with no source is silent, not a guess",
      fire(path, payload={"transcript_path": str(path)}) is None)

check("a payload the hook cannot read at all is silent",
      fire(None, payload={}) is None)

# ── both tool-call shapes ────────────────────────────────────────────────────
for label, shape in (("code mode", code_mode), ("the classic shell tool", function_call)):
    path = rollout(f"shape-{label.split()[0]}.jsonl", [shape("git commit -m 'the thing'")])
    note = fire(path)
    check(f"a command written by {label} comes back",
          note is not None and "git commit -m 'the thing'" in note)

# ── the last words ───────────────────────────────────────────────────────────
path = rollout("words.jsonl", [
    code_mode("git status"),
    said("Ruled out the sidecar: PostCompact has no additionalContext wire."),
])
note = fire(path)
check("its own last words come back", "no additionalContext wire" in (note or ""))

# ── what is NOT handed back ──────────────────────────────────────────────────
path = rollout("typed.jsonl", [typed("FIX THE COMPACT HOOKS"), code_mode("git push")])
note = fire(path)
check("the user's own message is not repeated",
      note is not None and "FIX THE COMPACT HOOKS" not in note)

path = rollout("empty.jsonl", [typed("do the thing")])
check("no residue means no injection", fire(path) is None)

# ── the budget ───────────────────────────────────────────────────────────────
path = rollout("polled.jsonl", [code_mode("git status")] * 40
               + [code_mode("git push origin main")])
note = fire(path)
check("a polled command is carried once, not forty times",
      note is not None and note.count("git status") == 1)
check("the command after the poll loop survives it",
      "git push origin main" in (note or ""))

path = rollout("noise.jsonl",
               [code_mode("schedule_self '2026-09-23 09:00' 'check the fleet'")]
               + [code_mode(f"ls dir{i}") for i in range(80)])
note = fire(path)
check("a booked wake outranks eighty re-runnable lines",
      "schedule_self" in (note or ""))

# ── it is appended to while it is read ───────────────────────────────────────
path = rollout("torn.jsonl", [
    '{"type": "response_item", "payload": {"type": "custom_tool_ca',
    code_mode("git push origin main"),
])
note = fire(path)
check("a torn line does not kill the carry",
      "git push origin main" in (note or ""))

# ── it names itself ──────────────────────────────────────────────────────────
path = rollout("labelled.jsonl", [code_mode("git push")])
note = fire(path) or ""
check("the carry says what it is", "CARRIED ACROSS THE COMPACTION" in note)
check("the carry says it is not new work", "nothing here is new work" in note)

# ── the other client ─────────────────────────────────────────────────────────
# One manifest serves both, so this fires on a Claude compaction too. Claude keeps
# its own window and re-reads its own transcript; there is nothing here to hand it,
# and the doubling scan below would read up to 8MB to find that out.
claude = TMP / "claude.jsonl"
with claude.open("w") as fh:
    fh.write(json.dumps({"type": "assistant", "message": {
        "id": "m1", "content": [{"type": "text", "text": "done"}]}}) + "\n")
    # A Claude transcript that quotes the Codex shape inside a tool result must not
    # be enough to claim it: the sniff reads the file's own opening records.
    fh.write(json.dumps({"type": "user", "message": {
        "role": "user", "content": '{"type": "response_item"}'}}) + "\n")
check("a Claude transcript is left to Claude", fire(claude) is None)

# A quiet rollout satisfies neither "found something" nor "hit the 8MB cap", so the
# widening loop needs the file's own size as its third bound — without it the hook
# re-reads the same bytes to reach the same silence, on every compaction.
quiet = rollout("quiet.jsonl", [typed("nothing to carry here")])
check("a rollout smaller than the tail is read once and answers silence",
      quiet.stat().st_size < 1024 and fire(quiet) is None)

if fails:
    print(f"\ncodex-carry: {len(fails)} FAILED — " + "; ".join(fails))
    sys.exit(1)
print("codex-carry: all pass")
PY
