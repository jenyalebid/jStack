#!/usr/bin/env python3
"""`ExitPlanMode` — the moment a plan becomes rows, and the only gate in this build.

The CLI's `normalizeToolInput` puts the whole approved markdown on `tool_input.plan`
and its path on `planFilePath`; this reads them, writes the stages, and flips the
plan to `active`. Claude only: Codex has no such tool, and `codex_hooks` drops the
matcher rather than registering a hook that can never fire.

THE BLOCKING BRANCH IS NARROW ON PURPOSE. Every agent on this machine passes
through here, so a refusal needs >= 2 stages AND at least one stage whose proof
`set_stages` would itself reject. Of the 12 plans on disk, 10 parse to zero stages
(they say phase, step, section) and sail through untouched, one parses clean, and
one blocks. Widening it is how a safety mechanism gets switched off by the first
person it annoys.

The block text carries the format contract inline. `rules-stage/execution-gates.md`
is scoped to `**/plans/**`, which injects when someone EDITS a plan file — and plan
mode does not author through Edit, the CLI writes the file itself. The model being
refused has therefore almost certainly never seen the rule.

Kill switch: `JSTACK_PLAN_GATE_DISABLED=1`, honoured by all three plan hooks.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402 — sibling module, path set above
import _host  # noqa: E402 — stdin kept, so a re-exec can hand it over
import _prompts  # noqa: E402

KILL_SWITCH = "JSTACK_PLAN_GATE_DISABLED"

# Loose on purpose, and deliberately NOT the parser's `_VERIFY`. Its job is to
# quote back what the author actually typed, including the lines the parser
# refused to read — a stricter pattern would drop exactly the line that needs
# showing. The parser's own `problems` entries say which stage is wrong; these
# lines say what is on the page.
_VERIFY_ISH = re.compile(r"verif(?:y|ication)[ \t]*:", re.IGNORECASE)

_MAX_VERIFY_LINES = 40


def _refused(stage, kinds) -> bool:
    """Stages `set_stages` would reject — the refusal set, computed not matched.

    Exactly the two conditions: no readable kind (the parser blanks it for a
    missing `Verify:` line, an unrecognised kind, and a kind-less one alike) and
    a `command` with nothing to run. Everything else `parse` complains about —
    an ordinal that disagrees with document order, an orphan `## Verification`,
    an H1 that reads like a stage — is cosmetic or structural, and blocking on a
    cosmetic complaint is what teaches people to disable the gate.

    Derived from the writer's rule rather than matched against problem strings,
    so a reworded problem cannot silently move a stage from one side to the other.
    """
    if stage.verify_kind not in kinds:
        return True
    return stage.verify_kind == "command" and not (stage.verify_spec or "").strip()


def _markdown(tool_input: dict) -> str:
    text = tool_input.get("plan")
    if isinstance(text, str) and text.strip():
        return text
    path = tool_input.get("planFilePath")
    if isinstance(path, str) and path:
        try:
            return Path(path).read_text()
        except OSError:
            return ""
    return ""


def _label(stage) -> str:
    """`plan_parse._label`'s spelling — how a problem names the stage it is about.

    The join between a refused stage and the parser's sentence about it. Pinned
    by the test that asserts a named stage's own problem reaches the block text;
    a change to either spelling fails there rather than quietly sorting every
    problem into the warnings.
    """
    return f'Stage {stage.ordinal} "{stage.title}"' if stage.title \
        else f"Stage {stage.ordinal}"


def _block_text(source: str, parsed, refused, kinds) -> str:
    # Both lists are the parser's own sentences, verbatim — it words them for
    # exactly this reader. Only which list they land in is decided here, and it
    # is ONE sentence per refused stage: `parse` emits a stage's proof complaint
    # before its numbering complaint, and a numbering complaint about a stage
    # that also lacks a proof is still only cosmetic. Taking every sentence
    # bearing the label would file it as a reason for the refusal, which is the
    # reader learning that renumbering their headings might unblock them.
    problems = list(dict.fromkeys(parsed.problems))
    fatal = []
    for stage in refused:
        label = _label(stage)
        first = next((p for p in problems if p.startswith(label) and p not in fatal),
                     f"{label} declares no proof that can close it.")
        fatal.append(first)
    warnings = [p for p in problems if p not in fatal]

    quoted = [f"    {raw.strip()}" for raw in (source or "").splitlines()
              if _VERIFY_ISH.search(raw)][:_MAX_VERIFY_LINES]
    also = ""
    if warnings:
        also = "\n" + _prompts.load("plan-gate.md", "also-noted").format(
            warnings="\n".join(f"  · {w}" for w in warnings))
    return _prompts.load("plan-gate.md", "refused").format(
        stage_count=len(parsed.stages), refused_count=len(refused),
        kinds=", ".join(kinds),
        refusals="\n".join(f"  · {f}" for f in fatal),
        verify_lines="\n".join(quoted) or _prompts.load("plan-gate.md", "no-verify-lines"),
        also=also)


def main() -> int:
    payload = json.loads(_host.read_stdin())
    if os.environ.get(KILL_SWITCH):
        return 0
    if payload.get("tool_name") != "ExitPlanMode":
        return 0

    # `host_environment()` resolves this machine's state directory and puts the
    # host package on the path; taking the plan modules after it is one loader.
    _env.host_environment()
    plan_parse, plans = _host.load("plan_parse", "plans")

    tool_input = payload.get("tool_input") or {}
    source = _markdown(tool_input)
    parsed = plan_parse.parse(source)
    refused = [s for s in parsed.stages if _refused(s, plan_parse.KINDS)]

    if len(parsed.stages) >= 2 and refused:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": _block_text(
                source, parsed, refused, plan_parse.KINDS)}}))
        sys.stdout.flush()
        return 0

    session_id = str(payload.get("session_id") or "")
    plan = plans.open_plan_for_session(session_id) if session_id else None
    if plan is None:
        # Nothing parsed as a stage and nothing was already open: the ten plans
        # in twelve that use phase/step/section wording land here, and a row
        # with no title, no stages and nothing to close is not worth minting.
        if not parsed.stages:
            return 0
        plan_id = plans.open_plan(parsed.title, plan_file=str(
            tool_input.get("planFilePath") or ""), engine="claude",
            repo=str(payload.get("cwd") or ""), session_id=session_id, role="author")
    else:
        plan_id = plan["id"]
        # The row was minted at plan-mode entry, titled from the first prompt
        # and knowing no file. This is the moment both are actually authored,
        # and the only one — nothing later in the plan's life sees them again.
        authored = str(tool_input.get("planFilePath") or "")
        if parsed.title or authored:
            plans.update_plan_meta(plan_id, title=parsed.title or None,
                                   plan_file=authored or None)

    if parsed.stages and not refused:
        try:
            plans.set_stages(plan_id, parsed.stages)
        except ValueError:
            # The writer refused a kind this hook read as fine — the two rules
            # have drifted. The plan is approved either way; losing the stages
            # is the cost, wedging the tool call is not.
            pass
    plans.activate(plan_id)
    # No `permissionDecision` on the way through: allowing here would approve
    # the plan on the user's behalf, which is the one thing plan mode is for.
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # Fail OPEN on our own errors: the tool goes through. A traceback out of
        # a PreToolUse hook reads as a broken tool, not a broken plan, and this
        # one stands in front of plan mode for every session on the machine.
        raise SystemExit(0)
