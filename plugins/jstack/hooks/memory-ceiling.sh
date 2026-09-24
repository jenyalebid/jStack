#!/bin/bash
# PreToolUse — the ceiling on the auto-memory index.
# MEMORY.md auto-loads into EVERY agent session, so its length is a tax on all of
# them. Memory holds personal things about the user; a durable rule belongs in the
# walk-up layer that owns it, where one write reaches every agent. This ceiling
# keeps the index at pointer scale so the pile can't rebuild itself.
#
# The decision is python3, not jq. jq is not on a stock macOS and nothing in this
# product installs it, so the jq version of this hook read an empty file path on
# every fresh machine, fell through its own path filter and allowed everything —
# silently, which is the worst way for a guard to be absent. python3 is already a
# hard dependency: five of the hooks beside this one are written in it.
exec python3 -c '
import json, sys

CEILING = 20

try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(0)   # a hook that dies is a hook switched off for the rest of the session

tool_input = payload.get("tool_input") or {}
path = tool_input.get("file_path") or ""
if not path.endswith("/memory/MEMORY.md"):
    sys.exit(0)

MOVE_IT = (" It auto-loads into every agent session. Memory holds personal things about "
           "the user — a durable rule belongs in the layer of the walk-up that owns it "
           "(org CLAUDE.md, agent root, seat, or a path-scoped rule in ~/.claude/rules/), "
           "and a platform truth belongs in an on-demand file under ~/Research/. Move it "
           "there and drop it from here.")


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason}}))
    sys.exit(0)


def lines_of(text):
    """Lines as a reader counts them: a final line with no newline is still a line,
    and a trailing newline does not invent an empty one after it."""
    if not text:
        return 0
    return len(text.split("\n")) - (1 if text.endswith("\n") else 0)


tool = payload.get("tool_name") or ""
if tool == "Write":
    count = lines_of(tool_input.get("content") or "")
    if count > CEILING:
        deny(f"MEMORY.md ceiling is {CEILING} lines; this write is {count}." + MOVE_IT)
elif tool in ("Edit", "MultiEdit"):
    # Judged on the file as it stands: an Edit does not carry the whole document, so
    # the only honest reading of what it is about to grow is what is there now.
    try:
        with open(path, errors="replace") as fh:
            count = lines_of(fh.read())
    except OSError:
        sys.exit(0)
    if count >= CEILING:
        deny(f"MEMORY.md is already at {count} lines (ceiling {CEILING}). Adding via "
             "Edit is blocked. Rewrite it with Write under the ceiling first — move "
             "anything that is really a rule into the walk-up layer that owns it, and "
             "delete what has aged out.")
sys.exit(0)
'
