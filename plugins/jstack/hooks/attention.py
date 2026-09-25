#!/usr/bin/env python3
"""Dialog markers — the waiting dot the client draws, from this machine's hooks.

`attention.py set` while a session is holding a dialog up, `clear` when it is
not. The host renders a seat as waiting while the marker exists, which is how a
phone knows a session is stuck on a permission prompt without anything reading
its screen. The engine reports those moments itself: the permission
notification, the dialog tools (a question, a plan) whose PreToolUse fires as
the dialog appears, and every event that can only happen after it resolved.

The logic is `jstack_host.attention_hook`, which is stdlib-only for exactly
this reason: it runs beside every tool call of every session on the machine, so
it may not import the host, and it may not be slow.

WHERE THE MARKER GOES is the one thing a hook cannot work out from its own
path. A Hub mounted into another server declares its state directory in the
embed marker precisely so a process that knows nothing can find it, and this is
such a process — the plugin ships to machines whose Hub keeps state somewhere
this checkout cannot see. Read directly rather than through `embed.adopt()`:
adopting costs an import of the host package on every tool call of every
session, and the one field needed is a string in a small JSON file.
"""

import json
import os
import sys
from pathlib import Path

MARKER = Path(os.environ.get("JREMOTE_EMBED_MARKER")
              or Path.home() / ".local/state/jremote/embedded.json")


def state_dir() -> str:
    """The state directory the Hub on this machine actually serves, or ""."""
    try:
        with MARKER.open() as stream:
            return str(json.load(stream).get("state_dir") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def main() -> None:
    if not os.environ.get("JREMOTE_STATE_DIR"):
        declared = state_dir()
        if declared:
            os.environ["JREMOTE_STATE_DIR"] = declared
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _host  # noqa: PLC0415 — sibling; finds the host from a cache copy too
    _host.load("attention_hook").main()   # after the state dir is known


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass   # a session must run whether or not its dot could be drawn
