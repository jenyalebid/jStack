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

Kill switch: JSTACK_TIMELINE_REMIND_DISABLED=1. Review spawns and other
plumbing set SKIP_SESSION_HOOK=1 — honored here.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from session_runtime import codex_user_engaged
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
    """Same discriminator as session-review-spawn: promptSource="typed" or TUI
    mode/permission-mode entries — only humans produce either."""
    if codex_user_engaged(jsonl_path):
        return True
    try:
        with jsonl_path.open() as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = e.get("type")
                if t == "session_meta" and (e.get("payload") or {}).get("source") == "cli":
                    return True
                if t in ("mode", "permission-mode"):
                    return True
                if (t == "user" and not e.get("isMeta")
                        and e.get("promptSource") == "typed"):
                    return True
    except OSError:
        return True   # unreadable → treat as user session, stay out of the way
    return False


def agent_source(cwd: str) -> str:
    """Timeline [source] from the session's workspace: ~/Agents/<Name>/<dirs>
    → name/dirs (same seat resolution as the review engine and the SessionStart
    injector: the session dir's full path under the agent dir, "/"-joined —
    per-dir seats; at the agent root → chat)."""
    try:
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
