"""The plan parser — the piece that decides whether a plan has owners.

Two halves, for two different fears.

The regression half runs every plan on this machine's `~/.claude/plans` through
`parse()`. Those files predate the contract and most of them will never yield a
stage; what is asserted is only that none of them makes the parser raise, and
that whatever it *does* produce is internally coherent. A hook that throws on a
plan reads to the author as a broken tool, and the plan ships unchecked anyway.

The contract half is the malformed cases. Each one asserts the *wording* that
comes back, because these strings are printed verbatim by the Stage 6 hook as
the reason it refused a plan. A message that reads as a parser diagnostic
("unexpected token") tells the author nothing they can act on, and a refusal
nobody can act on gets bypassed.
"""

import pathlib

import pytest

from jstack_host.plan_parse import _VERIFY, KINDS, ParsedPlan, Stage, is_plan, parse

PLANS = pathlib.Path.home() / ".claude" / "plans"

# This is a developer machine's directory, not repo data — a CI checkout has no
# reason to carry one, and skipping is the honest outcome there.
needs_plans = pytest.mark.skipif(not PLANS.is_dir(), reason="no ~/.claude/plans on this machine")


# --- the regression set ------------------------------------------------------


@needs_plans
def test_every_real_plan_parses_without_raising():
    files = sorted(PLANS.glob("*.md"))
    assert files, "~/.claude/plans exists but is empty — nothing to regress against"
    for f in files:
        parsed = parse(f.read_text(encoding="utf-8", errors="replace"))
        assert isinstance(parsed, ParsedPlan)
        assert isinstance(parsed.title, str)
        assert all(isinstance(p, str) and p for p in parsed.problems)
        # Well-formedness of the OUTPUT, not of these legacy plans: ordinals are
        # document position, and a kind is either empty or one this host knows.
        assert [s.ordinal for s in parsed.stages] == list(range(1, len(parsed.stages) + 1))
        for s in parsed.stages:
            assert isinstance(s, Stage)
            assert s.verify_kind == "" or s.verify_kind in KINDS
            assert s.body == s.body.strip()
            # The located proof line is consumed, never left sitting in the prose.
            assert not any(_VERIFY.match(line) for line in s.body.splitlines())
        assert is_plan(parsed) == (len(parsed.stages) >= 2)


@pytest.mark.skipif(
    not (PLANS / "cuddly-waddling-pie.md").is_file(), reason="the contract-conforming plan is absent"
)
def test_the_plan_that_defines_the_contract_parses_clean():
    """This plan is the one that specified the format, so it is the format's
    own acceptance test. It also carries a fenced `## Stage 1 — …` example
    inside its contract section; if that example were parsed as a stage, every
    real stage after it would be misnumbered and the plan would come back dirty.
    """
    parsed = parse((PLANS / "cuddly-waddling-pie.md").read_text())
    assert parsed.problems == []
    assert len(parsed.stages) >= 8
    assert all(s.verify_kind for s in parsed.stages)
    assert is_plan(parsed)


# --- the contract ------------------------------------------------------------


def test_title_is_the_document_h1():
    assert parse("# A plan\n\ntext\n").title == "A plan"
    assert parse("no heading here\n").title == ""


def test_ordinal_is_document_position_and_the_mismatch_is_reported():
    """Source numbering is the author's note to themselves. A stage inserted in
    the middle renumbers nothing, so position is the only id that can be filed
    against a receipt."""
    md = (
        "# P\n"
        "## Stage 3 — first\nVerify: none\nbody\n"
        "## Stage 3 — second\nVerify: none\nbody\n"
        "## Stage 1 — third\nVerify: none\nbody\n"
    )
    parsed = parse(md)
    assert [s.ordinal for s in parsed.stages] == [1, 2, 3]
    assert [s.title for s in parsed.stages] == ["first", "second", "third"]
    assert parsed.problems == [
        'Stage 1 "first" is numbered 3 but is the 1st stage in the document. '
        "Renumber the headings or drop the numbers — document order is what is used.",
        'Stage 2 "second" is numbered 3 but is the 2nd stage in the document. '
        "Renumber the headings or drop the numbers — document order is what is used.",
        'Stage 3 "third" is numbered 1 but is the 3rd stage in the document. '
        "Renumber the headings or drop the numbers — document order is what is used.",
    ]
    # A mismatch is a warning: the stages are still stages.
    assert all(s.verify_kind == "none" for s in parsed.stages)


def test_missing_verify_line():
    parsed = parse("# P\n## Stage 1 — ship the thing\njust prose\n")
    assert parsed.stages[0].verify_kind == ""
    assert parsed.problems == [
        'Stage 1 "ship the thing" has no `Verify:` line. Add one under the heading — '
        "`Verify: <kind> · <spec>`, kind one of command, commit, artifact, manual, none. "
        "A stage that declares no proof is a stage nobody can close."
    ]


def test_unknown_kind():
    parsed = parse("# P\n## Stage 1 — ship it\nVerify: eyeball · looks fine\n")
    assert parsed.stages[0].verify_kind == ""
    assert parsed.problems == [
        'Stage 1 "ship it" declares `Verify: eyeball`, which is not a proof kind. '
        "Use command, commit, artifact, manual or none — `none` is the right answer "
        "for genuinely trivial work, but it has to be that word."
    ]


def test_command_with_no_spec():
    parsed = parse("# P\n## Stage 1 — ship it\nVerify: command\n")
    assert parsed.stages[0].verify_kind == "command"
    assert parsed.stages[0].verify_spec == ""
    assert parsed.problems == [
        'Stage 1 "ship it" declares `Verify: command` with nothing to run. Put it '
        "after the separator — `Verify: command · ./verify/thing.sh` — or declare a "
        "kind that needs no spec."
    ]


