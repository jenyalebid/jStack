#!/usr/bin/env python3
"""PreToolUse / PostToolUse / Stop hook — the directive, at the moment it binds.

Entry states the environment and the delta corrects it; this is the
reinforcement, and it fires where a setting is about to be ignored rather than
on a clock. A setting declares its own trigger points in the host registry, so
the moment worth speaking at is a property of the setting and not a regex in
this file that nobody updates when a setting changes.

ONCE PER SESSION PER VALUE, on a marker keyed by value: a second `.swift` edit
says nothing and the hundredth says nothing, while a mid-session flip re-arms
the line instead of being swallowed by the marker the old value left. The only
thing that repeats a line is `JSTACK_RULE_REINJECT_BYTES` of transcript growth
— the same answer to decay the path rules already use.

Because dedup makes a miss the expensive case and a spurious match the cheap
one, the registry's triggers are deliberately over-broad: a trigger that misses
costs the reinforcement, never the setting, which entry already stated.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402 — sibling module, path set above


def fires(trigger, event: str, tool: str, tool_input: dict, rules) -> bool:
    """Whether one declared trigger matches the payload in hand.

    `pattern`'s meaning follows `tool`, which is the registry's own rule: a
    command regex against Bash, a path glob against an edit tool. The glob is
    the path rules' matcher rather than a second one, so `**/*.swift` decides
    the same way here as it does when a rule body is injected.
    """
    if trigger.event != event:
        return False
    if trigger.tool:
        try:
            if not re.fullmatch(trigger.tool, tool):
                return False
        except re.error:
            return False
    if not trigger.pattern:
        return True
    if tool == "Bash":
        try:
            return re.search(trigger.pattern,
                             str(tool_input.get("command") or "")) is not None
        except re.error:
            return False
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    return bool(path) and rules._glob_match(trigger.pattern, str(path))


def main() -> int:
    if _env.disabled():
        return 0
    payload = json.load(sys.stdin)
    event = str(payload.get("hook_event_name") or "")
    if not event:
        return 0
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    env = _env.host_environment()
    session_id = str(payload.get("session_id") or "")
    in_force = _env.moved(env, session_id)
    if not in_force:
        return 0

    rules = _env.path_rules()
    grown = rules._file_size(str(payload.get("transcript_path") or ""))
    threshold = _env.reinject_bytes()

    lines = []
    for s in env.SETTINGS:
        value = in_force.get(s.key)
        if not value:
            continue
        sentence = env.announce(s.key, value)
        if not sentence:
            continue
        if not any(fires(t, event, tool, tool_input, rules) for t in s.triggers):
            continue
        marker = _env.announce_marker(env, session_id, s.key, value)
        last = rules._read_marker(marker)
        if last is not None and grown - last < threshold:
            continue
        rules._write_marker(marker, grown)
        lines.append(f"SESSION ENVIRONMENT — {s.key}={value}: {sentence}")

    if lines:
        _env.emit(event, "\n".join(lines))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # Never a block: a hook that cannot read the store must cost a tool
        # call nothing, and this one has no verdict to give in any case.
        raise SystemExit(0)
