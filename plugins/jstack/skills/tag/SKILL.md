---
name: tag
description: Use when filing this session under a timeline subject, or when asked what it is filed under.
argument-hint: "[name] | -[name] | --session <id>"
---

# /jstack:tag — file a session under a subject

A tag answers what a stretch of work was *about*, across seats and dates, and it
attaches to the **session** — so every entry that session wrote, and every one it
writes later, reaches the tag through its session id.

**When the user types `/tag`, this skill is not what runs.** `hooks/tag-command.py`
intercepts it at UserPromptSubmit and answers from the db without starting a turn —
bookkeeping shouldn't cost a model call. Use this skill for what the hook cannot do:
tagging a session mid-turn, in light of work you have actually been doing.

## Procedure

Read the vocabulary before picking. Inside a session it answers both halves at once —
the tags with their descriptions and use counts, and a `●` on the ones this session
already carries:

```bash
log_event tag list
```

Then, one tag at a time:

- **Already `●`** — say it is filed. Don't re-set it.
- **A name in the list** — `log_event tag set <name>`
- **Nothing fits** — `log_event tag new <name> --description "..."` then `set`. Write
  the description as the boundary of the subject, never a restatement of the name —
  it is what the next session matches against.
- **Wrong tag on the session** — `log_event tag unset <name>`

`--session <id>` retargets any of these; without it they mean the session you are in.
`log_event: command not found` → no timeline here. Say so in one line and stop.

## What makes a good tag

A subject **several sessions will share**. Never the seat, never the date, never this
session's headline — those are the other axes, and `tail`/`recall` already answer them.
Something that can only ever describe one sitting is not a tag.

A near match beats a new tag nearly every time; the vocabulary is worth sharing only
while it stays small. Two tags on a session is a session that did two things. Four is a
subject being described rather than named.

**Tags are siblings, never nested.** A second tag is a second subject, never a wider
one the first already sits inside. Work on something that runs on the substrate is not
also work on the substrate, however much of it the work touched — file it under the
narrower subject and stop. Stacking the containing tag beside the specific one is what
turns a subject into a bucket, and a bucket is the thing whoever opens that subject
asked to get away from.

A tag on a session with no timeline entry is invisible to every read — `tail`, `show`
and `grep` all reach entries through the session. Tag anyway; the entry the session
ends with files itself under it.
