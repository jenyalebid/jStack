#!/usr/bin/env python3
"""The plan row, opened off `permission_mode` — the half that works on Codex.

`permission_mode: "plan"` rides on every hook payload of both engines and
nothing else reads it. It is the only signal Codex gives that a session is
planning at all: it has no `ExitPlanMode` tool, so `plan-exit.py` never fires
there. Seeing the field flip is the whole cross-vendor seam — this hook opens
the `planning` row that lights the Work tab, and the exit hook fills it with
stages on the engine that can.

EVENTS: `UserPromptSubmit` and `Stop`, once each per turn. Not a matcher-less
`PreToolUse` group — that runs on every tool call of every session on the
machine, ~40ms of interpreter start apiece, and `attention.py` refuses the same
group for the same reason. Nothing is lost by the cheaper pair: a turn cannot
begin without a prompt, so entry is seen at the prompt that opens it, and the
exit transition is seen at the `Stop` of the turn it happened in.

Kill switch: `JSTACK_PLAN_GATE_DISABLED=1`, honoured by all three plan hooks.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _env  # noqa: E402 — sibling module, path set above
import _prompts  # noqa: E402

KILL_SWITCH = "JSTACK_PLAN_GATE_DISABLED"

#: Beside the environment snapshot and the path-rule markers, holding the mode
#: this session was last seen in. The transition is the event, not the mode, and
#: a hook process remembers nothing between two of its own runs.
MARKER = "plan-mode.json"

#: How much of the opening prompt becomes the provisional plan title. The row
#: has to be titled at `planning` time, before any markdown exists, and the
#: prompt that entered plan mode is the only account of the work there is.
TITLE_CHARS = 120


def _title(payload: dict) -> str:
    prompt = str(payload.get("prompt") or "").strip()
    first = next((line.strip() for line in prompt.splitlines() if line.strip()), "")
    return first[:TITLE_CHARS]


def _read(marker: Path) -> dict:
    try:
        was = json.loads(marker.read_text())
    except (OSError, ValueError):
        return {}
    return was if isinstance(was, dict) else {}


def main() -> int:
    payload = json.load(sys.stdin)
    if os.environ.get(KILL_SWITCH):
        return 0
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return 0

    planning = str(payload.get("permission_mode") or "") == "plan"
    marker = _env.session_dir(session_id) / MARKER
    was = _read(marker)
    # The overwhelming majority of turns are neither in plan mode nor leaving
    # it, and they get out before the host is imported: measured +14ms over
    # bare python against +58ms with it, twice per turn of every session here.
    if not planning and not was.get("planning"):
        return 0

    # `host_environment()` is the loader that resolves this machine's state
    # directory from the embed marker and puts the host package on the path;
    # taking `plans` after it is one loader, not a second copy of it.
    _env.host_environment()
    from jstack_host import plans  # noqa: PLC0415 — only once the path is set

    if planning:
        if not was.get("planning"):
            marker.write_text(json.dumps({"planning": True}))
        if plans.open_plan_for_session(session_id) is None:
            from session_runtime import engine  # noqa: PLC0415 — minting only
            plans.open_plan(_title(payload), engine=engine(payload),
                            repo=str(payload.get("cwd") or ""),
                            session_id=session_id, role="author")
        return 0

    if not was.get("planning") or was.get("nudged"):
        return 0
    marker.write_text(json.dumps({"planning": False, "nudged": True}))

    # The honest partial. On Claude the plan is already `active` with its
    # stages: `ExitPlanMode` fired before the mode flipped, and the hook next
    # door read the markdown out of the tool call. Codex hands the transition
    # over with nothing attached — no tool call, no markdown — so the stages
    # cannot be parsed here, and inventing them would file a plan nobody wrote.
    # What is left is to say so, once, to the only party holding the text.
    plan = plans.open_plan_for_session(session_id)
    if plan is None or plan["status"] != "planning" or plans.stages(plan["id"]):
        return 0
    _env.emit(str(payload.get("hook_event_name") or "Stop"),
              _prompts.load("plan-gate.md", "codex-exit").format(plan_id=plan["id"]))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # A store this hook cannot reach costs the session nothing, and a plan
        # row is never worth a traceback in front of a prompt.
        raise SystemExit(0)
