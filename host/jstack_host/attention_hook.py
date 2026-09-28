#!/usr/bin/env python3
"""Claude Code hook → jRemote dialog markers. The red dot's waiting source.

Wired in ~/.claude/settings.json for every session on this Mac. Claude Code
itself reports the exact moments a select dialog is up — nothing is inferred
from screen content or transcript shape:

  set   — Notification whose message says permission is needed (the
          permission prompt), or PreToolUse of a dialog tool
          (AskUserQuestion, ExitPlanMode — the dialog IS the tool's
          execution, so PreToolUse fires as it appears).
  clear — any PostToolUse (the dialog resolved and the tool ran),
          UserPromptSubmit, Stop, SessionEnd.

Marker = one file per session id under dashboard/state/jremote_attention/;
board.py renders `attention: "waiting"` while it exists. Known imperfection,
accepted: a dialog tool batched with other tool calls can have its marker
cleared by a sibling's PostToolUse — dialogs are conventionally called alone.

Stdlib only, no package imports, always exit 0 — a hook that fails or dawdles
taxes every tool call of every agent on the machine.
"""

import json
import os
import sys

# Where the host keeps its state. `hostenv` owns this answer for the rest of
# the package, but this file is run by Claude Code as a script with no package
# context, so it cannot import it — it reads the same env var by hand instead.
# The fallback is the standalone Hub's own answer, `hostenv.DefaultProfile
# .state_dir()`: `~/.local/state/jremote`. It used to be `<package>/../state`,
# which on a plain `install.sh` Mac wrote a second turn-state tree into the
# code checkout — untracked dirt beside the source, and a tree nothing that
# follows `jstack-host where` ever reads (#207). An embedded Hub declares its
# state dir in the embed marker, which the plugin hook turns into
# JREMOTE_STATE_DIR before this runs; the fallback is only ever a standalone.
_STATE = os.environ.get("JREMOTE_STATE_DIR") or os.path.join(
    os.path.expanduser("~"), ".local", "state", "jremote")

_DIR = os.environ.get("JREMOTE_ATTENTION_DIR") or os.path.join(
    _STATE, "jremote_attention")

_TURN_DIR = os.environ.get("JREMOTE_TURN_DIR") or os.path.join(
    _STATE, "jremote_turn")

_DIALOG_TOOLS = {"AskUserQuestion": "question", "ExitPlanMode": "plan"}


def _turn_clock(event: dict, sid: str) -> None:
    """Second marker, same idea: the harness says when a turn opens
    (UserPromptSubmit) and closes (Stop / SessionEnd). board._turn_state
    trusts this over transcript sampling — mid-turn the transcript trails
    its last text line while the model thinks, and a board tick reading
    that gap as idle flapped the working dot and sprayed done-pushes
    through every long autonomous turn. Keyed on the event name, not the
    argv mode: UserPromptSubmit arrives here in `clear` mode for the
    dialog marker while it opens the turn."""
    name = event.get("hook_event_name") or ""
    path = os.path.join(_TURN_DIR, sid)
    if name == "UserPromptSubmit":
        try:
            os.makedirs(_TURN_DIR, exist_ok=True)
            with open(path, "w") as f:
                f.write("open")
        except OSError:
            pass
    elif name in ("Stop", "SessionEnd"):
        try:
            os.unlink(path)
        except OSError:
            pass


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return
    sid = event.get("session_id") or ""
    if not sid or "/" in sid:
        return
    _turn_clock(event, sid)
    path = os.path.join(_DIR, sid)

    if mode == "clear":
        try:
            os.unlink(path)
        except OSError:
            pass
        return
    if mode != "set":
        return

    if event.get("hook_event_name") == "Notification":
        # Only the permission prompt is a dialog; "waiting for your input"
        # is an idle session, which the unread dot already covers.
        if "permission" not in (event.get("message") or "").lower():
            return
        kind = "permission"
    else:
        kind = _DIALOG_TOOLS.get(event.get("tool_name") or "")
        if not kind:
            return
    try:
        os.makedirs(_DIR, exist_ok=True)
        with open(path, "w") as f:
            json.dump({"kind": kind}, f)
    except OSError:
        pass


if __name__ == "__main__":
    main()
