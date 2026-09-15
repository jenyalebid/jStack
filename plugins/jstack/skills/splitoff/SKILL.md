---
name: splitoff
description: Use only when the user asks to fork or copy this session into a new terminal and the jStack hook did not answer it.
argument-hint: "[name for the copy]"
---

# Fork this session into a separate terminal

Preserve the whole conversation and the source provider in the same workspace.
The fork gets its own native id; the source keeps running unchanged. Words
following the command name the copy, not a narrower focus. Use handoff for a
scoped restart with a brief.

The command hook normally does this without a model turn. jRemote translates
`/splitoff` for Codex; in a raw Codex terminal use `JSTACK_SPLITOFF_CMD name`.
If this skill was invoked instead, call the same adapter with the user's name:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/session_runtime.py" splitoff "name for the copy"
```

Omit the final argument for the default name. The adapter uses a native Codex
fork for Codex and dub-session for Claude. Do not copy or rewrite a Codex
rollout by hand, or resume the source id in a second process.

Report the new id and window result. If opening failed after the fork, preserve
and report the id with the adapter's resume command; do not create another fork.
