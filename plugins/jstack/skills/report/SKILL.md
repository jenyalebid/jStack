---
name: report
description: Use at the end of any task that produced work.
argument-hint: "[draft]"
---

# /jstack:report

**Fixed, or filed.** Every confirmed defect leaves the session carrying a number — a sha or `#N`.

`draft` = triage and show; file and commit nothing.

## 1. Settle every finding

A finding is a confirmed defect. Fix it if the fix is reachable in the tree you are standing in. File only what you did not fix, and put the reason in the body — out of scope, another domain, needs a decision, too big for the turn.

Filing procedure: `${CLAUDE_PLUGIN_ROOT}/skills/report/filing.md`.

Fix written but blocked on a permission you lack: file it, then report it under **Blocker**. In an autonomous session nobody reads the report, so send it live instead:
`ping_boss blocker "SYSTEM FIX BLOCKED — <file> / <cost until it lands> / #<N>"`

Nitpicks, taste calls and unconfirmed suspicions are not findings. No commit, no issue — they go under **Extra**.

## 2. Commit

Run `/jstack:push`. Name the split first: side fixes as their own units before the main work, the deliverable next, generated artifacts last.

## 3. Report

The user skims. Declarative, no narration, no filler — full sentences, short ones. Include only blocks that have content; an empty block is noise and an invented one is worse.

- `Question` — one per question the user asked, answered under it
- `Blocker` — work stopped. What is needed, and from whom
- `Issues` — `#N — title`
- `Fixed` — `sha — what`, for repairs beyond the ask
- `Done` — the result of the request
- `Not Done` — each line carries `#N` or a blocker
- `Extra` — one line each, no numbers
- `Next Move` — what happens next

Read it back. Every **Issues** line carries `#N`, every **Fixed** line carries a sha, no finding sits in another block's prose. Nothing found is a normal outcome — send `Done` alone.

Asked for it spoken instead — one paragraph, nothing in it to open — that is `/jstack:elevator`, which settles the same way first.