def test_commit_needs_no_spec_and_none_closes_on_assertion():
    parsed = parse("# P\n## Stage 1 — a\nVerify: commit\n## Stage 2 — b\nVerify: none\n")
    assert [(s.verify_kind, s.verify_spec) for s in parsed.stages] == [("commit", ""), ("none", "")]
    assert parsed.problems == []


def test_trailing_verification_section_is_not_a_stage():
    """The 2026-09-24 shape, reproduced. The section is reported and produces
    nothing — a checklist outside a stage has no owner by construction."""
    md = "# P\n## Stage 1 — a\nVerify: none\n## Verification\n1. the merge command refuses on red\n"
    parsed = parse(md)
    assert len(parsed.stages) == 1
    assert parsed.problems == [
        '"## Verification" is not a stage and is not read. Move each of its items onto '
        "the `Verify:` line of the stage that owns it — an unowned verification "
        "checklist is the exact shape that shipped 8 commits on zero receipts."
    ]


@pytest.mark.parametrize(
    "heading",
    [
        "## Stage 1 — em dash",
        "## Stage 1 – en dash",
        "## Stage 1 - hyphen",
        "## Stage 1: colon",
        "## Stage 1 the separator is optional",
    ],
)
def test_every_accepted_heading_form(heading):
    parsed = parse(f"# P\n{heading}\nVerify: none\nbody\n")
    assert len(parsed.stages) == 1, heading
    assert parsed.stages[0].title
    assert parsed.problems == []


@pytest.mark.parametrize("sep", ["·", "|", "--", "—", "–", "-", ":", ""])
def test_every_accepted_verify_separator(sep):
    """Every form yields the identical pair. A separator that leaked into the
    spec would be shelled by `run_verify`, and the author would be told their
    verification failed rather than that their `Verify:` line is malformed —
    the em dash being the likeliest to be typed, since every stage heading in
    the plan that defined this format uses one."""
    parsed = parse(f"# P\n## Stage 1 — a\nVerify: command {sep} ./verify/x.sh\n")
    assert (parsed.stages[0].verify_kind, parsed.stages[0].verify_spec) == (
        "command",
        "./verify/x.sh",
    )
    assert parsed.problems == []


def test_a_spec_containing_a_double_dash_is_not_split_on_it():
    """The kind is the first word, so `--` inside a command cannot be mistaken
    for the separator that introduces it."""
    parsed = parse("# P\n## Stage 1 — a\nVerify: command · pytest -q --tb=short\n")
    assert parsed.stages[0].verify_spec == "pytest -q --tb=short"


def test_verify_need_not_be_the_first_line_under_the_heading():
    """DOCUMENTED CHOICE: found, not reported. Every plan written to this
    contract so far states the proof after the prose that motivates it, and a
    parser that only looked at the first non-blank line would refuse the plans
    it was written from. First `Verify:` in the stage wins."""
    md = (
        "# P\n"
        "## Stage 1 — a\n"
        "\n"
        "Some prose first, which is how people actually write.\n"
        "\n"
        "**Verify: command** · `pytest tests/test_x.py` — and the reason why\n"
    )
    parsed = parse(md)
    assert parsed.problems == []
    assert parsed.stages[0].verify_kind == "command"
    # A code span at the head of the spec is the spec; the trailing sentence is
    # commentary, and running the commentary is not a failure anyone debugs fast.
    assert parsed.stages[0].verify_spec == "pytest tests/test_x.py"
    assert parsed.stages[0].body == "Some prose first, which is how people actually write."


def test_h1_is_structure_and_a_stage_shaped_h1_is_reported():
    md = "# A plan\n# Part one — Environment\n## Stage 1 — a\nVerify: none\n# Stage 9 — orphan\n"
    parsed = parse(md)
    assert parsed.title == "A plan"
    assert [s.title for s in parsed.stages] == ["a"]
    assert parsed.problems == [
        '"# Stage 9 — orphan" is an H1, and H1s are structure, not stages. Make it '
        "`## Stage <N> — <deliverable>` if it is work, or rename it if it is a part heading."
    ]


def test_fenced_headings_are_not_stages():
    md = "# P\n## The contract\n\n```markdown\n## Stage 1 — the example\nVerify: command · ./x.sh\n```\n\n## Stage 1 — the real one\nVerify: none\n"
    parsed = parse(md)
    assert [s.title for s in parsed.stages] == ["the real one"]
    assert parsed.problems == []


def test_subheadings_belong_to_their_stage():
    md = "# P\n## Stage 1 — a\nVerify: none\n### 1a. detail\nmore\n## Stage 2 — b\nVerify: none\n"
    parsed = parse(md)
    assert [s.title for s in parsed.stages] == ["a", "b"]
    assert "1a. detail" in parsed.stages[0].body


def test_is_plan_is_more_than_one_stage():
    assert not is_plan(parse("# P\n## Stage 1 — a\nVerify: none\n"))
    assert is_plan(parse("# P\n## Stage 1 — a\nVerify: none\n## Stage 2 — b\nVerify: none\n"))
    assert not is_plan(parse(""))


def test_empty_and_garbage_never_raise():
    for text in ["", "\n\n\n", "####### not a heading", "## Stage\n", "```\n## Stage 1 — x\n"]:
        assert isinstance(parse(text), ParsedPlan)


def test_the_word_stage_alone_does_not_make_a_heading_a_stage():
    parsed = parse("# P\n## Stages of the rollout\ntext\n## Staging environment\ntext\n")
    assert parsed.stages == []
