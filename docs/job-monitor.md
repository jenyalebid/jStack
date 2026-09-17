# Background shell jobs

`host/tools/job_monitor.py` provides the `job_monitor` MCP server and an
equivalent CLI. A detached supervisor owns each command, waits for its exit,
and uses native `codex queue --thread <UUID>` to send one completion event to
the originating conversation. There is no model-side polling, terminal typing,
new agent, or second process resuming the same conversation.

Install with `host/tools/codex_setup.py --job-monitor`, or register just this
server with `codex mcp add job_monitor -- <python> <checkout>/host/tools/job_monitor.py mcp`.
An existing server configuration is preserved by the setup script. Fresh Codex
sessions discover the tools; an already running session can use the CLI.

This is an opt-in local shell executor with the MCP server's OS permissions,
not a sandbox. Install only in a trusted execution environment. Deployments
with shell-specific PreToolUse guards must classify `mcp__job_monitor__start`
as a shell command, preserving its `command` and `cwd`, before enabling it.
Job commands require exactly the same user authorization as ordinary shell
commands. Interactive stdin is not supported.

## Use

Any command expected to outlast a minute belongs here rather than in a turn
held open waiting for it — a push into a `pre-push` gate that runs the repo's
suite, a VM boot, a full test run. Spend the wait on work outside that repo's
working tree: a gate runs the suite out of the tree, so editing it mid-gate
judges a tree that never existed. Settle the job in the session that started
it — read its result, report what it says, own a red gate — because a result
nobody read is not an outcome.

- `start(command, cwd, thread_id, timeout_seconds=3600)` returns a job ID.
  Read `CODEX_THREAD_ID` from the current shell; never infer a recipient from
  a seat, session name, most recent transcript, or managed board identifier.
- Continue independent work. If nothing remains until completion, yield the
  turn. The native queue holds the event while a turn is active and provides
  the continuation through Codex's own message channel.
- On the event, `status(job_id)` returns the exit code, notification receipt,
  log location, and at most 2 KB of log text. Read more from the log only when
  needed. Do not poll this tool while waiting.
- `cancel(job_id)` explicitly requests termination of the command's process
  group. It does not signal a PID from a saved record, so stale PID reuse cannot
  cancel an unrelated process. Cancellation produces a completion event too.

A completion event carries the state and exit code. For a **failed** job it also
carries a bounded tail of the log, fenced as data rather than instructions,
because the log is the whole point of that event and fetching it separately
spends a round trip on the same bytes. A success carries no output.

## The wrapper: a command that hands itself over

`start` is a decision. `job_monitor.py run --command '<cmd>'` is not: it wraps a
command the caller was going to execute anyway, waits `--threshold-seconds` (20
by default), and only hands it over once it has PROVEN slow.

- Inside the threshold it is the bare command — same exit code, same output
  (merged, since the log is one stream), no job to settle, nothing to remember.
- Past it, the wait ends and nothing else changes: the command keeps running
  under the supervisor that has owned it since the first millisecond, and the
  handover prints the job ID and log path. Do not poll it or run the command
  again.
- Nothing here is allowed to be the reason a command does not run. No thread to
  notify, no `codex` binary, no private job directory: it execs the command in
  the foreground and behaves like the plain shell.

This is for a **shim or a script**, not for a model choosing a tool, and it is
deliberately absent from the MCP surface — a model calling it would be back to
holding the turn open. It exists because the engine's own exec yields while the
command is still running, and a session that meets a half-finished command starts
polling partial output. Wrap the surfaces that produce long commands and the
waiting stops being a choice anyone can get wrong.

CLI equivalent (from this checkout):

```sh
python host/tools/job_monitor.py start --command 'make test' --cwd "$PWD"
python host/tools/job_monitor.py status JOB_ID
python host/tools/job_monitor.py cancel JOB_ID
```

## Receipts and boundaries

Runtime data lives in `$CODEX_HOME/jobs/<job_id>` (default `~/.codex/jobs`),
inside private directories. Logs retain the first 16 MiB while excess output
is drained and counted. Timeouts are explicit (1 second to 24 hours) and stop
the process group; a supervisor survives the originating MCP connection closing.

Job outcome and notification outcome are separate. `notification: queued`
means Codex acknowledged enqueueing the event, **not** that a model consumed
it. A failed notification preserves the result and diagnostic receipt. There
is no automatic retry after uncertain delivery, avoiding duplicate wakes.
Notifications contain status and a log path, never arbitrary command output.
An unavailable supervisor reports `unknown`, never a fabricated completion.
Jobs do not survive a machine reboot, and pending notifications are not
automatically recovered after supervisor failure.

`host/tests/test_job_monitor.py` exercises real detached processes, MCP
requests, process-group termination, output limits, and notification errors.
The queue executable is replaced in those tests; actual native queue delivery
must also be checked on the installed Codex version.
