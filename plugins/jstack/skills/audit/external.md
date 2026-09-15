# External mode

Replaces steps 2–4: the auditor reports its verdict to the user in its own terminal, with full context separation.

Write the brief as in step 1, then show it and wait a beat for the user to correct a wrong premise before opening the terminal.

Use the bundled adapter's briefing and kickoff options, which preserve both
providers' argument quoting. It selects Codex when `CODEX_THREAD_ID` is set:

```bash
AUDIT_TMP="$(mktemp -t jstack-audit)"
cp "$TARGET_CWD/audit-brief.md" "$AUDIT_TMP"
"${CLAUDE_PLUGIN_ROOT}/bin/open-terminal-here" "$TARGET_CWD" \
  --prompt-file "$AUDIT_TMP" --name "Audit" \
  --first-prompt "Audit session. Read the attached Audit Protocol and Audit Brief. Verify the claims and report your verdict here. Ask only if blocked."
```

Nonzero exit means no auditor started. Give the user the brief's absolute path
and launch error. Do not report an independent verdict before an auditor runs.

The brief lives in the target workspace, so each agent keeps its own and the new session loads the CLAUDE.md walk-up from there.
