---
name: takeover
description: Use only when the user asks to hand this session to a fresh one that reads it from source, and the jStack hook did not answer it.
argument-hint: "[@agent[/seat]] [focus]"
---

# /jstack:takeover — hand the work to a session that reads the source itself

Opens a new Claude Code session — here or in another agent's workspace — pointed at
**this session's transcript** and told to take over. No summary travels: it reads the
JSONL, checks the claims against the tree, and continues.

`/jstack:handoff` sends a brief this session wrote, so the new one inherits this one's
account of itself; `/jstack:splitoff` dubs the transcript whole, so it inherits every
conclusion the original reached. Takeover sends a path and a mandate, and the new session
trusts nothing until it has checked it. Take over when the session's own read is what is
in doubt.

**When the user types `/takeover`, this skill is not what runs.**
`hooks/takeover-command.py` answers it at UserPromptSubmit without starting a turn —
every step is fixed, and running it through a model puts this session's voice back into
a payload whose purpose is to exclude it. You are reading this because the hook did not
fire. Say so in one line, then do it by hand — **without summarizing this session.**

## Arguments

`@agent` retargets the workspace, resolved against the agent root: exact, then
case-insensitive, then `-`/`_` folded away. `@agent/seat` names a seat; otherwise `chat/`
if it exists, else the agent root. Unknown name → list the agents and stop, never join a
path blind. The rest is the **focus**: it scopes what the new session works on, never
what it may read.

## 1. Source, then briefing

Take the transcript path from the hook payload or `/print`. Never rebuild it from the id
and cwd: that is a guess about path encoding, and a takeover built on a guess reviews the
wrong conversation.

`hooks/takeover-command.py`'s `BRIEFING` is the template and the authority — it carries
the verified jq recipes. Two things in it a rewrite tends to drop:

- **Shape before extract.** `wc -l`, `jq -r .type | sort | uniq -c`, then targeted `jq`.
  Never `Read` a JSONL whole; it is mostly tool noise and costs the window.
- **The user spine must select `queue-operation`/`enqueue` as well as `user`.** A message
  typed while a turn was running is not filed as a `user` record, and those interjections
  are usually the corrections — select on `user` alone and the transcript reads as though
  the user never pushed back.

Stage it outside every workspace — one-shot payload, not a file anyone commits:
`BRIEF="$(mktemp -t jstack-takeover)" && rm -f "$BRIEF"`. The `rm` dodges the Write
tool's read-before-write guard; the name stays collision-safe.

## 2. Open it

```bash
open-terminal-here "$TARGET_CWD" --prompt-file "$BRIEF" --name "$TITLE" \
  --first-prompt "Take over the session named in your briefing: read it from the transcript, verify what it claims against the tree, then continue — focus: <focus>."
```

Both channels are load-bearing. `--prompt-file` is the standing mandate (appended system
prompt — survives compaction); `--first-prompt` is the positional argument that starts
the work. Without it the window waits at an empty box: a staged context, not a takeover.

The adapter is overridable, so detect the flag rather than assuming it —
`open-terminal-here 2>&1 | grep -q -- '--first-prompt'`. One that does not parse it hands
it to `claude`, which dies on an unknown option before anyone reads the window. Missing →
drop the flag and say the session opened but did not start.

`--name` is required: `TO · <topic>`, or `TO→<Agent> · <topic>`. The topic is the focus
as typed, clipped — not a title you invent.

Nonzero exit means nothing spawned: hand back `$BRIEF` and a
`cd … && claude --append-system-prompt "$(cat $BRIEF)"`.

## 3. Report

The title, where it landed, the transcript it was pointed at, and that this session is
unchanged. Say it if the spawn did not auto-start.
