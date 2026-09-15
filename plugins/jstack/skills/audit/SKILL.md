---
name: audit
description: Use when the user asks to audit, double-check, or get a second opinion on this session's work.
argument-hint: "[focus] [@agent] [internal|external]"
---

# /jstack:audit

The brief is a claims document, not a briefing: what this session says it did, handed to an auditor with fresh eyes and no investment in it being right.

Arguments, order-agnostic, all optional. `internal` (default) runs the auditor as a subagent and triages its findings here; `external` opens a separate terminal instead — `${CLAUDE_PLUGIN_ROOT}/skills/audit/external.md`. `@agent` runs under another agent's identity: match the `@`-name case-insensitively against subdirectories of `${user_config.agent_root}`, target its `chat/` subdir if present else the agent root, and stop with the directory list if nothing matches. Anything else is a **focus** — narrow the brief to it and go deep. No `@agent` → target cwd is the current cwd.

## 1. Write the brief

You are the session under audit, so write it honestly: overselling what you verified, omitting a shortcut, or softening a user constraint is exposed anyway, since every claim gets checked from source.

Seed the file with the protocol and its section headings, then fill the sections in place:

```bash
trash "$TARGET_CWD/audit-brief.md" 2>/dev/null || rm -f "$TARGET_CWD/audit-brief.md"
cp "${CLAUDE_PLUGIN_ROOT}/skills/audit/protocol.md" "$TARGET_CWD/audit-brief.md"
```

Absolute paths throughout — the auditor may boot elsewhere. Name the repo, branch and commit range (or "uncommitted working tree") so it can derive the real diff. **Caution Flags is mandatory and verbatim**: every constraint the user stated, in their words; if none, "None stated by user" plus your own read of the riskiest thing you could have broken. Keep the file under 150 lines.

Show the user the brief sections, not the protocol, so a wrong premise can be corrected. Proceed without waiting — triage catches a premise that is slightly off.

## 2. Dispatch

Use Claude's Agent tool or Codex's `collaboration.spawn_agent` for one auditor. A large diff may be split among auditors by correctness, blast radius and Caution Flags. Respect the user's delegation constraints; if agents are prohibited, verify locally and label it self-verification.

> Read `{TARGET_CWD}/audit-brief.md` in full and follow the Audit Protocol at its
> head exactly. Return ONLY the findings list in the verdict format that protocol
> specifies. No narrative, no recap of the brief.

## 3. Triage each finding

Trust the auditor no more than it trusted the brief. Re-verify every finding independently from source — open the file, run the command, read the consumer — then land it in exactly one bucket:

- **REAL** — your own check confirms it. Fix it, justified by your verification and never by "the auditor flagged it", then verify the fix.
- **REFUTED** — your own check shows otherwise. Record the one line of source evidence that refutes it.
- **UNCLEAR** — undeterminable from source, or a judgment call about intent. Surface it as a question; never blind-fix, never silently drop.

No fix on the auditor's word alone. Every finding it raised appears in the report under exactly one bucket. A fix touching a Caution Flag area stops and asks first, even when the issue is real.

## 4. Report

- **Verdict** — one line: clean, or N real / M refuted
- **Fixed** — per issue: what it was, the fix, how the fix was verified, `file:line`
- **Refuted** — one line each: the claim, and why it is not real
- **Needs you** — unclear findings, as direct questions

Leave `audit-brief.md` in place as the record.
