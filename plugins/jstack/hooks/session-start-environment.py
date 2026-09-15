#!/usr/bin/env python3
"""Load a Claude-configured workspace's instructions in Codex as well."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from session_runtime import engine


def instruction_context(cwd):
    """Read existing Claude instructions and memory without running injectors."""
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))).expanduser()
    files = [claude / "CLAUDE.md"]
    for directory in [*reversed(cwd.parents), cwd]:
        if not any((directory / name).is_file() for name in ("AGENTS.md", "AGENTS.override.md")):
            files.append(directory / "CLAUDE.md")
        # Local overrides remain additive even after the shared file migrates.
        files.append(directory / "CLAUDE.local.md")
    rules = Path(os.environ.get("JSTACK_RULES_DIR", str(claude / "rules"))).expanduser()
    for rule in sorted(rules.glob("**/*.md")):
        try:
            text = rule.read_text()
        except OSError:
            continue
        if not text.startswith("---") or not re.search(r"(?m)^paths\s*:", text.split("---", 2)[1]):
            files.append(rule)
    chunks = []
    for path in dict.fromkeys(files):
        if path.is_file():
            try:
                text = path.read_text()
            except OSError:
                continue
            text = re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()
            if text.startswith("---") and "\n---" in text[3:]:
                text = text.split("---", 2)[2].strip()
            if text:
                chunks.append(f"Instructions from {path}:\n{text}")
    if os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY") != "1":
        project = str(cwd)
        try:
            result = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                                    capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                project = result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
        key = re.sub(r"[^A-Za-z0-9]", "-", project)
        memory = claude / "projects" / key / "memory" / "MEMORY.md"
        if memory.is_file():
            body = "\n".join(memory.read_text().splitlines()[:200]).strip()
            if body:
                chunks.append(f"Auto-memory from {memory} (first 200 lines):\n{body}")
    return "\n\n".join(chunks)


def main():
    payload = json.load(sys.stdin)
    if engine(payload) != "codex":
        return
    cwd = Path(payload.get("cwd") or os.getcwd()).resolve()
    context = instruction_context(cwd)
    if context:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                               "additionalContext": context}}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError):
        pass
