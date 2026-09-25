#!/usr/bin/env python3
"""SessionStart hook — the session's environment, stated once, as state.

The floor the other two moments stand on: how this sitting runs is said before
the first prompt, so nothing downstream has to repeat it to be obeyed. Where
nothing has been moved, `state_line` is `""` and this hook writes no block at
all — a line announcing that everything is normal is precisely the bloat the
mechanism exists to avoid, and the silence is what makes it safe to ship to
machines nobody has configured.

Separate from `session-start-inject.py` on purpose. That file's contract is
stdlib only, and it holds because a SessionStart hook that raises breaks every
session start on this machine. This one reads the store, so it cannot live
inside that contract and carries its own bare-exception floor instead.

Registered LAST in its group: SessionStart contexts concatenate in group order,
and standing state the model must hold all sitting belongs nearest the prompt.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402 — sibling module, path set above
import _host  # noqa: E402 — stdin kept, so a re-exec can hand it over


def main() -> int:
    if _env.disabled():
        return 0
    payload = json.loads(_host.read_stdin())
    env = _env.host_environment()
    # The agent comes from the cwd, not from the store's session row: at
    # SessionStart there is no row yet. Session id stays first so a value set
    # on a session that DOES exist — a resume, a compact — still wins.
    line = env.state_line(env.resolve(
        str(payload.get("session_id") or ""),
        _env.seat_agent(str(payload.get("cwd") or ""))))
    if not line:
        return 0
    _env.emit("SessionStart",
              f"<jstack-environment>\n{line}\n</jstack-environment>")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # A store this hook cannot reach costs the session nothing.
        raise SystemExit(0)
