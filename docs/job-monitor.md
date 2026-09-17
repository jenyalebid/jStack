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
