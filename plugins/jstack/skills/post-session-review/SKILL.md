---
name: post-session-review
description: Use when reviewing a session that has ended — spawned by the session-review engine, or invoked with a session id.
argument-hint: "<session-id>"
---

# /jstack:post-session-review

Session ID is in `$ARGUMENTS`. The timeline entry is the deliverable; thread extraction and doc accuracy are hygiene and never substitute for it.

Output contract, parsed by the engine: `${CLAUDE_PLUGIN_ROOT}/skills/post-session-review/output.md`. One pass, no sub-spawns; read the JSONL once at Phase A and reuse it. The agent's walk-up CLAUDE.md carries glue applying after this procedure — read it.

## Setup

```bash
SID="$ARGUMENTS"
eval "$(bash "${CLAUDE_PLUGIN_ROOT}/skills/post-session-review/resolve-seat.sh" "$SID")"
```

Empty `$JSONL` → emit a single-line `## SUMMARY` saying no transcript was found, and exit.

## Phase A — thread extraction

```bash
python3 - "$JSONL" <<'PY'
import json, os, sys
sys.path.insert(0, os.environ["CLAUDE_PLUGIN_ROOT"])
from session_runtime import user_text
for line in open(sys.argv[1]):
    try:
        row = json.loads(line)
    except ValueError:
        continue
    text = user_text(row)
    if text and not text.startswith(("<", "Caveat:", "# AGENTS.md instructions")):
        print(row.get("timestamp", ""), " ".join(text.split()))
PY
```

Count only real user prose — skip `<system-reminder>`, `<command-message>`, `<command-name>`, tool-result blobs, bootstrap payloads.

Classify each distinct topic into exactly one: `resolved-in-session`, citing the resolution · `filed-elsewhere`, citing the destination · `user-dropped` — skip / forget / moving on / later, citing the exact line · `unfinished-active-work` — live in the last exchanges, never completed · `silently-dropped` — raised, moved past, in no file.

File a follow-up for each of the last two:

```bash
file-followup "Glanceable issue title" "1–2 plain sentences: what's unfinished and why it matters."
```

Title states the issue plainly, never action framing; body carries no hashes, GUIDs, paths or estimates. Already filed → update or skip, never a stacked "still pending" copy. Under `followup_backend: none` this is a no-op, so carry the thread as a Phase C detail bullet instead.

A correction or new rule the user gave is applied at the most specific place that loads when the behavior matters — never parked in a follow-up.

## Phase B — accuracy

Reconcile the docs this session touched, referenced or invalidated. `agree` needs no action. `fossil` — references superseded work; `Edit` the line out. `phantom` — the session claimed an update that never landed; apply it now and cite the edit.

## Phase C — timeline

```bash
log_event tail "$SEAT" -n 10
STAMP=$(python3 -c 'import os,sys,datetime as d;print(d.datetime.fromtimestamp(os.stat(sys.argv[1]).st_mtime).strftime("%Y-%m-%d %H:%M"))' "$JSONL")
log_event "$SEAT" --at "${STAMP#* }" --date "${STAMP%% *}" --session "$SID" "headline ≤120 chars" \
  --detail "≤80 chars" --detail "≤80 chars" [--context "freeform depth, loaded on demand"]
```

Use `$STAMP` verbatim — timestamps inside the JSONL are UTC and would stamp the entry hours ahead.

Tags are the other axis, and they are **read before the entry is composed**, not only set after it — the seat's ten say what this cockpit did, a tag says what the subject did across every seat. Procedure, both halves: `${CLAUDE_PLUGIN_ROOT}/skills/post-session-review/tags.md`.

`--context` is optional depth, stored but never injected. Use it when the session left state outgrowing three bullets — transcripts get cleaned, so the entry plus its context is all the next session gets.

Timeline-worthy: code shipped, feature live, decision made, problem fixed, a user directive that drove work, significant autonomous work, something durable learned. Not: reviewed-session notes, build or test counts, paths, hashes, UUIDs, cleanup. Detail bullets serve the next run — an open thread, a decision and its why, a do-not-repeat.
