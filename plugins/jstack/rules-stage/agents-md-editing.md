---
paths:
  - "**/AGENTS.md"
  - "**/rules/*.md"
  - "**/commands/*.md"
  - "**/SKILL.md"
---

# Editing AGENTS.md and instruction docs

AGENTS.md files load on every walk-up. Every line you add is paid for in context on every session that touches the cwd. Before adding content, find where it already lives.

Tag the session — `log_event tag set md` — so instruction-doc work reads back as one thread across every seat that touched it.

## One canonical home per procedure

Process lives in exactly one place. AGENTS.md sites point to it; they do not restate it. Concretely:

- Agent autonomous procedure (cron-fired, has a defined contract) → agent-scoped skill at `~/Agents/{agent}/.claude/commands/{skill}.md` (single file: description + Inputs / Outputs / Failure modes / Steps / Edge cases). Cron payload = direct `/skill_name` slash command. Sub-mode AGENTS.md = identity + on-entry + cron schedule + hard rules only.
- Path-scoped technical pattern → `~/.claude/rules/*.md` with `paths:`. AGENTS.md does not duplicate.
- Repeatable workflow invoked from anywhere → global slash command at `~/.claude/commands/{name}.md`.
- Project architecture → that project's own AGENTS.md. Agent AGENTS.md does not restate.
- Owner preference that governs how work is done → the layer of the walk-up that owns it, stated once as law.
- A platform truth or failure mode that cost real time → an on-demand reference file, reached by name when the symptom appears.

If the content already exists upstream, your edit is a one-line pointer — not a copy.

## Memory holds personal things

Auto-memory is for what is personal to the owner and would otherwise be forgotten. Everything else is one of two things: stale, or a rule. A rule belongs in the layer that owns it, where one write reaches every agent through the walk-up; stale gets deleted. A memory that has become law is a memory that has finished its job — move it and drop it.

## Guide, don't restrict

Instructions carry HOW to do the thing. What to do is the task, and it arrives with the work; a doc that enumerates whats becomes a checklist that ages badly and steers the reader away from thinking.

Say what to avoid only broadly, and only where the wrong move is genuinely inviting. A pile of narrow prohibitions reads as distrust and monopolizes attention — the reader spends it on compliance instead of on the problem.

## Examples

A good example is a trap. The reader copies its surface instead of applying the principle, and every output starts to look like the sample. State the principle and let the shape follow from it.

An example of a *failure* is worth including when the failure is counterintuitive — where the wrong move looks correct and the reader cannot get there by reasoning. Describe the failure shape; a reader who has never seen it should recognize it when it appears.

## Principles over templates

Write what to do and why, not the literal output shape. No verbatim report templates, no phase-by-phase walkthroughs, no agent-specific concrete examples pasted into another agent's playbook. A reader who knows the principle should be able to produce the correct shape; if they need a template, the template belongs in the canonical home, not here.

## Instruct, don't explain

Write the instruction. Leave out the reasoning that produced it, the cost of ignoring it, and the failure it prevents. A why earns its line only where the instruction reads as arbitrary and a reader without it will override the rule; elsewhere it doubles the length and changes nothing the reader does.

## A repeated correction is a binding bug

When the same correction has to be given twice, the wording is rarely the problem. The law exists in a layer that does not load for that session type, or a broader line nearby steers the opposite way. Fix the binding — move the law up a layer, or code the seam so the wrong move is not reachable. Rewriting the sentence more precisely buys wording-level compliance and nothing else.

## Laws are timeless — no provenance in instruction text

An instruction doc states the rule and how to comply — nothing else.

- **No date stamps, no "(Owner YYYY-MM-DD)" attributions, no quoted chat.** Git holds who and when; the doc holds only the law. A date is content only when the reader needs it to act (a deadline, a schedule).
- **Corrections refine the existing line in place** — never appended as dated changelog or diary entries. A "log" section, where structure requires one, is a pointer to git, not an accumulator.
- **Removals are silent.** When a law dies, delete it and every row/pointer that referenced it, everywhere — never write "X was removed/killed/reversed" in any doc or index. The next reader has no memory of the old law; annotating an absence is noise. The session timeline and git hold the event. A dated-row index (a corrections ledger, an INDEX table) is not an exemption — a row lives exactly as long as the rule it maps to.
- **Describe the failure shape, never the episode.** "Ratio thinking turns one emoji into a stamp across a batch" teaches; a dated account of who caught it where is noise that ages.
- **Shared docs stay instance-agnostic.** A shared or template doc never hardcodes one account's, product's, or agent's name where the law is general — state it generically once; instance files carry only what is genuinely theirs. In a skill the reader's owner is `the user` — never a name, a title, or a pronoun standing in for one.

## After writing — reread as a cold reader

Reread the final file top to bottom before moving on: does any line carry provenance, another instance's name, template leftovers, or a duplicate of a law stated elsewhere? The writer's review is part of the write, not the owner's job.

## Before editing — verify, don't assume

1. Grep for the procedure / duty / format you're about to write. If it exists elsewhere, you're pointing at it, not restating it.
2. Read the file you're editing in full. Don't add a section that contradicts or duplicates one already there.
3. If a migration changes who-does-what, check every audit signal that depends on the old contract (cron payloads, meta-review checks, tests) and update them together. A one-side edit is a contract drift.
4. **Changing a law means finding every place it is stated, hardest first.** A law that has been live a while exists at several strengths — the decisive statement, a restatement further down, a worked example, a counter-set in a reference file, the shared skill that repeats it for every instance. Edit whichever one you happened to be reading and the strongest copy stands, so nothing changes while the diff says it did. Grep the law's own words rather than the heading, then fix the copy a reader reaches first and the copy written most absolutely.
5. **A doc describing a coded gate is checked against that gate.** Prose drifts wider than the code it claims to describe — it says the check reads intent where the check counts markers — and readers obey the prose over the code. State what the implementation actually reads, and what it does not.

## Bloat ceiling

Anthropic spec — each AGENTS.md ≤ 200 lines; walk-up total (org root + agent root + sub-mode) ≤ 400. A skill's SKILL.md stays under 1,000 tokens (~4,000 chars) and aims well below it; the file is read whole on every invocation, so it carries the gate and the exact command, and anything a reader needs only sometimes goes in a sibling reference file the skill names. Going over isn't "needs trimming later" — it's a failed edit. Trim now or pick a different destination.

What makes an instruction doc long is almost never the law; it is the same law stated three times at three strengths — the rule, then a table restating it, then a DO-NOT list restating it again. Say it once, at full force, in the place the reader hits first. A prohibition that only echoes a rule already stated above earns nothing and costs the reader's attention.

## Tone

Tight, conversational, declarative. No filler. No emojis. Dates as `YYYY-MM-DD`. Code/paths in backticks. Tables only when comparing ≥3 things across the same dimensions; otherwise prose is denser.
