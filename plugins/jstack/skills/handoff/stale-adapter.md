# Launching through an older adapter

A stale PATH can resolve an `open-terminal-here` that predates `--prompt-file`,
`--name` or `--first-prompt`. Such an adapter forwards what it does not parse to
the provider, which dies on the unknown flag — so pass only what its usage
advertises:

```bash
USAGE="$(open-terminal-here 2>&1)"
GO=(); [[ -z "$STAGE" && "$USAGE" == *--first-prompt* ]] && GO=(--first-prompt "[system prompt] begin working")
if [[ "$USAGE" == *--prompt-file* ]]; then
  open-terminal-here "$TARGET_CWD" --prompt-file "$HANDOFF_TMP" --name "$TITLE" "${GO[@]}"
else
  SAFE_TITLE="${TITLE// · /·}"; SAFE_TITLE="${SAFE_TITLE// /-}"
  open-terminal-here "$TARGET_CWD" --append-system-prompt-file "$HANDOFF_TMP" --name "$SAFE_TITLE"
fi
```

`STAGE` is set when the user passed `--stage`. The fallback branch uses a flag
every version forwards verbatim and needs a single-token title, since
pass-through adapters do not re-quote it; the temp file then lingers in `/tmp`,
never in the workspace. An adapter without `--first-prompt` opens the session
waiting even without `--stage` — say so in the report, so the user knows to
type to start it.
