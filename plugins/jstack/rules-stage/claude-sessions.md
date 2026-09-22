---
paths:
  - ".claude/projects/**"
---

# Claude Session Files

**Never read raw session JSONL directly** — transcript files are large and mostly token noise; a full read can blow the context window.

## Working with transcripts

- **This session's own transcript path**: `/jstack:print` emits it.
- **Dialogue is the default read.** A `Read` of a session transcript — Claude session file
  or Codex rollout — is intercepted by `hooks/pretooluse-read-transcript.py` and answered
  with speech only: what the user said and what the agent said, with tool calls, tool
  results, thinking, system-reminders and skill injections stripped. Just Read the file.
  Standalone: `session_runtime.py dialogue <file.jsonl> [--tail N]`.
- **Anything else: extract, don't read.** Reach for `jq` only when you need a specific
  non-speech record — a tool result, a timestamp, a hook error. Note that the naive filter
  below still carries injected `<system-reminder>` blocks inside user turns:

```bash
jq -r 'select(.type == "user" or .type == "assistant") | .message.content | if type == "array" then .[] | select(.type == "text") | .text else . end' <session>.jsonl | tail -50
```

- **Counts / shape first**: `wc -l`, `jq -r .type | sort | uniq -c` — decide what to extract before extracting.
- Reviewing a finished session is what the jstack session-review engine and `/jstack:post-session-review <session-id>` are for — prefer them over hand-parsing.
