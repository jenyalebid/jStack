#!/usr/bin/env python3
"""Record successful native patch writes, including calls inside code mode."""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from session_runtime import patch_paths


def main():
    payload = json.load(sys.stdin)
    sid = payload.get("session_id") or ""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", sid) or payload.get("tool_name") != "apply_patch":
        return
    paths = patch_paths((payload.get("tool_input") or {}).get("command", ""), payload.get("cwd") or os.getcwd())
    if not paths:
        return
    root = Path(os.environ.get("JSTACK_SESSION_FILES_DIR", str(Path.home() / ".claude/jstack/session-files")))
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / (sid + ".jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(paths) + "\n").encode())
    finally:
        os.close(fd)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError):
        pass
