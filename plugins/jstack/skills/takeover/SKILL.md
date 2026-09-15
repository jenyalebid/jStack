---
name: takeover
description: Use only when the user asks to hand this session to a fresh one that reads it from source, and the jStack hook did not answer it.
argument-hint: "[@agent[-seat]] [focus]"
---

# Continue in a fresh session that reads the source itself

Open a fresh session on the source provider, pointed at this transcript. The
new session reads the user exchanges, checks prior claims against the tree,
and continues. No outgoing summary travels; the fixed briefing template is
`prompts/takeover-briefing.md`. The source stays open and unchanged.

The prompt hook handles `$jstack:takeover` in Codex and `/jstack:takeover` in Claude
without a model turn. If this skill was invoked instead, call the shared adapter:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/session_runtime.py" takeover "@agent-seat focus words"
```

Omit the argument for this workspace and its current task. An optional `@agent`
or `@agent-seat` chooses the target workspace; the remaining words narrow the
work, not the transcript the new session may read. Keep those words as supplied
by the user. The adapter resolves the address, stages the fixed briefing, and
opens with both standing instructions and a first prompt so work starts.

Report the title, target, transcript, and launch result. If the terminal failed,
report the staged briefing path. Do not invent a summary or silently change
providers as a fallback.
