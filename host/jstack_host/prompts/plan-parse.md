## h1
"# {text}" is an H1, and H1s are structure, not stages. Make it `## Stage <N> — <deliverable>` if it is work, or rename it if it is a part heading.

## orphan-verification
"## {text}" is not a stage and is not read. Move each of its items onto the `Verify:` line of the stage that owns it — an unowned verification checklist is the exact shape that shipped 8 commits on zero receipts.

## no-verify
{label} has no `Verify:` line. Add one under the heading — `Verify: <kind> · <spec>`, kind one of {kinds}. A stage that declares no proof is a stage nobody can close.

## unknown-kind
{label} declares `Verify: {kind}`, which is not a proof kind. Use {kinds} or none — `none` is the right answer for genuinely trivial work, but it has to be that word.

## no-kind
{label} has a `Verify:` line with no kind on it. Write `Verify: <kind> · <spec>`, kind one of {kinds}.

## no-spec
{label} declares `Verify: {kind}` with nothing to run. Put it after the separator — `Verify: {kind} · ./verify/thing.sh` — or declare a kind that needs no spec.

## misnumbered
{label} is numbered {declared} but is the {ordinal}{suffix} stage in the document. Renumber the headings or drop the numbers — document order is what is used.
