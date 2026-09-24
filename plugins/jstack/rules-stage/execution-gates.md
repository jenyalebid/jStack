---
paths:
  - "**/plans/**"
  - "**/*plan*.md"
---

# Plan format — stages and their proofs

**More than one stage means a plan.** Procedure, not size: a button tint and a
release-system refactor both run through this. What scales between them is the
proof kind each stage declares.

One sequencing word: **Stage**. Not phase, step, part, milestone or section.

```markdown
## Stage 1 — install and update build from a ref
Verify: command · ./verify/build-from-ref.sh
<prose: what this stage delivers>
```

- The heading is `## Stage <N> — <deliverable>`. Write the em dash; the parser
  also takes `–`, `-` and `Stage N:`.
- `Verify:` is one line, `<kind>` or `<kind> · <spec>`. Write `·`; `|` and `--`
  are also read.
- Numbering is advisory. Document order is the stage's identity; a number that
  disagrees is reported and ignored.
- `# Part one — …` H1s are structure. A stage never lives at H1.

## Proof kinds

| Kind | Spec | Closes on |
|---|---|---|
| `command` | **required** | the command exiting 0 |
| `commit` | optional | a sha from the stage's own commits |
| `artifact` | a path | that file existing |
| `manual` | what to look at | the user saying so |
| `none` | — | assertion |

`none` is a real answer. It is how the verification bureaucracy scales with the
severity of the change instead of taxing a one-line UI tweak.

## Three rules, each a 2026-09-24 failure written down

1. **A headline names its deliverable, never its epoch.** The merge gate died
   because it lived under "the last two releases" while releases were being
   deleted. A heading naming something being removed takes its contents with it.
2. **Verification is not a trailing section.** Every proof belongs to a stage's
   `Verify:` line. A plan with an orphan `## Verification` checklist is the exact
   shape that shipped 8 commits on zero receipts; the parser does not read one.
3. **A blocked stage surrenders its contents.** Blocking touches that stage
   only. Nothing rides a dead heading into the ground.
