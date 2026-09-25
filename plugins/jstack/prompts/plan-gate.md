## refused
PLAN REFUSED — `ExitPlanMode` was blocked and nothing was recorded.

This plan has {stage_count} stages, and {refused_count} of them declare no proof that could ever close them. A stage without a readable `Verify:` line is work nobody can mark done, which is the one thing this gate exists to stop. Fix the lines below and call `ExitPlanMode` again.

THE FORM. Every `## Stage` heading owns exactly one `Verify:` line, directly under it:

    ## Stage 1 — the parser that reads the format
    Verify: command · ./verify/parse.sh

    ## Stage 2 — the rule the format is written in
    Verify: artifact · plugins/jstack/rules-stage/execution-gates.md

The kind is one of {kinds}, and it has to be one of those words. `command` is run and must exit 0, so it needs something to run. `commit` is satisfied by the shas of the stage's own commits. `artifact` is a path. `manual` waits for the user to say so. `none` closes on your assertion alone, and is the right answer for genuinely trivial work.

WHAT IS WRONG, stage by stage:

{refusals}

THE PROOF-ISH LINES READ OUT OF THE PLAN, exactly as written:

{verify_lines}
{also}

## also-noted

ALSO NOTED — none of this is why the plan was refused, and none of it has to be fixed to get through:

{warnings}

## no-verify-lines
    (none — the plan contains no line that reads as a `Verify:` line at all)

## codex-exit
PLAN MODE ENDED WITH NOTHING RECORDED. A plan row was opened for this session when it entered plan mode, and it still has no stages: the plan text never reached this machine, because this engine ends plan mode by flipping a mode rather than by calling a tool that carries the markdown.

Nothing here can read a plan it was not handed, so the stages are yours to file — once, now, while the plan is in front of you:

    jstack-host plan stages {plan_id} --from-file <the plan markdown>

Each `## Stage` heading needs one `Verify:` line under it — `Verify: <kind> · <spec>`, kind one of command, commit, artifact, manual, none. Until the stages exist there is nothing to close and nothing to prove, and "is this done" has no answer but a conversation.
