# stop-inbox-guard — fixed prose around the mail rows

Sections are loaded by `hooks/_prompts.py`; the hook computes plurals and
passes them in via `.format()`. Multi-line sections are split back into
lines by the hook, so their layout here is the injected layout.

## replies-head

{n} answer{s} to what this session sent {have} come back. This is the reply you asked for — use it before the turn ends.

## replies-coda

Carry on with whatever you sent it for. If it answers the question, the exchange is over — say nothing back; a message acknowledging a message is traffic, not work. If it does not, the follow-up goes into the same conversation on their side:
  {plugin_bin}/msg reply <id> "the one thing that was missing"

## injects-head

{n} comment{s} landed on GitHub issue{s2} this conversation created.

## injects-coda

You are the issue's creator. If a comment asks you something or the work has gone wrong, answer on the issue itself — `gh issue comment <N> -R <owner/repo> --body "…"` — digest form, a few lines; the operator reads these. A progress note that needs nothing gets nothing back.

## tasks-head

{n} task{s} {were} handed to this session and {have} no answer yet. The sender is blocked waiting on it — answer before the turn ends.

## tasks-coda

Do it, then answer — the reply IS the close, there is nothing else:
  {plugin_bin}/msg reply <id> "what you did, or the answer they wanted"

If you cannot do it, say that in the reply and why. An answer that reports a refusal or a blocker is a real answer; silence is not. If the ask is unclear, reply with the question — it reaches the session that sent it and they can answer you.

## closing

Then stop. This is the only thing you owe anyone here — do not go looking for other mail and do not start unrelated work.
