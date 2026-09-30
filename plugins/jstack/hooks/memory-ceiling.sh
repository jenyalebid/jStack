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
# Text is file-sourced (rules-stage/prompt-sourcing.md): prompts/memory-ceiling.md.
JSTACK_HOOKS_DIR="$(cd "$(dirname "$0")" && pwd)" exec python3 -c '
import json, os, sys

CEILING = 20

try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(0)   # a hook that dies is a hook switched off for the rest of the session

tool_input = payload.get("tool_input") or {}
path = tool_input.get("file_path") or ""
if not path.endswith("/memory/MEMORY.md"):
    sys.exit(0)

sys.path.insert(0, os.environ["JSTACK_HOOKS_DIR"])
from _prompts import load


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
        deny(load("memory-ceiling.md", "write").format(ceiling=CEILING, count=count))
elif tool in ("Edit", "MultiEdit"):
    # Judged on the file as it stands: an Edit does not carry the whole document, so
    # the only honest reading of what it is about to grow is what is there now.
    try:
        with open(path, errors="replace") as fh:
            count = lines_of(fh.read())
    except OSError:
        sys.exit(0)
    if count >= CEILING:
        deny(load("memory-ceiling.md", "edit").format(ceiling=CEILING, count=count))
sys.exit(0)
'
