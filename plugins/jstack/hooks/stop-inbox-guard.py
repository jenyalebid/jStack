#!/usr/bin/env python3
"""jStack Stop hook — what was addressed to THIS session reaches it.

A channel has two ends and this hook serves both:

  - A **task** is another agent's blocking request: they sent it with `--wake`,
    a session was spawned here for it, and they are waiting on the reply.
    Ending the turn without answering leaves them waiting on something that
    will never come.
  - A **reply** is the return leg: this session asked another seat for
    something, and the answer has come back. It is delivered here because this
    is the conversation that asked — a stop is the one moment a running session
    can be handed something without touching what a person is typing.
  - An **inject** is the task system's creator leg: a GitHub comment on an
    issue this session created, riding the reply's delivery. It differs in one
    thing only — an answer, if owed, goes on the issue (`gh issue comment`),
    never through `msg reply`; GitHub holds that conversation.

It binds one thing and one thing only: **messages addressed to this exact
session** — the task this session was spawned for (its first prompt carries
`[inbox:N]`), or a row bound to this session id, which is how an answer finds
the session that asked for it.

What it will never do is surface something that was merely sitting around.
Updates are addressed to a seat, not a session, and are never blocked on. A
message belonging to another session is invisible to this hook. So opening a
chat cannot make an agent go off doing whatever accumulated in a mailbox:
nothing accumulates, and nothing undelivered is discoverable.

Loop guards (all three):
  - `stop_hook_active` — the harness sets it while the model is continuing
    from a Stop-hook block. Never block again.
  - a per-session marker listing message ids already blocked on, so a long
    session gets one block per message, not one per turn.
  - the marker is written BEFORE the block is emitted; if it cannot be
    written, the hook allows rather than risk a loop.

Kill switch: JSTACK_INBOX_GUARD_DISABLED=1. Review spawns and other plumbing
set SKIP_SESSION_HOOK=1 — honored here.

Defensive: any error → silent allow. A Stop hook must never wedge a session.
Stdlib only, no host dependency.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_BIN = PLUGIN_ROOT / "bin"
sys.path.insert(0, str(PLUGIN_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from session_runtime import user_text
from _prompts import load as load_prompt  # noqa: E402 — sibling module
try:
    import root as _root
except ImportError:
    # An older install runs this hook without root.py — a supported state. A
    # Stop hook that raises wedges the session it is guarding, so this is the
    # one place degrading quietly is right: fall back to the literal pre-root
    # default at the use site.
    _root = None
STATE_DIR = Path(os.environ.get(
    "JSTACK_REVIEW_STATE", str(Path.home() / ".claude" / "jstack" / "review-state")
)).expanduser() / "inbox-guarded"

#: The routing marker a wake carries in its first prompt (bin/msg _wake_prompt).
_WAKE_MARKER = re.compile(r"\[inbox:(\d+)\]")


def allow():
    sys.exit(0)


def _config() -> dict:
    path = Path(os.environ.get(
        "JSTACK_REVIEW_CONFIG",
        str(Path.home() / ".claude" / "jstack" / "review.json"),
    )).expanduser()
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def resolve_seat(cwd: str) -> "str|None":
    """agent/submode for a workspace session — the same seat resolution the
    SessionStart injector and the review engine use."""
    cfg = _config()
    if _root is not None:
        agent, submode = _root.seat_of(cwd, cfg)
        return f"{agent}/{submode}" if agent else None
    # Older install without root.py: the literal gate this shipped with.
    root = Path(cfg.get("agent_root") or "~/Agents").expanduser()
    try:
        rel = Path(cwd).resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    if not rel.parts or not (root / rel.parts[0] / "CLAUDE.md").is_file():
        return None
    submode = "/".join(p.lower() for p in rel.parts[1:]) or "chat"
    return f"{rel.parts[0].lower()}/{submode}"


def open_items(seat: str, session_id: str = "", woken: str = "") -> list:
    """Messages addressed to THIS session and not yet taken.

    Both handles are passed explicitly rather than inherited: `woken` matches
    the task this run was spawned for — the wake, which is how every task
    arrives — and the session id matches a row bound to this session directly,
    which is how a reply finds the session that asked for it. A message
    matching neither was never addressed here and is not ours."""
    argv = [str(PLUGIN_BIN / "msg"), "pending-for", seat]
    if session_id:
        argv += ["--session", session_id]
    if woken:
        argv += ["--woken", woken]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return []
    if not r.stdout.strip():
        return []
    try:
        rows = json.loads(r.stdout)
        return rows if isinstance(rows, list) else []
    except (json.JSONDecodeError, ValueError):
        return []


def first_prompt(jsonl_path: Path) -> str:
    """The session's opening user message — where a wake's routing marker is."""
    try:
        with jsonl_path.open() as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = user_text(e)
                if content.strip():
                    return content
    except OSError:
        pass
    return ""


def already_blocked(session_id: str) -> set:
    try:
        return set((STATE_DIR / session_id).read_text().split())
    except OSError:
        return set()


def record_blocked(session_id: str, ids: set) -> bool:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = STATE_DIR / session_id
        prior = already_blocked(session_id)
        path.write_text("\n".join(sorted(prior | ids)))
        return True
    except OSError:
        return False


def _body_lines(r: dict) -> list:
    out = [f"  {r['subject']}"]
    if r.get("body"):
        body = r["body"].strip().splitlines()
        out.extend("  " + ln for ln in body[:6])
        if len(body) > 6:
            out.append(f"  … ({len(body) - 6} more lines — msg read {r['id']})")
    for att in json.loads(r.get("attachments") or "[]"):
        out.append(f"  attached: {att}")
    return out


def build_reason(rows: list, seat: str) -> str:
    """Both ends of the channel, in one block. Answers first — they are what
    this session was already waiting on — then what it owes."""
    replies = [r for r in rows if r.get("state") == "reply"]
    injects = [r for r in rows if r.get("state") == "inject"]
    tasks = [r for r in rows if r.get("state") not in ("reply", "inject")]
    lines = []

    if replies:
        n = len(replies)
        lines += load_prompt("stop-inbox-guard.md", "replies-head").format(
            n=n, s="s" if n != 1 else "",
            have="have" if n != 1 else "has").splitlines() + [""]
        for r in replies:
            head = f"[#{r['id']}] from {r['from_seat']}"
            if r.get("reply_to"):
                head += f" · answering your #{r['reply_to']}"
            lines.append(head)
            lines += _body_lines(r)
            lines.append("")
        lines += load_prompt("stop-inbox-guard.md", "replies-coda").format(
            plugin_bin=PLUGIN_BIN).splitlines() + [""]

    if injects:
        n = len(injects)
        lines += load_prompt("stop-inbox-guard.md", "injects-head").format(
            n=n, s="s" if n != 1 else "",
            s2="s" if n != 1 else "").splitlines() + [""]
        for r in injects:
            lines.append(f"[#{r['id']}] from {r['from_seat']}")
            lines += _body_lines(r)
            lines.append("")
        lines += load_prompt(
            "stop-inbox-guard.md", "injects-coda").splitlines() + [""]

    if tasks:
        n = len(tasks)
        lines += load_prompt("stop-inbox-guard.md", "tasks-head").format(
            n=n, s="s" if n != 1 else "", were="were" if n != 1 else "was",
            have="have" if n != 1 else "has").splitlines() + [""]
        for r in tasks:
            lines.append(f"[#{r['id']}] from {r['from_seat']}")
            lines += _body_lines(r)
            lines.append("")
        lines += load_prompt("stop-inbox-guard.md", "tasks-coda").format(
            plugin_bin=PLUGIN_BIN).splitlines() + [""]

    lines.append(load_prompt("stop-inbox-guard.md", "closing"))
    return "\n".join(lines)


def mark_delivered(ids: set) -> None:
    """Record that this session was shown them — and drop the resume wake that
    was booked as the slower half of the race, now that this half has won.

    Best effort: a failure here costs a duplicate delivery from the wake, never
    a lost one, so it must not stand between the session and its block."""
    try:
        subprocess.run([str(PLUGIN_BIN / "msg"), "deliver"] + sorted(ids),
                       capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        pass


def main() -> None:
    if os.environ.get("SKIP_SESSION_HOOK") == "1":
        allow()
    if os.environ.get("JSTACK_INBOX_GUARD_DISABLED") == "1":
        allow()
    try:
        d = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, ValueError):
        allow()

    if d.get("stop_hook_active"):
        allow()

    session_id = d.get("session_id") or ""
    seat = resolve_seat(d.get("cwd") or "")
    if not session_id or not seat:
        allow()

    transcript = Path(d.get("transcript_path") or "").expanduser()
    m = _WAKE_MARKER.search(first_prompt(transcript))
    rows = open_items(seat, session_id, m.group(1) if m else "")
    if not rows:
        allow()

    ids = {str(r["id"]) for r in rows}
    fresh = ids - already_blocked(session_id)
    if not fresh:
        allow()   # already blocked on every one of these — say it once, not per turn
    rows = [r for r in rows if str(r["id"]) in fresh]

    if not record_blocked(session_id, fresh):
        allow()   # can't guarantee once-only → never risk a loop

    mark_delivered(fresh)
    print(json.dumps({"decision": "block", "reason": build_reason(rows, seat)}))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:   # never wedge a session
        allow()
