---
name: takeover
description: Use only if /takeover was typed and the jStack hook did not answer it.
argument-hint: "[@agent[/seat]] [focus]"
---

JSTACK_TAKEOVER_CMD $ARGUMENTS

---

**If you are reading this, the hook did not fire.** `hooks/takeover-command.py` normally
intercepts this prompt and answers it without starting a turn; reaching the model
means it is not wired, not executable, or this Claude Code version does not run
`UserPromptSubmit` for slash commands. Say so in one line, then do the work by hand —
and do NOT write a summary of this session while doing it. A takeover exists precisely
so the next session does not inherit this one's account of itself. Point it at the
transcript; it reads the source.

`/jstack:takeover`'s SKILL.md carries the briefing template and the by-hand procedure.
The short form:

```bash
# the transcript is the payload's, never reconstructed from id + cwd
BRIEF="$(mktemp -t jstack-takeover)"; rm -f "$BRIEF"     # write the template there
open-terminal-here "$TARGET_CWD" --prompt-file "$BRIEF" --name "TO · <focus>" \
  --first-prompt "Take over the session named in your briefing: read it from the transcript, verify what it claims against the tree, then continue — focus: <focus>."
```

`--first-prompt` is what makes the spawn a task rather than a staged context. If the
adapter on this machine does not advertise it (`open-terminal-here` with no args prints
its usage), drop the flag and tell the user the window opened but did not start.

`@agent` resolves case-insensitively under the agent root; `@agent/seat` names a seat
directly. Unknown name → list the agents and stop, never guess a directory.
