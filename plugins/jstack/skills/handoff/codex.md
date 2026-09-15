# Native handoff launch

Use the bundled adapter, which supports Codex's native briefing and managed
identity routing. Keep the doc and title rules from SKILL.md.

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/open-terminal-here" "$TARGET_CWD" \
  --engine codex --prompt-file "$HANDOFF_TMP" --name "$TITLE"
```

The adapter inlines the doc as Codex developer instructions and removes the
temporary file. A nonzero exit means no session started; report the retained
doc path and the launch error. Never forward Claude-only flags to Codex.
