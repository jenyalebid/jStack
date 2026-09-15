---
name: pict
description: Use only when the user asks what a session opens with and the jStack hook did not answer it.
argument-hint: "[dir] [--full] [pict flags]"
---

# /jstack:pict — what a seat actually loads

`bin/pict` renders everything a session spawned from a directory is injected with —
globals, the CLAUDE.md walk-up, auto-memory, timeline injection, anchored path rules,
hook injectors — as one ordered document.

The Codex hook selects `--engine codex`: native AGENTS instruction discovery,
the actual jStack instruction bridge and timeline. Its header marks this as a
startup source preview; it does not claim to capture native harness/tool bytes.
Use that flag for a manual Codex preview too.

**When the user types `/pict`, this skill is not what runs.** `hooks/pict-command.py`
intercepts it at UserPromptSubmit, renders, places and opens the document, then blocks
the prompt. Three fixed steps, no judgement in any of them — and a session asked to
describe its own context describes what it *believes* is there, which is the guess the
tool exists to replace.

You are reading this because the hook did not fire: not wired, not executable, or a
Claude Code version that does not run `UserPromptSubmit` for slash commands. Say so in
one line, then do it by hand.

## By hand

```bash
pict <dir> --bare > <workspace>/pad/pict-<dir>.md
show-doc <file> --title "<dir> · pict"      # open-artifact where there is no show-doc
```

`<dir>` defaults to the session's cwd. The render must land in a directory the host's
viewer may read — a file outside the workspace opens onto a refusal. The name is stable
per rendered directory, so a second ask refreshes one document instead of stacking two.

Bare is the reading copy: the injection itself, in wire order, no weight table and no
on-demand layer — rules that fire when a file is touched are a different question from
what a session opens with. `--full` drops `--bare` for the annotated view: token weight
per source, plus the on-demand pool. Anything else is `pict`'s own; `pict --help` lists
them, `--exec-hooks` and `--prompt` are the ones worth reaching for.

Neither opener present is not a failure — the render is the product and its path is the
answer.

## After it opens

One line: what was rendered and where it is. Then read it, and name anything worth the
user's attention — the same content arriving twice, a doc claiming something that does
not load, a rule contradicting another. Nothing to flag, say nothing.

Never describe, summarise, or draw what a seat loads. Prose about context in place of a
command run means it has already gone wrong.
