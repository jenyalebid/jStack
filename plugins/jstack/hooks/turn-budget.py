#!/usr/bin/env python3
"""PreToolUse — hold an unattended run to its turn budget.

A turn re-reads the whole session, so a run costs turns x accumulated context.
The run this was written for spent 91.5M tokens over 386 turns, 167 of which
carried no tool call at all — pure narration between calls, at the price of the
whole conversation each time. Prose in a doc did not stop it twice. This counts
the run from its own transcript and refuses past the ceiling.

Counts, per session:
  turns  — assistant messages that requested at least one tool
  idle   — assistant messages that requested none (the 43% above)

WHO IS POLICED. Only a session nobody is typing into. A person watching their
own run does not need a hook to tell them it is long, and a denied tool call in
front of them is an obstacle rather than a saving; the discriminator is the
shipped one (`session_runtime.user_engaged`), the same answer the review engine
and the timeline reminder act on.

THE BUDGET. One shipped default for every unattended run, and named run kinds
over it in `turn-budgets.json` — a machine that knows it runs a nightly
publisher can give that kind its own ceiling without touching a hook. The
default is deliberately generous: it exists to stop a runaway, not to shape
work, so it sits above the worst real run anybody has had a reason to defend.

Under soft: silent. Over soft: every call carries the count back. Over hard:
deny, naming the one exit that strands nothing — stage what is open, book the
wake, end the turn.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from session_runtime import user_engaged  # noqa: E402

#: (soft, hard) in TOOL turns, for any unattended session. A run at 160 tool
#: turns has re-read its whole context 160 times; nothing legitimate needs more
#: without a named kind saying so.
DEFAULT = (110, 160)

CONFIG = Path(os.environ.get(
    "JSTACK_TURN_BUDGETS", str(Path.home() / ".claude/jstack/turn-budgets.json")))


def budgets() -> tuple[tuple[int, int], dict]:
    """`(default, per-kind)`. A missing or broken file is the shipped default —
    a budget that fails open is a budget; one that fails closed is an outage."""
    try:
        data = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return DEFAULT, {}
    fallback = data.get("default") or DEFAULT
    kinds = {name: tuple(pair) for name, pair in (data.get("kinds") or {}).items()
             if isinstance(pair, (list, tuple)) and len(pair) == 2}
    try:
        return (int(fallback[0]), int(fallback[1])), kinds
    except (TypeError, ValueError, IndexError):
        return DEFAULT, kinds


def classify(path, kinds) -> str:
    """The run kind, read off the session's opening turn — the wake payload that
    started it. Nothing matches on a session a person opened."""
    if not kinds:
        return ""
    try:
        with open(path, "r", errors="replace") as fh:
            for _ in range(8):
                line = fh.readline()
                if not line:
                    break
                for kind in kinds:
                    if kind in line:
                        return kind
    except OSError:
        pass
    return ""


def count(path) -> tuple[int, int]:
    """`(tool turns, idle turns)` in either dialect.

    Claude writes one `assistant` row per message, tool calls inside its
    content. Codex writes a rollout of `response_item`s, where a `function_call`
    is a tool and a `message` with role assistant is narration. Counting them
    the same way is what lets one ceiling govern both engines.
    """
    turns = idle = 0
    try:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                if '"assistant"' not in line and '"function_call"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("type") == "assistant" and isinstance(row.get("message"), dict):
                    blocks = row["message"].get("content") or []
                    used = any(isinstance(b, dict) and b.get("type") == "tool_use"
                               for b in blocks)
                    turns, idle = (turns + 1, idle) if used else (turns, idle + 1)
                elif row.get("type") == "response_item":
                    payload = row.get("payload") or {}
                    kind = payload.get("type")
                    if kind in ("function_call", "local_shell_call", "custom_tool_call"):
                        turns += 1
                    elif kind == "message" and payload.get("role") == "assistant":
                        idle += 1
    except OSError:
        pass
    return turns, idle


def say(**fields) -> None:
    print(json.dumps({"hookSpecificOutput": dict(hookEventName="PreToolUse", **fields)}))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    path = payload.get("transcript_path")
    if not path or not os.path.exists(path):
        return 0
    if user_engaged(Path(path)):
        return 0

    fallback, kinds = budgets()
    kind = classify(path, kinds)
    soft, hard = kinds.get(kind, fallback)
    label = f"{kind} run" if kind else "unattended run"

    turns, idle = count(path)
    if turns < soft:
        return 0

    waste = f"{idle} of {turns + idle} turns so far carried no tool call"
    if turns < hard:
        say(additionalContext=(
            f"TURN BUDGET — {turns}/{soft} tool turns used on this {label} ({waste}). "
            "Every turn re-reads the whole session, so narration between calls is the "
            "most expensive thing here. Batch independent calls into one turn, write no "
            f"prose between them, and never sleep to wait. Hard stop at {hard}."))
        return 0

    say(permissionDecision="deny", permissionDecisionReason=(
        f"TURN BUDGET EXCEEDED — {turns} tool turns on this {label}, ceiling {hard} "
        f"({waste}). This run is now costing more than the work it is doing. Take the "
        "exit that strands nothing: commit and push whatever is open, book the wake that "
        "carries the rest, record what you did, and end the session. Do not start new "
        "work and do not ask to continue — a fresh session costs a fraction of this "
        "one's next turn."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
