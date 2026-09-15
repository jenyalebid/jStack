---
name: print
description: Use only when the user asks for this session's transcript path and the jStack hook did not answer it.
argument-hint: ""
---

# This session's transcript path

The command hook answers without a model turn. jRemote translates `/print` to
the hook sentinel for Codex; in a raw Codex terminal use `JSTACK_PRINT_CMD`.
If this skill was invoked instead, use the same deterministic adapter:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/session_runtime.py" print
```

Return its path. The adapter resolves `$CODEX_THREAD_ID` or
`$CLAUDE_CODE_SESSION_ID` exactly. A missing or ambiguous transcript is an
error; never choose the newest file in a shared workspace.
