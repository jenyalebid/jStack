#!/usr/bin/env python3
"""Load a Claude-configured workspace's instructions in Codex as well."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from session_runtime import engine


def main():
    payload = json.load(sys.stdin)
    if engine(payload) != "codex":
        return
    cwd = Path(payload.get("cwd") or os.getcwd()).resolve()
    files = [Path.home() / ".claude/CLAUDE.md"]
    # Native AGENTS instructions take precedence when a host has migrated a
    # directory. Otherwise preserve the existing global-to-local walkup.
    for directory in [*reversed(cwd.parents), cwd]:
        if not any((directory / name).is_file() for name in ("AGENTS.md", "AGENTS.override.md")):
            files.append(directory / "CLAUDE.md")
    for rule in sorted((Path.home() / ".claude/rules").glob("*.md")):
        text = rule.read_text()
        if not text.startswith("---") or "paths:" not in text.split("---", 2)[1]:
            files.append(rule)
    chunks = []
    for path in dict.fromkeys(files):
        if path.is_file():
            text = path.read_text().strip()
            if text:
                chunks.append(f"Instructions from {path}:\n{text}")
    if chunks:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                               "additionalContext": "\n\n".join(chunks)}}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError):
        pass
