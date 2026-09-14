---
paths:
  - "**/hooks/**"
  - "**/plugins/**"
---

# Injected Text Is File-Sourced

Any model-bound text longer than one sentence — a briefing, a reminder, a preamble, a stage prompt, anything a hook or a spawn puts into a session's context — lives in its own file. Code loads it; code never holds it.

- jStack plugin: `plugins/jstack/prompts/*.md`, loaded through `hooks/_prompts.py` — a whole file, or one `## section` of it.
- Runtime values enter as `{placeholders}` filled with `.format()`. Compute plurals and conditionals in code and pass the result in — the file holds prose, never expressions.
- A single sentence (an error line, a one-line nudge) may stay inline. Anything longer moves.

The reason is operational, not aesthetic: prose buried in code is invisible to the people managing the prompts, drifts without review, and every feature that inlines it rebuilds the same plumbing. A file has an owner, a diff, and a place.
