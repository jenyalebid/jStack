#!/usr/bin/env python3
"""UserPromptSubmit hook — one line when the environment MOVED under a session.

A toggle flipped from the phone halfway through a sitting changes how the work
must be done from that moment on, and the session was told the old answer at
entry. This is the correction: the non-defaults are re-read each prompt,
compared with the snapshot this session left last turn, and only what actually
moved is said. Unchanged is completely silent — a per-prompt reminder of state
that has not changed is the repetition the mechanism was rejected for once
already.

The first prompt of a session has no snapshot, and stays silent too: entry has
just said the same thing, and a delta on top of it would be the same line
twice in the same breath. The snapshot is written and nothing is emitted.

Cost is one sqlite read per prompt, against the store the Hub already serves.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402 — sibling module, path set above

#: Beside the path-rule markers, as JSON `{key: value}` of non-defaults only —
#: the shape `delta_lines` documents as an acceptable `before`, so the file can
#: be handed to it without a translation step that could invent a change.
SNAPSHOT = "environment.json"


def main() -> int:
    if _env.disabled():
        return 0
    payload = json.load(sys.stdin)
    session_id = str(payload.get("session_id") or "")
    env = _env.host_environment()
    after = _env.moved(env, session_id, str(payload.get("cwd") or ""))
    snapshot = _env.session_dir(session_id) / SNAPSHOT

    before = None
    try:
        loaded = json.loads(snapshot.read_text())
        if isinstance(loaded, dict):
            before = {str(k): str(v) for k, v in loaded.items()}
    except (OSError, ValueError):
        before = None

    try:
        snapshot.write_text(json.dumps(after))
    except OSError:
        # An unwritable marker dir would make every prompt a first prompt.
        # Saying nothing beats saying the same delta on every turn forever.
        return 0

    if before is None:
        return 0
    lines = env.delta_lines(before, after)
    if lines:
        _env.emit("UserPromptSubmit", "\n".join(lines))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # A store this hook cannot reach costs the prompt nothing.
        raise SystemExit(0)
