#!/usr/bin/env python3
"""UserPromptSubmit — `/print` answers with the transcript path, no turn spent.

"Where is this conversation stored on disk" has exactly one right answer and
the harness is already holding it: every hook payload carries `transcript_path`.
Routing the question through the model bought a turn to run a glob that can only
ever reconstruct what was handed over for free — and reconstruct it worse, since
`$CLAUDE_CODE_SESSION_ID` plus a glob is a guess about path encoding, while the
payload is the file the harness is writing to. So this hook answers in-process
and BLOCKS the prompt, the way `/tag` and `/pict` do.

Grammar:

    /print                   the path, one line

No arguments. Anything trailing is ignored rather than refused: nothing about
this question has a parameter, and a refusal over a stray word would cost the
retype the zero-turn answer exists to save.

Falls back to the session-id glob only when the payload has no transcript path —
a harness old enough to omit the field can still be answered, badly but
honestly. Never falls back to "newest .jsonl in the project dir": concurrent
sessions share a project dir, and mtime-guessing hands back somebody else's
conversation with no sign that it did.
"""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from session_runtime import transcripts
from _answer import block  # noqa: E402 — sibling module, path set above

# `/print`, `/jstack:print`, or the stub's sentinel — then the rest of the line.
TRIGGER = re.compile(r"^\s*(?:/(?:jstack:)?print|JSTACK_PRINT_CMD)\b[ \t]*(.*)$",
                     re.IGNORECASE | re.DOTALL)


def by_glob(session: str) -> list[Path]:
    """The pre-payload answer: this id's transcript, wherever it was filed.

    The glob over project dirs is deliberate — computing the encoding of a cwd
    by hand gets symlinked paths wrong, and the id is unique across them.
    """
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"
    return sorted(set(root.glob(f"*/{session}.jsonl")) | set(transcripts(session)))


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    if not TRIGGER.match(payload.get("prompt") or ""):
        sys.exit(0)          # not ours — every other prompt passes untouched

    path = (payload.get("transcript_path") or "").strip()
    if path:
        found = Path(path).expanduser()
        # Stated, not hidden: the file is created with the first turn, so the
        # very first prompt of a session can name a path nothing is at yet.
        # The path is still the right answer; the absence is worth one clause.
        note = "" if found.exists() else "   (not written yet — this session has no turn on disk)"
        block(f"{found}{note}")

    session = (payload.get("session_id") or "").strip()
    if not session:
        block("/print: no transcript path and no session id in the hook payload")

    hits = by_glob(session)
    if not hits:
        block(f"/print: no transcript on disk for session {session}")
    if len(hits) > 1:
        block("/print: the same id under two project dirs — this should not happen:\n"
              + "\n".join(str(h) for h in hits))
    block(str(hits[0]))


if __name__ == "__main__":
    main()
