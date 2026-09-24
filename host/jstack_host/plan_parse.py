"""Turn an approved plan's markdown into stages, each carrying its own proof.

On 2026-09-24 a plan shipped 8 commits to `main` on zero receipts. Item 8 of its
`## Verification` section read "The merge command refuses on a red receipt and
merges on a green one." Every numbered *Phase* got a dispatched agent, because a
phase number was what the dispatcher keyed on; the Verification section had no
phase number, so it had no owner. Nothing was lost. Nothing was *assigned*. A
proof cannot sit outside the thing it proves, so a stage is a heading and its
`Verify:` line and there is nowhere else to put one.

**Pure.** No I/O, no imports from the rest of the package: the caller is a hook
deciding whether to refuse a plan, and this is the piece that has to be testable
from a string. It never raises either — a traceback out of a hook reads as a
broken tool rather than as a broken plan. Everything wrong comes back in
`problems`, worded so the hook prints an entry verbatim and the author knows
what to fix. Liberal in what it accepts (the 11 plans on disk used six different
words for one concept), exact in what it emits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["KINDS", "Stage", "ParsedPlan", "parse", "is_plan"]

#: The declared proof kinds. This tuple *is* the severity dial, which is why
#: `none` is in it: a button tint that declares `none` and a release-system
#: refactor that declares `command` run through the same procedure, and the
#: bureaucracy scales with the change instead of taxing the one-line tweak.
#:
#: `command` — run it, exit 0 is the proof.  `commit` — the sha of the stage's
#: own commits.  `artifact` — a path.  `manual` — waits for the user.
#: `none` — closes on assertion.
KINDS = ("command", "commit", "artifact", "manual", "none")

#: Kinds that are meaningless without a spec. `commit` is not here: the sha
#: comes from the stage's commits, so a spec is an optional narrowing.
_SPEC_REQUIRED = frozenset({"command"})

# Up to three leading spaces is a heading in CommonMark, and plans get pasted
# out of editors that indent. The trailing `#`s are ATX closing syntax.
_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")

# `Stage 4 — the format`, `Stage 4: the format`, `Stage 4 - the format`. The
# number is optional and advisory; `\b` after `stage` keeps "Stages of work"
# and "Staging" out.
_STAGE_HEADING = re.compile(r"^stage\b[ \t]*(\d+)?[ \t]*(?:[—–:-][ \t]*)?(.*)$", re.IGNORECASE)

# The proof line, as authors actually write it: bulleted, blockquoted, bolded,
# or bare. Everything up to and including the colon is ceremony.
_VERIFY = re.compile(r"^[ \t>]*(?:[-*+][ \t]+)?(?:\*\*|__|\*|_|`)*[ \t]*verify[ \t]*:[ \t]*", re.IGNORECASE)

# `<kind>` then an optional separator then the rest. The kind is taken as the
# first word rather than by splitting on the separator first, so a spec that
# contains `--` cannot be mistaken for the separator that introduces it.
#
# Every dash the heading accepts is accepted here too. An author whose stage
# headings all read `Stage 4 — …` reaches for the same em dash on the Verify
# line, and a separator that leaked into the spec would be *shelled*: the stage
# runs `— ./verify/build.sh`, comes back nonzero, and the author is told their
# verification failed rather than that their line is malformed. A bare `-` is
# only a separator when whitespace follows it, so a spec opening on a flag
# survives.
_VERIFY_BODY = re.compile(
    r"^([A-Za-z][\w]*)[ \t]*(?:(?:·|\||--|—|–|:)[ \t]*|-[ \t]+)?(.*)$"
)

# A code span at the head of the spec is the spec; the em-dashed sentence these
# plans trail it with is commentary. `command` means something gets executed,
# and executing the commentary is not a failure anyone would debug quickly.
_LEADING_CODE_SPAN = re.compile(r"^`([^`]+)`")

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")

# A section named for verification rather than for a deliverable. Reported, not
# read — see rule 2 in `plugins/jstack/rules-stage/execution-gates.md`.
_ORPHAN_VERIFICATION = re.compile(r"^verif(?:y|ication)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Stage:
    """One row of the plan: a deliverable and the proof that closes it.

    `ordinal` is the 1-based position in document order, never the number the
    author typed. Source numbering drifts the moment a stage is inserted, and a
    plan whose rows renumber themselves under an editing pass is a plan whose
    stage ids stop matching the receipts already filed against them.
    """

    ordinal: int
    title: str
    verify_kind: str
    verify_spec: str
    body: str


@dataclass(frozen=True)
class ParsedPlan:
    title: str = ""
    stages: list[Stage] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _label(ordinal: int, title: str) -> str:
    """How a problem names the stage it is about — position first, then the
    author's own words, because the number in their heading may be the thing
    that is wrong."""
    return f'Stage {ordinal} "{title}"' if title else f"Stage {ordinal}"


def _fence_mask(lines: list[str]) -> list[bool]:
    """Which lines sit inside a fenced code block.

    The contract's own example is a fenced `## Stage 1 — …` block inside the
    plan that defines the contract. Parsing that example as a stage would give
    every real stage after it an off-by-one ordinal and a numbering complaint,
    which is a parser inventing work out of documentation.
    """
    mask = [False] * len(lines)
    marker = ""
    for i, line in enumerate(lines):
        m = _FENCE.match(line)
        if marker:
            mask[i] = True
            if m and m.group(1)[0] == marker[0] and len(m.group(1)) >= len(marker):
                marker = ""
        elif m:
            marker = m.group(1)
            mask[i] = True
    return mask


def _split_verify(rest: str) -> tuple[str, str]:
    """`command · ./verify/x.sh` → `("command", "./verify/x.sh")`."""
    rest = rest.replace("**", "").replace("__", "").strip().strip("*_").strip()
    m = _VERIFY_BODY.match(rest)
    if not m:
        return "", ""
    kind = m.group(1).lower()
    spec = m.group(2).strip()
    span = _LEADING_CODE_SPAN.match(spec)
    if span:
        spec = span.group(1).strip()
    return kind, spec


def parse(markdown: str) -> ParsedPlan:
    """Read a plan. Returns what it found and what is wrong with it."""
    lines = (markdown or "").splitlines()
    fenced = _fence_mask(lines)

    doc_title = ""
    problems: list[str] = []
    # (level, title, declared_number, body_start) for each stage heading, plus
    # the sentinel heading that ends the last stage's body.
    found: list[tuple[int, str, int | None, int]] = []
    boundaries: list[tuple[int, int]] = []  # (line index, level) of every heading

    for i, line in enumerate(lines):
        if fenced[i]:
            continue
        h = _HEADING.match(line)
        if not h:
            continue
        level, text = len(h.group(1)), h.group(2).strip()
        boundaries.append((i, level))
        if level == 1:
            if not doc_title:
                doc_title = text
            if _STAGE_HEADING.match(text):
                # Silently dropping this is the 2026-09-24 failure in miniature:
                # something that reads like work, owned by nobody.
                problems.append(
                    f'"# {text}" is an H1, and H1s are structure, not stages. '
                    "Make it `## Stage <N> — <deliverable>` if it is work, or "
                    "rename it if it is a part heading."
                )
            continue
        s = _STAGE_HEADING.match(text)
        if s:
            declared = int(s.group(1)) if s.group(1) else None
            found.append((level, s.group(2).strip(), declared, i + 1))
        elif _ORPHAN_VERIFICATION.match(text):
            problems.append(
                f'"## {text}" is not a stage and is not read. Move each of its '
                "items onto the `Verify:` line of the stage that owns it — an "
                "unowned verification checklist is the exact shape that shipped "
                "8 commits on zero receipts."
            )

    stages: list[Stage] = []
    for ordinal, (level, title, declared, start) in enumerate(found, start=1):
        # A stage ends at the next heading that is at least as senior as its
        # own: sub-headings inside a stage are its prose, not the next stage.
        end = len(lines)
        for idx, lvl in boundaries:
            if idx >= start and lvl <= level:
                end = idx
                break

        kind, spec = "", ""
        body_lines: list[str] = []
        verify_seen = False
        for i in range(start, end):
            v = None if verify_seen or fenced[i] else _VERIFY.match(lines[i])
            if v:
                # Searched across the whole stage, not just the first non-blank
                # line: every real plan on disk states the proof after the prose
                # that motivates it, and refusing those would make the contract
                # unadoptable for the plans it was written from. First one wins.
                verify_seen = True
                kind, spec = _split_verify(lines[i][v.end():])
                continue
            body_lines.append(lines[i])

        if not verify_seen:
            problems.append(
                f"{_label(ordinal, title)} has no `Verify:` line. Add one under the "
                "heading — `Verify: <kind> · <spec>`, kind one of "
                + ", ".join(KINDS)
                + ". A stage that declares no proof is a stage nobody can close."
            )
        elif kind and kind not in KINDS:
            problems.append(
                f"{_label(ordinal, title)} declares `Verify: {kind}`, which is not a "
                "proof kind. Use " + ", ".join(KINDS[:-1]) + " or none — `none` is the "
                "right answer for genuinely trivial work, but it has to be that word."
            )
            kind = ""
        elif not kind:
            problems.append(
                f"{_label(ordinal, title)} has a `Verify:` line with no kind on it. "
                "Write `Verify: <kind> · <spec>`, kind one of " + ", ".join(KINDS) + "."
            )
        elif kind in _SPEC_REQUIRED and not spec:
            problems.append(
                f"{_label(ordinal, title)} declares `Verify: {kind}` with nothing to "
                f"run. Put it after the separator — `Verify: {kind} · ./verify/thing.sh` "
                "— or declare a kind that needs no spec."
            )

        if declared is not None and declared != ordinal:
            # A warning, never a refusal: the numbers are the author's notes to
            # themselves and the position is what everything downstream keys on.
            problems.append(
                f'{_label(ordinal, title)} is numbered {declared} but is the '
                f"{ordinal}{_ordinal_suffix(ordinal)} stage in the document. Renumber "
                "the headings or drop the numbers — document order is what is used."
            )

        stages.append(
            Stage(
                ordinal=ordinal,
                title=title,
                verify_kind=kind,
                verify_spec=spec,
                body="\n".join(body_lines).strip(),
            )
        )

    return ParsedPlan(title=doc_title, stages=stages, problems=problems)


def _ordinal_suffix(n: int) -> str:
    if 11 <= n % 100 <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def is_plan(parsed: ParsedPlan) -> bool:
    """More than one stage means a plan.

    The trigger is procedure, not size — this is deliberately a count and not a
    judgement about how big the work is, because every judgement about size is
    an argument, and the argument is what got skipped last time.
    """
    return len(parsed.stages) >= 2
