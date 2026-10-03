"""PreToolUse hook: keep tests and guests where they belong.

Two refusals. A guest booted by hand, anywhere, ever: the sandbox boots every
guest, and every guest has a window. And on a host whose mode is `off`, a
simulator boot, a UI test run or a Simulator launch outside a lease: that work
goes through a lease.

Only a command in command position counts — after a separator or a pass-through
prefix (nohup, sudo, env, timeout…). The quoted payload of an exec wrapper (ssh,
machine run, tmux, sh -c, eval) is a command by construction, so it is checked
as one; quotes inside it are data again. Reading about a command is not running
it: a grep for `tart run` passes, here or over ssh. Heredoc bodies are data
unless the heredoc feeds a shell.
"""
from __future__ import annotations

import json
import re
import sys

from . import settings

PREFIX = (r"(?:(?:sudo|nohup|setsid|caffeinate|env|command|exec|time|timeout|stdbuf|nice)"
          r"(?:\s+[^\s;&|'\"]+)*\s+)?")
BOOT = r"(?:\S*/)?(?:tart\s+run\b|qemu-system-\S+|VBoxHeadless\b)"
LOCAL_WORK = (r"(?:(?:\S*/)?xcrun\s+)?(?:\S*/)?simctl\s+boot\b"
              r"|(?:\S*/)?xcodebuild\b[^\n;&|]*\btest(?:-without-building)?\b"
              r"|open\s+(?:-\S+\s+)*-a\s+\"?Simulator\b")
PAYLOAD = re.compile(r"\b(?:ssh|machine\s+run|tmux|eval|watch|(?:ba|z)?sh\s+-c)\b"
                     r"[^'\"\n]*?(['\"])(.*?)(?<!\\)\1")
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


def _at_command(pattern: str, line: str, depth: int = 0) -> bool:
    if re.search(rf"(?:^|[;&|(\n])\s*{PREFIX}(?:{pattern})", line):
        return True
    return depth < 4 and any(_at_command(pattern, m.group(2), depth + 1)
                             for m in PAYLOAD.finditer(line))


def verdict(command: str, mode: str) -> str:
    for line in live_lines(command):
        if _at_command(BOOT, line):
            return ("Guests are booted by the sandbox, each with a window, never by "
                    "hand: `jstack-host sandbox get <image>` boots one and leases it. "
                    f"Refused: {line.strip()}")
        if (mode == "off" and _at_command(LOCAL_WORK, line)
                and not THROUGH_SANDBOX.search(line)):
            return ("This host takes no test work (sandbox mode off). Run it in a "
                    "lease: `jstack-host sandbox get <image>`, then "
                    "`jstack-host sandbox exec <lease> -- <command>`. "
                    f"Refused: {line.strip()}")
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
