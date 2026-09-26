#!/usr/bin/env python3
"""jStack Stop hook — auto sessions append their OWN timeline line before dying.

Auto work (cron/gateway/--print sessions nobody typed into) gets no post-session
review (by design) — but its work must still land on the daily timeline,
written by the model that did the work while its context is still loaded, not
scraped or generated after the fact. Mechanism: block the FIRST stop of an auto
session once, with a reminder to append via log_event and file it under its
subject tag (or do nothing if the wake was a no-op); the model finishes the
append and stops for real.

The tag step lives here and not in a later pass for the same reason the entry
does — this turn is the last thing that knows what the session was about, and a
tagger reading the entry back is guessing from a headline. Without it the tag
vocabulary only ever sees user-engaged seats (which tag in the review engine's
self-write), and the whole fleet's recurring work is invisible to `tag show`.

Loop guards (both required):
  - `stop_hook_active` in the hook input — the harness sets it when the model
    is already continuing from a Stop-hook block. Never block again.
  - a per-session marker file — a long-lived gateway session fires Stop after
    EVERY turn; one reminder per session, ever.

User-engaged sessions (typed prompt / TUI attach — same signals as the review
engine) are skipped: they get real reviews, which own their timeline entries.
A session that made no tool call is skipped too — it only answered, and the
block would replace that answer on a `-p` caller's stdout.

Kill switch: JSTACK_TIMELINE_REMIND_DISABLED=1. Review spawns and other
plumbing set SKIP_SESSION_HOOK=1 — honored here.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from session_runtime import user_engaged
import root
from _prompts import load as load_prompt  # noqa: E402 — sibling module

PLUGIN_BIN = Path(__file__).resolve().parent.parent / "bin"
STATE_DIR = Path(os.environ.get(
    "JSTACK_REVIEW_STATE", str(Path.home() / ".claude" / "jstack" / "review-state")
)).expanduser() / "timeline-reminded"
MIN_SESSION_BYTES = int(os.environ.get(
    "JSTACK_TIMELINE_REMIND_MIN_BYTES", 20_000
))   # below this a wake did nothing worth a timeline line


def allow():
    sys.exit(0)


def is_user_engaged(jsonl_path: Path) -> bool:
    """The shipped discriminator, which every hook that must stay out of a
    person's way now shares — see `session_runtime.user_engaged`."""
    return user_engaged(jsonl_path)


def agent_source(cwd: str) -> str:
    """Timeline [source] from the session's workspace: ~/Agents/<Name>/<dirs>
    → name/dirs (same seat resolution as the review engine and the SessionStart
    injector: the session dir's full path under the agent dir, "/"-joined —
    per-dir seats; at the agent root → chat)."""
    try:
        config = Path(os.environ.get("JSTACK_REVIEW_CONFIG", str(
            Path.home() / ".claude/jstack/review.json"))).expanduser()
        try:
            cfg = json.loads(config.read_text())
        except (OSError, ValueError):
            cfg = {}
        agent, submode = root.seat_of(cwd, cfg)
        if agent:
            return f"{agent}/{submode}"
        if cfg.get("agent_root") or os.environ.get("JSTACK_ROOT"):
            return "auto"
        parts = Path(cwd).resolve().parts
        agents_root = Path.home() / "Agents"
        if parts[:len(agents_root.parts)] == agents_root.parts:
            rest = parts[len(agents_root.parts):]
            agent = rest[0].lower()
            submode = "/".join(p.lower() for p in rest[1:]) or "chat"
            return f"{agent}/{submode}"
    except (ValueError, IndexError, OSError):
        pass
    return "auto"


def made_a_tool_call(jsonl_path: Path) -> bool:
    """Did this session do anything, or only answer?

    The byte floor was the no-op test, and it stopped being one: a fresh
    install's SessionStart injections put a one-reply `-p` session past
    20 KB before the model wrote a word. Blocking such a session replaces
    the answer its caller is reading on stdout with "nothing to log"
    (plugin/commands, 2026-09-26). Work leaves tool calls; a session with none
    shipped, fixed and replied nothing.
    """
    try:
        with jsonl_path.open() as stream:
            for line in stream:
                if '"tool_use"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("type") != "assistant":
                    continue
                content = (row.get("message") or {}).get("content")
                if isinstance(content, list) and any(
                        isinstance(b, dict) and b.get("type") == "tool_use" for b in content):
                    return True
    except OSError:
        return True   # unreadable: treat as work, the same side every guard errs on
    return False


def main():
    if os.environ.get("SKIP_SESSION_HOOK") == "1":
        allow()
    if os.environ.get("JSTACK_TIMELINE_REMIND_DISABLED") == "1":
        allow()
    try:
        d = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        allow()

    if d.get("stop_hook_active"):
        allow()   # already continuing from our block — never loop

    session_id = d.get("session_id") or ""
    transcript = d.get("transcript_path") or ""
    if not session_id or not transcript:
        allow()
    jsonl_path = Path(transcript).expanduser()
    if not jsonl_path.exists():
        allow()

    marker = STATE_DIR / session_id
    if marker.exists():
        allow()   # one reminder per session, ever (gateway Stop fires per turn)

    try:
        if jsonl_path.stat().st_size < MIN_SESSION_BYTES:
            allow()
    except OSError:
        allow()

    if not made_a_tool_call(jsonl_path):
        allow()   # answered and left — nothing happened that a line could name

    if is_user_engaged(jsonl_path):
        allow()   # user sessions get real reviews — those own the timeline

    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(os.getpid()))
    except OSError:
        allow()   # can't guarantee once-only → don't risk a loop

    source = agent_source(d.get("cwd") or "")
    reason = load_prompt("stop-timeline-reminder.md").format(
        source=source, session_id=session_id, plugin_bin=PLUGIN_BIN).rstrip("\n")
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


if __name__ == "__main__":
    main()
