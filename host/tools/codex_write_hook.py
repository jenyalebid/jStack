#!/usr/bin/env python3
"""Present each path of a Codex patch to an existing local post-write hook."""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "plugins/jstack"))
from session_runtime import patch_paths


def main():
    payload = json.load(sys.stdin)
    if payload.get("tool_name") != "apply_patch":
        return
    contexts = []
    for path in patch_paths((payload.get("tool_input") or {}).get("command", ""), payload.get("cwd") or "."):
        translated = dict(payload, tool_name="Write", tool_input={"file_path": path},
                          tool_response={"filePath": path})
        result = subprocess.run(sys.argv[1], shell=True, input=json.dumps(translated),
                                capture_output=True, text=True, timeout=15)
        if result.stdout.strip():
            try:
                context = json.loads(result.stdout).get("hookSpecificOutput", {}).get("additionalContext")
                if context:
                    contexts.append(context)
            except ValueError:
                contexts.append(result.stdout.strip())
    if contexts:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                               "additionalContext": "\n\n".join(contexts)}}))


if __name__ == "__main__":
    main()
