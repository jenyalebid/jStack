#!/usr/bin/env bash
# Resolve either provider's native session id to a safely quoted transcript/seat.
set -u
SID="${1:?usage: resolve-seat.sh <session-id> [session-cwd]}"
REVIEW_PLUGIN_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
python3 - "$SID" "$REVIEW_PLUGIN_DIR" <<'PYTHON'
import json, os, shlex, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from session_runtime import metadata, transcripts
import root
paths = transcripts(sys.argv[1])
if len(paths) != 1:
    print("JSONL=\nSEAT=")
    raise SystemExit(1)
p = paths[0]
config = Path(os.environ.get("JSTACK_REVIEW_CONFIG", Path.home() / ".claude/jstack/review.json"))
cfg = json.loads(config.read_text()) if config.is_file() else {}
cwd = metadata(p).get("cwd")
if not cwd:
    for line in p.read_text().splitlines():
        try:
            cwd = json.loads(line).get("cwd")
        except ValueError:
            continue
        if cwd:
            break
seat = root.enclosing_seat(cwd, cfg) if cwd else None
if not seat:
    print("JSONL=\nSEAT=")
    raise SystemExit(1)
print("JSONL=" + shlex.quote(str(p)))
print("SEAT=" + shlex.quote(seat.timeline))
PYTHON
