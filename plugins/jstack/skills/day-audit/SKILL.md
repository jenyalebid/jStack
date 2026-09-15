---
name: day-audit
description: Use when the user asks for a day audit — "audit yesterday", "did anything we shipped break".
argument-hint: "[today|yesterday|<date>]"
---

# /jstack:day-audit

The timeline is a claims document. It says where to look, never what is true — every verdict comes from the diffs. You discover, fan out, and synthesize; each repo's audit goes to the agent that owns it, so the review carries that codebase's domain context.

Report-only on code. The single write is one timeline block.

## 1. Resolve the day and load its claims

```bash
DAY=$(python3 "${CLAUDE_PLUGIN_ROOT}/skills/day-audit/resolve-day.py" "$ARGUMENTS") || exit 1
NOW=$(date +%H:%M)
log_event recall "$DAY" 2>/dev/null || echo "(no timeline for $DAY)"
```

Unparseable date → ask the user for it and stop. Keep the full timeline text: each repo auditor gets it as grounding, and synthesis needs it to find claim↔commit gaps. An empty timeline is fine — the audit then runs purely off commits.

## 2. Resolve repos and owners

```bash
AGENT_ROOT="${user_config.agent_root}"; AGENT_ROOT="${AGENT_ROOT/#\~/$HOME}"
REPO_ROOT="${user_config.repo_root}"; [ -z "$REPO_ROOT" ] && REPO_ROOT="$(dirname "$AGENT_ROOT")"; REPO_ROOT="${REPO_ROOT/#\~/$HOME}"
REGISTRY="${user_config.agent_registry}"; [ -z "$REGISTRY" ] && REGISTRY="$AGENT_ROOT/agents.json"; REGISTRY="${REGISTRY/#\~/$HOME}"
python3 "${CLAUDE_PLUGIN_ROOT}/skills/day-audit/resolve-repos.py" "$AGENT_ROOT" "$REPO_ROOT" "$REGISTRY"
```

One line per repo: `agent⇥workspace⇥repo_path`, agent `-` when unowned. Nothing printed → tell the user the root resolved to `$REPO_ROOT` and holds no git repo, then stop. An unowned repo still gets audited, generically.

## 3. Pull the day's commits

Commit date, all refs — work lands on develop and feature branches, not only `main`:

```bash
git -C "$R" log --all --no-merges \
  --since="${DAY}T00:00:00" --until="${DAY}T23:59:59" \
  --date=local --pretty=format:'%h %an %s' 2>/dev/null
```

Drop repos with no commits. Every repo empty → skip to step 6.

## 4. Fan out, one auditor per repo

Dispatch one auditor per repo using Claude's Agent tool or Codex's `collaboration.spawn_agent`. Respect the user's delegation constraints; if agents are prohibited, verify locally and label it self-verification. The kickoff prompt: `${CLAUDE_PLUGIN_ROOT}/skills/day-audit/auditor-prompt.md`.

## 5. Synthesize

Re-verify every high-severity finding yourself from source before repeating it — auditors are input, not verdict. Then: per app `IMPROVED` / `NEUTRAL` / `REGRESSED` with the one or two findings that drove it; spine gaps in both directions (claimed but never committed, landed but never claimed); and a lead line saying net improvement or what regressed.

## 6. Write the audit block

One block per audited day, replaced on re-run through the pipeline-task tag. `--at` is now, `--date` is the audited day. Headline ≤120 chars, present tense, no hashes or paths; 0–3 details at ≤80 chars.

```bash
log_event day-audit --at "$NOW" --date "$DAY" --pipeline-task day-audit#"$DAY" \
  "Day audit: {N} repos with commits — {clean | M regressed / risky}" \
  --detail "{app}: {IMPROVED|NEUTRAL|REGRESSED} — {driver}" \
  --detail "{the single most important finding, or a claim↔commit gap}"
```

No commits anywhere → one line: `"Day audit: no commits across {N} repos on {DAY}"`.

## 7. Report

- **Verdict** — one line: net-better, or N apps regressed / risky
- **Per app** — `IMPROVED` / `NEUTRAL` / `REGRESSED`, the driving finding, `file:line`
- **Spine gaps** — claimed but not committed, committed but unclaimed
- **Needs you** — judgment calls unsettled from source, as direct questions
