---
name: work
description: Use when the user says "let's work on X", or otherwise wants you prepared on an area before the work starts.
argument-hint: "[@project] <topic>"
---

# /jstack:work

Preparation, not execution: load the right tools, read what recently changed, read the core files, and form a grounded read of the state. End ready to execute, having changed no code and opened no PR.

Who you are, what you own and your stack come from the CLAUDE.md walk-up already loaded. Nothing here is looked up in a project registry.

`$ARGUMENTS` is `[@project] <topic>`. An `@`-prefixed first token targets a project explicitly: match it case-insensitively against the repo directories under `${user_config.repo_root}` (default: the parent of `${user_config.agent_root}`), and list the candidates and stop if nothing matches. Without it, use your own project — the one your identity establishes; where you own several and the topic does not settle which, infer from the topic and ask one short question only if it stays genuinely unclear. Everything after that is the **topic**, free text naming the area, and it drives both which skills you load and which code you survey.

## 1. Orient

`date`. Your seat's recent timeline entries are injected on entry; more history is `log_event tail <agent>/<submode> -n 20`.

## 2. Resolve the project and its stack

End this step knowing the exact repo directory. On an `@project` override, sniff to confirm the stack: `*.xcodeproj` or `Package.swift` → Swift/iOS, `package.json` → web/Node, `Cargo.toml` → Rust, `pyproject.toml` or `requirements.txt` → Python.

## 3. Load every relevant skill

Match stack plus topic against the available skill descriptions. Use the `Skill` tool in Claude; in Codex, read each selected `SKILL.md` from its catalog path. Load every applicable skill and announce each as it loads.

## 4. Survey the area

```bash
cd <repo> && git log --oneline -8 && echo "---" && git status --short
```

Then find and grep for the topic's keywords, and read the recent commits that touched those paths (`git log -p -- <paths>`) so you know what changed and why.

## 5. Read the core files

Open what the survey surfaced as central, plus the project's `CLAUDE.md` or any nearby design doc for the area.

## 6. Report

- **Project, repo, stack** as resolved
- **Skills loaded** — one line each on why
- **Recent changes** — what the last relevant commits did
- **Core files** read, and what they are
- **Your read** of the current state and where this topic's work would land
