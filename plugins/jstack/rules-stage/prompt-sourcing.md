---
paths:
  - "**/hooks/**"
  - "**/plugins/**"
---

# Injected Text Is File-Sourced

Any model-bound text longer than one sentence — a briefing, a reminder, a preamble, a stage prompt, anything a hook or a spawn puts into a session's context — lives in its own file. Code loads it; code never holds it.

- jStack plugin: `plugins/jstack/prompts/*.md`, loaded through `hooks/_prompts.py` — a whole file, or one `## section` of it.
- jStack host: `host/jstack_host/prompts/*.md`, loaded through `jstack_host/prompt_files.py` the same way. The host ships without the plugin tree, so its text ships beside it.
- Runtime values enter as `{placeholders}` filled with `.format()`. Compute plurals and conditionals in code and pass the result in — the file holds prose, never expressions.
- A single sentence (an error line, a one-line nudge) may stay inline. Anything longer moves.
- Enforced, not trusted: `host/tests/test_prompt_sourcing.py` fails any hook or host module holding a multi-sentence literal. Text only a person at a terminal reads is exempted there by name, with the reason.

## A machine never speaks as the user

An untagged user turn is the person. Anything else that lands in the user's seat — a spawn's first prompt, a nudge typed into a pane, a continue, a wake — opens with `[system prompt]`, the one shared prefix.

- Enforced where text is delivered, not trusted to each sender: the plugin's `open-terminal-here`, the host's `spawn.build_shell_parts`, and each host nudge pass the text through `tagged()` (`hooks/_prompts.py`, `jstack_host/prompt_files.py` — pinned equal by test). It is idempotent, so layers stack nothing; a slash command stays bare, because the CLI only runs one that opens the line.
- A person's own input never passes through it. The composer, the PTY and the phone's turn and new-session text are the user's and stay clean — tagging a delivery path a person also uses would mark the person as a machine.
- Readers drop it: a takeover's user spine and `session_runtime.dialogue` treat a tagged turn as not the user's speech.

The reason is operational, not aesthetic: prose buried in code is invisible to the people managing the prompts, drifts without review, and every feature that inlines it rebuilds the same plumbing. A file has an owner, a diff, and a place.
