---
name: handoff
description: Use when the user asks to hand this session off to a fresh terminal or to another agent.
argument-hint: "[@agent[-seat]] [focus]"
---

# /jstack:handoff

Opens a fresh session in the current provider with this session's actionable state. Target instructions establish identity; the doc carries context. In Codex, use [the native launcher](codex.md) for step 3.

Arguments, both optional. `@agent` retargets the workspace: `seat @alice-social` prints the directory to boot in, and `seat --list` every address there is. That is the one address grammar, shared with mail and the scheduler — `@agent` alone is the cockpit, hyphens walk down the seat tree (`@alice-social-threads`), a seat holding its own `chat/` resolves to that operator seat. A miss exits non-zero naming the seats that exist; stop there, never join a path blind. The seat is the user's to name and not yours to infer from the focus: a sub-mode chosen by reading the wording lands the same command in different seats on different phrasings, and path-scoped rules load from whichever tree it picked. No `@agent` → the current cwd. Everything after the agent token is a **focus** that scopes the doc; it is an explicit narrowing instruction, so drop unrelated tangents rather than balancing them.

## 1. Write the doc

Actionable state, not history. Specific: file paths, function names, line numbers, branch names, exact state. Skip what the next session derives from CLAUDE.md or the code. Under 200 lines — it rides in system-prompt space.

Sections: **Current Work** (the immediate task) · **In Progress** (partial implementations, uncommitted changes, pending decisions) · **Still To Do** · **Key Decisions** the next session must respect · **Context** — blockers and gotchas.

With `@agent`, write it *for that agent*: address their role and what they own, name who is handing off and why, mark which decisions came from the user and are not open to relitigation, and drop what only mattered to your own duties.

## 2. Stage it outside the tree

The doc is a one-shot payload — it must not land in any workspace:

```bash
HANDOFF_TMP="$(mktemp -t jstack-handoff)" && rm -f "$HANDOFF_TMP"
```

The `rm` is deliberate: an existing-but-unread file trips the Write tool's read-before-write guard, while the deleted name stays collision-safe. Write the doc there with the Write tool, then show it to the user.

## 3. Open the session

`--name` is required on every invocation — a handoff terminal without an `HF ·` title is a failed handoff. Plain handoff → `HF · <topic>`; `@agent` → `HF→<Agent> · <topic>`. Derive `<topic>` in 1–3 words from the doc you just wrote, never from the focus argument or the user's phrasing.

`--prompt-file` and `--name` are adapter options. A stale PATH can resolve an older `open-terminal-here` that forwards them to `claude`, which dies on the unknown flag — so detect the contract rather than assuming it:

```bash
if open-terminal-here 2>&1 | grep -q -- '--prompt-file'; then
  open-terminal-here "$TARGET_CWD" --prompt-file "$HANDOFF_TMP" --name "$TITLE"
else
  SAFE_TITLE="${TITLE// · /·}"; SAFE_TITLE="${SAFE_TITLE// /-}"
  open-terminal-here "$TARGET_CWD" --append-system-prompt-file "$HANDOFF_TMP" --name "$SAFE_TITLE"
fi
```

The first branch inlines the briefing and deletes the temp file before Claude starts. The fallback uses a flag every version forwards verbatim and needs a single-token title, since pass-through adapters do not re-quote it; the temp file then lingers in `/tmp`, never in the workspace.

The adapter self-detects the terminal. Nonzero exit means none started — give the user `$HANDOFF_TMP` and tell them to open a session in the target workspace with `--append-system-prompt-file`.
