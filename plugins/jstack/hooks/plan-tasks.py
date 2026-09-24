#!/usr/bin/env python3
"""Each engine's native task list, mirrored onto the stage it is being worked in.

Order of operations inside a stage. Neither engine's list knows about stages and
neither is asked to: the model keeps using its own tool, and the rows follow.

Claude keeps `~/.claude/tasks/<sid>/<n>.json`, richer than a `stage_tasks` row —
`{id, subject, description, activeForm, status, blocks, blockedBy, metadata}`.
Id, subject and status come across; the stage id goes back the other way into
`metadata`, so the link is legible from the native side too. The whole directory
is re-read on every call rather than the one task the tool touched, which makes
a missed call self-healing.

Codex has no task directory. `update_plan` carries the whole flat list every
time, so the last call is the current plan — the same shape `compact_delivery.
codex_plan` reads out of a rollout, taken here off `tool_input` where the call
itself puts it. That tool is gated by `[tools] update_plan` and 20 sampled
rollouts contain zero calls: a Codex stage with no tasks is normal, not a fault.

Kill switch: `JSTACK_PLAN_GATE_DISABLED=1`, honoured by all three plan hooks.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402 — sibling module, path set above

KILL_SWITCH = "JSTACK_PLAN_GATE_DISABLED"

#: Test override only, the same affordance `inject-path-rules.py` carries.
TASKS_DIR_ENV = "JSTACK_TASKS_DIR"

CLAUDE_TOOLS = ("TaskCreate", "TaskUpdate")
CODEX_TOOL = "update_plan"

#: Stage statuses that are finished work. The mirror targets the stage being
#: worked, and falls back to the first unfinished one when nothing is running —
#: a task list written between stages belongs to the stage about to be taken.
CLOSED = ("done",)


def _tasks_dir() -> Path:
    override = os.environ.get(TASKS_DIR_ENV)
    return Path(override).expanduser() if override else Path.home() / ".claude/tasks"


def _target(plans, plan_id: str):
    rows = plans.stages(plan_id)
    running = [s for s in rows if s["status"] == "running"]
    if running:
        return running[0]
    open_rows = [s for s in rows if s["status"] not in CLOSED]
    return open_rows[0] if open_rows else None


def _claude_tasks(session_id: str) -> list[tuple[Path, dict]]:
    folder = _tasks_dir() / session_id
    found = []
    try:
        names = sorted(folder.iterdir())
    except OSError:
        return []
    for path in names:
        if path.suffix != ".json":
            continue
        try:
            row = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(row, dict):
            found.append((path, row))
    # Numeric filenames, so `10.json` sorts after `9.json` rather than after
    # `1.json` — the file name IS the order the tool assigned.
    found.sort(key=lambda pair: (len(pair[0].stem), pair[0].stem))
    return found


def _stamp(path: Path, row: dict, stage_id: str) -> None:
    """Put the stage id in the native task's own `metadata`, additively."""
    metadata = row.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    if metadata.get("stage_id") == stage_id:
        return
    metadata["stage_id"] = stage_id
    try:
        path.write_text(json.dumps({**row, "metadata": metadata}, indent=2))
    except OSError:
        pass   # the mirror is already written; the back-reference is a courtesy


def _codex_steps(tool_input: dict) -> list[dict]:
    steps = tool_input.get("plan")
    if not isinstance(steps, list):
        return []
    out = []
    for i, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        subject = step.get("step") or step.get("subject") or step.get("title") or ""
        out.append({"native_id": str(i), "subject": str(subject),
                    "status": str(step.get("status") or "pending")})
    return out


def main() -> int:
    payload = json.load(sys.stdin)
    if os.environ.get(KILL_SWITCH):
        return 0
    tool = str(payload.get("tool_name") or "").split(".")[-1]
    if tool not in CLAUDE_TOOLS and tool != CODEX_TOOL:
        return 0
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return 0

    # `host_environment()` resolves this machine's state directory and puts the
    # host package on the path; taking `plans` after it is one loader.
    _env.host_environment()
    from jstack_host import plans  # noqa: PLC0415

    plan = plans.open_plan_for_session(session_id)
    if plan is None:
        return 0
    stage = _target(plans, plan["id"])
    if stage is None:
        return 0

    if tool == CODEX_TOOL:
        steps = _codex_steps(payload.get("tool_input") or {})
        if steps:
            plans.set_tasks(stage["id"], steps)
        return 0

    for path, row in _claude_tasks(session_id):
        native = str(row.get("id") or path.stem)
        plans.upsert_task(stage["id"], native, str(row.get("subject") or ""),
                          str(row.get("status") or "pending"))
        _stamp(path, row, stage["id"])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # A store this hook cannot reach costs the tool call nothing.
        raise SystemExit(0)
