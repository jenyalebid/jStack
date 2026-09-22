# Transcript Dialogue — Architecture & Extension Guide

PreToolUse hook that answers a `Read` of a session transcript with speech only — what the user said and what the agent said — for both Claude session files and Codex rollouts.

This doc is the canonical reference. Read it before extending the strip list, adding an engine, or changing what counts as speech.

## Why the hook exists

A session `.jsonl` is an append-only record of everything a session *did*, not what was *said* in it. Every tool call, every tool result, every reasoning block, every file-history snapshot, every injected rule and `<system-reminder>` is a line in that file. Speech is a rounding error inside it:

| transcript | raw | dialogue |
|---|---|---|
| Claude session (43 records) | 355 KB | 4.0 KB |
| Codex rollout | 832 KB | 1.7 KB |

So a plain `Read` of one spends most of a context window on text nobody said — and worse, the machinery that lands in context then colors every answer that follows. Injected rule bodies read as instructions. Old tool output reads as current state.

## Why it is a hook and not a rule

This was already a rule. `rules-stage/claude-sessions.md` said *never read raw session JSONL directly* and offered a `jq` filter instead. It was routinely ignored, for a structural reason: the model is handed a path and reading it is the obvious move, and every obvious move that has to be suppressed by instruction will eventually be made. The rule's own filter was also leaky — it passed `<system-reminder>` blocks straight through, so following it exactly *still* pulled machinery into context.

So the reader changed instead of the instruction. The raw `Read` is denied and the dialogue is returned in its place: the obvious move now produces the right result, and no discipline is required for it to keep doing so. The rule remains, demoted to describing what the hook does.

## What it intercepts

`tool_name == "Read"`, on a path that `is_transcript()` accepts:

- `~/.claude/projects/<slug>/<uuid>.jsonl` — a Claude session file, matched by path shape.
- `rollout-*.jsonl` — a Codex rollout, matched by filename.
- any other `.jsonl` carrying a `session_meta` record — a rollout somewhere unusual.

Everything else exits 0 immediately with no output. Deliberate raw access is untouched: `jq`, `grep`, `cat` and every other Bash path over the same file work exactly as before. This hook owns the *default*, not the file.

## stdin contract

```json
{
  "tool_name": "Read",
  "tool_input": { "file_path": "/Users/.../<uuid>.jsonl" }
}
```

On a match it emits a `deny` with the dialogue in `permissionDecisionReason`:

```json
{"hookSpecificOutput": {
  "hookEventName": "PreToolUse",
  "permissionDecision": "deny",
  "permissionDecisionReason": "A session transcript is never read whole — …\n\n[dialogue only — N turns]\n\n── user …"
}}
```

The reason text states plainly that this *is* the read, so the denial is not retried and does not get routed around with `cat`.

## What counts as speech

Extraction lives in `session_runtime.py`, alongside the existing `user_text()`, so both engines normalize in the one provider-neutral module rather than growing a second parser.

- `user_text(row)` / `assistant_text(row)` — pull `text` and `input_text`/`output_text` blocks only. Reasoning, `tool_use` and `tool_result` blocks are never text blocks, so they are excluded by construction rather than by blocklist.
- `dialogue(path)` — walks the file, drops sidechain rows and anything carrying `toolUseResult` (Claude delivers tool results as `type: "user"`, which is the one place machinery wears speech's clothing).
- `strip_injected(text)` — removes harness-written wrappers from *inside* an otherwise-real turn: `<system-reminder>`, `<persisted-output>`, `<jstack-timeline>`, `<local-command-stdout>`, `<command-message>`, `<command-args>`, `<user-prompt-submit-hook>`, `<environment_context>`, `<permissions instructions>`.
- `_NOT_SPEECH` — drops a turn that is *only* machine opening (`<command-name>`, the caveat banner, an interrupt marker).

The distinction that matters: a wrapper is stripped and **what surrounds it survives**. A prompt the harness wrote into is still a prompt. Dropping the whole turn because it contains an injection would silently eat real questions — which is exactly what the old `jq` filter's inverse mistake looked like from the other side.

## Failure behavior

It fails **open**, always. An unparseable file, a missing module, an exception anywhere in extraction, or a transcript with nothing said in it — all exit 0 and let the real `Read` proceed. Failing open costs a context window; failing closed costs the user a file they explicitly asked for, with no way to tell why.

Output over `MAX_CHARS` (60 000) is truncated with a pointer to the `--tail` invocation, so a very long session degrades to *recent speech* rather than to a blocked read.

## Standalone use

```bash
python3 plugins/jstack/session_runtime.py dialogue <file.jsonl> [--tail N]
```

Same rendering, no hook involved — useful for reviewing a finished session, or from a non-Claude engine.

## Extending

- **A new injected wrapper** — add its pattern to `_INJECTED` in `session_runtime.py`. Patterns are `re.S` and must be non-greedy so two wrappers in one turn do not swallow the speech between them.
- **A new engine** — teach `is_transcript()` to recognize it and `user_text()`/`assistant_text()` to read its message shape. Nothing else in the hook is engine-aware.
- **Never** widen `is_transcript()` to all `.jsonl`. Log files, datasets and event streams are line-delimited JSON too, and hijacking a read of one would be a silent data loss.

## Test

`tests/read-transcript.sh` — pipes fixture PreToolUse payloads through the real hook. Asserts passthrough for ordinary files, non-Read tools and unrelated `.jsonl`; denial with both sides' speech for both engines; that no tool call, tool result, thinking, meta, sidechain, snapshot or injected text survives; that a stripped wrapper leaves its surrounding prompt intact; and that an unparseable transcript falls through to the real read.
