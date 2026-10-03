"""PreToolUse hook: keep tests and guests where they belong.

Two refusals. A guest booted with no window, anywhere, ever: every guest is a
GUI guest. And on a host whose mode is `off`, a simulator boot, a UI test run or
a hand-driven `tart run` outside the sandbox: that work goes through a lease.
Heredoc bodies are data and are skipped unless the heredoc feeds a shell.
"""
from __future__ import annotations

import json
import re
import sys

from . import settings

HEADLESS = re.compile(r"(?<![\w-])(--no-graphics|--headless|-nographic)\b")
LOCAL_WORK = re.compile(
    r"\bsimctl\s+boot\b|\bxcodebuild\b[^\n]*\btest(-without-building)?\b"
    r"|\bopen\s+(-\S+\s+)*-a\s+\"?Simulator\b|\btart\s+run\b")
THROUGH_SANDBOX = re.compile(r"\bsandbox\s+(exec|shell)\b")
HEREDOC = re.compile(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)")
SHELLS = re.compile(r"\b(ssh|sh|bash|zsh|machine\s+run|tmux|eval)\b")


def live_lines(command: str) -> list[str]:
    out, term = [], None
    for line in command.splitlines():
        if term is not None:
            if line.strip() == term:
                term = None
            continue
        out.append(line)
        opener = HEREDOC.search(line)
        if opener and not SHELLS.search(line[:opener.start()]):
            term = opener.group(1)
    return out


def verdict(command: str, mode: str) -> str:
    for line in live_lines(command):
        if HEADLESS.search(line):
            return ("Every guest boots with a window. Drop the headless flag; "
                    "`jstack-host sandbox get <image>` boots a GUI guest.")
        if mode == "off" and LOCAL_WORK.search(line) and not THROUGH_SANDBOX.search(line):
            return ("This host takes no test work (sandbox mode off). Run it in a "
                    "lease: `jstack-host sandbox get <image>`, then "
                    "`jstack-host sandbox exec <lease> -- <command>`.")
    return ""


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    command = (event.get("tool_input") or {}).get("command") or ""
    why = verdict(command, settings.load()["mode"]) if command else ""
    if why:
        print(why, file=sys.stderr)
        return 2
    return 0
