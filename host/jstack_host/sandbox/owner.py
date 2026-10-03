"""Which session owns a lease, and whether that session is still alive.

The owner is the engine process (claude, codex) above the caller, named by pid
and start time so a recycled pid never reads as alive. The session id rides
along as a label only: env ids leak across tmux panes, the process tree does
not. A caller with no engine above it (a script, cron) owns by its own
parent shell, which is alive exactly as long as the script runs.
"""
from __future__ import annotations

import os
import re
import subprocess

ENGINES = ("claude", "codex")
_SID = re.compile(r"--(?:session-id|resume)[ =]([0-9a-fA-F-]{8,})")


def _ps(*pids: int) -> dict[int, dict]:
    args = ["ps", "-o", "pid=,ppid=,lstart=,command="]
    if pids:
        args += ["-p", ",".join(str(p) for p in pids)]
    else:
        args.append("-ax")
    out = subprocess.run(args, capture_output=True, text=True).stdout
    rows = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 8:
            continue
        pid, ppid = int(parts[0]), int(parts[1])
        rows[pid] = {"pid": pid, "ppid": ppid, "start": " ".join(parts[2:7]),
                     "command": " ".join(parts[7:])}
    return rows


def _is_engine(command: str) -> bool:
    exe = os.path.basename(command.split()[0]) if command else ""
    return exe in ENGINES or exe.startswith(tuple(e + "-" for e in ENGINES))


def _session_id(row: dict) -> str:
    match = _SID.search(row["command"])
    if match:
        return match.group(1)
    return (os.environ.get("CLAUDE_CODE_SESSION_ID")
            or os.environ.get("CODEX_THREAD_ID") or "")


def current(start_pid: int | None = None) -> dict:
    """The owner record for whoever is calling: {pid, start, sid, engine}."""
    pid = start_pid or os.getppid()
    fallback = None
    seen = set()
    while pid and pid > 1 and pid not in seen:
        seen.add(pid)
        row = _ps(pid).get(pid)
        if not row:
            break
        if fallback is None:
            fallback = row
        if _is_engine(row["command"]):
            return {"pid": row["pid"], "start": row["start"],
                    "sid": _session_id(row),
                    "engine": os.path.basename(row["command"].split()[0])}
        pid = row["ppid"]
    if fallback is None:
        raise RuntimeError("cannot read this process's ancestry")
    return {"pid": fallback["pid"], "start": fallback["start"], "sid": "",
            "engine": "process"}


def alive(owner: dict) -> bool:
    row = _ps(int(owner.get("pid") or 0)).get(int(owner.get("pid") or 0))
    return bool(row) and row["start"] == owner.get("start")


def find_session(sid: str) -> dict | None:
    """The live engine process running session `sid`, as an owner record."""
    for row in _ps().values():
        if _is_engine(row["command"]) and sid in row["command"]:
            return {"pid": row["pid"], "start": row["start"], "sid": sid,
                    "engine": os.path.basename(row["command"].split()[0])}
    return None
