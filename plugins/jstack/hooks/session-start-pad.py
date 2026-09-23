#!/usr/bin/env python3
"""SessionStart — make the session's scratchpad the seat's one shared folder.

A seat has one pad — `<seat>/pad`, a plain directory the user and every session
of that seat both read and write. It is a place for file communication: he
drops things in for the agent, the agent drops things in for him, and either
can use it as working room for a temporary checkout. The Files pane in the
client serves exactly that directory, from any device.

The harness disagrees by default. It hands each session a private directory and
hardcodes the path — the system temp dir, keyed by uid and working directory,
then by session id — with an on/off setting and no path. That gives a folder
per session, in temp, that nobody shares and the system eventually empties. A
session told to put its output there has put it where the user cannot reach it,
and no instruction in a doc can outrank the path in its own system prompt.

So this runs at session start and replaces that private directory with a
symlink to the seat's pad, before anything writes to it. The path the session
is told to use is then literally the shared folder: there is no second location
to remember, no instruction to obey, and no way for a session to write its
output somewhere the user cannot see it.

Only a seat gets one. A session in a scratch worktree or anywhere outside the
agents tree is left with the harness's default — a pad is a fixture of a place
agents work from, and a throwaway checkout is not one.
"""

import json
import os
import shutil
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import root  # noqa: E402 — the one declaration of where the agents tree is

PAD = "pad"


def pad_of(cwd: Path):
    """The pad of the seat this session is running under, or None.

    `enclosing_seat` and not a path test of our own: which seat a directory
    belongs to is a question the tree already answers, and a second opinion
    here would name a different seat than every other tool on the machine.
    """
    try:
        seat = root.enclosing_seat(cwd)
    except Exception:
        return None
    return None if seat is None else seat.path / PAD


def harness_scratchpad(cwd: Path, sid: str) -> Path:
    slug = re.sub(r"[/.]", "-", str(cwd))
    return Path(f"/private/tmp/claude-{os.getuid()}") / slug / sid / "scratchpad"


def link(cwd: Path, sid: str) -> str:
    pad = pad_of(cwd)
    if pad is None:
        return f"not a seat: {cwd}"
    pad.mkdir(parents=True, exist_ok=True)
    p = Path(os.environ.get("JSTACK_SCRATCHPAD") or harness_scratchpad(cwd, sid))

    if p.is_symlink():
        return "already linked" if p.resolve() == pad.resolve() else \
            f"left alone, links elsewhere: {os.readlink(p)}"

    if p.exists():
        if not p.is_dir():
            return f"left alone, not a directory: {p}"
        # A resume, or a hook that lost the race with the first write. Whatever
        # the session already put here is its output and belongs in the pad.
        for item in list(p.iterdir()):
            dest = pad / item.name
            if dest.exists():
                dest = pad / f"{item.stem}-{sid[:8]}{item.suffix}"
            shutil.move(str(item), str(dest))
        p.rmdir()

    p.parent.mkdir(parents=True, exist_ok=True)
    p.symlink_to(pad)
    return f"linked -> {pad}"


def main() -> int:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    sid = payload.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    cwd = Path(payload.get("cwd") or os.getcwd()).resolve()
    if not sid:
        return 0
    try:
        link(cwd, sid)
    except OSError:
        pass        # a session must start whether or not its pad could be wired
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
