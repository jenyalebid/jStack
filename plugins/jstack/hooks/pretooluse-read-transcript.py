#!/usr/bin/env python3
"""PreToolUse:Read — a session transcript is read as dialogue, never raw.

A session .jsonl is an append-only machine log: every tool call, every tool result,
every injected rule and system-reminder and file snapshot. Speech is a rounding error
inside it — on a typical session, under 2% of the bytes. So a plain Read of one spends
most of a context window on text nobody said, and the noise then colors every answer
that follows.

This is not a thing to instruct. A rule saying "don't read transcripts whole" already
shipped in this plugin's rules-stage and was routinely ignored, because the model is
handed a path and reading it is the obvious move. So the reader changes instead: the
raw Read is denied and the dialogue-only rendering is returned in its place. The
obvious move now produces the right result, with no discipline required.

Both engines. Claude session files and Codex rollouts have different record shapes;
session_runtime normalizes them, so this hook only has to recognize a transcript.

Deliberate raw access is untouched — jq, grep and Bash over the same file all work.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import session_runtime  # noqa: E402

# Claude: ~/.claude/projects/<slug>/<uuid>.jsonl · Codex: .../rollout-*.jsonl
CLAUDE_SESSION = re.compile(r"/\.claude/projects/[^/]+/[0-9a-fA-F-]{36}\.jsonl$")
MAX_CHARS = 60_000


def is_transcript(path: Path) -> bool:
    if path.suffix != ".jsonl" or not path.is_file():
        return False
    if CLAUDE_SESSION.search(str(path)) or path.name.startswith("rollout-"):
        return True
    # Anything else claiming to be a rollout proves it by carrying session_meta.
    return bool(session_runtime.metadata(path))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if payload.get("tool_name") != "Read":
        return 0
    raw = (payload.get("tool_input") or {}).get("file_path") or ""
    if not raw:
        return 0
    path = Path(raw).expanduser()
    try:
        if not is_transcript(path):
            return 0
        turns = session_runtime.dialogue(path)
    except Exception as exc:  # a failure here must never block a legitimate read
        print(f"pretooluse-read-transcript: {exc}", file=sys.stderr)
        return 0
    # Nothing was said — a corrupt file, or one that only ever held machinery.
    # Fall through to the real read rather than answering with an empty header:
    # failing open costs context, failing closed costs the user their file.
    if not turns:
        return 0
    body = session_runtime.render_dialogue(turns)

    if len(body) > MAX_CHARS:
        body = body[:MAX_CHARS] + (
            f"\n\n[truncated at {MAX_CHARS} chars — for the recent end run: "
            f"python3 {Path(session_runtime.__file__).parent}/session_runtime.py "
            f"dialogue {path} --tail 40]")

    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason":
            "A session transcript is never read whole — it is almost entirely tool calls, "
            "tool results and injected context. Below is the session as dialogue: what the "
            "user said and what the agent said, nothing else. This IS the read — do not "
            "retry it, and do not cat the file. Reach for jq only if you need one specific "
            "non-speech record.\n\n" + body,
    }}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
