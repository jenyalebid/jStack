# Scheduler

Fires one-time and recurring agent runs on RFC-5545 RRULE schedules. It is the piece that lets jStack **start** a session — the timeline remembers what sessions did and the session-end engine reviews them, but neither can begin one.

Native `CronCreate` lives and dies with a session; cloud routines run in Anthropic's cloud, not on your machine. This runs locally, unattended, and keeps state across restarts.

## What it does that a shell script does not

`launchctl` plus `claude -p` in a wrapper is ten lines and genuinely works. What it does not give you:

- **Timeout counted from spawn**, not from the scheduled fire time
- **TTFT and stall watchdogs** — a turn that hangs with no output is killed and retried, instead of holding a slot until the absolute timeout
- **The daemon owns child pgids** — a kill is authoritative (SIGTERM → 10s → SIGKILL on the group), no PID heuristics
- **Catch-up** — occurrences missed while the machine was asleep fire once within a grace window, older ones journal as skipped
- **Concurrency policy** per job (`skip` / `queue` / `parallel`) plus a global cap
- **Rate-limit deferral** — a run that hits the account's usage limit reschedules to just after the quoted reset instead of failing the day
- **State and a journal** — per-job status, error streaks, run history, so a job that silently stopped working is visible

## Install

**Requirements:** Python 3.11+, `python-dateutil`, the `claude` CLI, and a workspace with a `CLAUDE.md` for each agent you schedule.

### 1. Point Python at the package

The package ships inside the plugin. Any process that runs it — daemon, CLI, your own tooling — needs it importable and needs `SCHEDULER_HOME` set to the directory holding your `config/` and `state/` (unset, it defaults to `~/.scheduler`). One hard rule: that directory can never be inside the checkout that ships the package — the plugin lives in a public git repository, and `feed-token` writes a live secret under `$SCHEDULER_HOME/Credentials/`. Any config/state/credentials path resolving into the checkout is refused at import with a `RuntimeError`, deliberately: a loud failure beats a secret in a public working tree.

The tidiest way, if you use a virtualenv, is one `.pth` file in its `site-packages`:

```
import os, sys; _p = os.path.expanduser('~/.claude/plugins/jstack/plugins/jstack'); (_p in sys.path) or sys.path.insert(0, _p); os.environ.setdefault('SCHEDULER_HOME', os.path.expanduser('~/scheduler'))
```

Otherwise set `PYTHONPATH` and `SCHEDULER_HOME` in the environment of whatever launches it. Note that a virtualenv rebuild discards the `.pth` — recreate it, or the CLI stops resolving even while the daemon (which sets both explicitly) keeps running.

### 2. Declare the machine — `$SCHEDULER_HOME/config/scheduler.json`

Everything host-specific lives here. The package holds no machine facts at all.

```jsonc
{
  "timezone": "America/Los_Angeles",     // absent → $TZ, then /etc/localtime, then UTC

  "spawn_env": {                          // env every run carries
    "MY_AGENT_IS_UNATTENDED": "1"
  },
  "spawn_path_prepend": ["~/bin/agent-tools"],   // dirs a run's PATH starts with
  "spawn_dirs": ["~/.local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"],

  "agent_root": "~/Agents",               // {agent_root}/{agent_id}/
  "agent_registry": "~/Agents/agents.json",       // optional {agent: {workspace}} map
  "seat_rules": [                          // an id ending in the suffix runs in a subdir
    {"agent_id_suffix": "-social", "seat": "chat"}
  ],

  "workspace_resolver": null,             // optional "module:function" taking an agent id
  "python_path": []                        // sys.path entries so that module imports
}
```

**Workspace resolution order:** the job's own `workspace` override → `workspace_resolver` hook → `agent_registry` → `{agent_root}/{agent_id}`. Then `seat_rules` redirect into a subdir, but only when that subdir has its own `CLAUDE.md` — a redirect into a directory with no context would start the run blind. A malformed `workspace_resolver` raises rather than falling back, because a silent fallback runs every job in the wrong place and reads as the agent malfunctioning.

### 3. Create the registry and run the daemon

```bash
mkdir -p "$SCHEDULER_HOME/config"
python3 -m scheduler.cli add-recurring --agent myagent \
    --cron "0 9 * * 1-5" --message "morning sweep" --name "weekday sweep"
python3 -m scheduler          # the daemon; run it under launchd or systemd
```

Under launchd, set `PYTHONPATH` and `SCHEDULER_HOME` in `EnvironmentVariables` and use `KeepAlive`. A daemon inherits a stripped PATH, which is why `spawn_dirs` exists — without it a run cannot find `claude`.

## Job settings and the resolver chain

A job's effective value for an inherited setting resolves **job → its category → global default**:

```jsonc
{
  "defaults":   { "model": "opus", "timeout_seconds": 1800, "permission_mode": "bypassPermissions" },
  "categories": { "reviews": { "model": "sonnet", "timeout_seconds": 2700 } },
  "jobs": [ { "category": "reviews", "model": "opus" } ]
}
```

Inherited keys: `engine`, `codex_model`, `codex_bin`, `model`,
`timeout_seconds`, `stall_timeout_seconds`, `ttft_timeout_seconds`,
`claude_bin`, `catch_up_grace_seconds`, `permission_mode`, `notify_on_failure`.

`engine: "codex"` selects a native fresh run; `--engine codex` is accepted by
both add commands. `codex_model` selects its model through the same inheritance
chain; absent it, a GPT `model` value applies, otherwise Codex's configuration
selects the model. A native resume source always selects Codex and is forked
through its API. Fresh native threads are persisted through that API before
the CLI resumes them. Neither path resumes the original source concurrently.
`schedule-self` preserves Codex for fresh wakes as well as resume wakes.

Categories are the point of leverage — set a model and a time limit once for a whole class of runs rather than remembering them at every call site. A per-job value outranks its category, so setting one "just to be explicit" silently overrides a class that was deliberately given more room.

### permission_mode

Defaults to `bypassPermissions`, and that default is not a convenience. A scheduled run has nobody present to answer a prompt, so anything stricter trades autonomy for a run that blocks until its watchdog kills it. The flag exists so a single job or a whole category can be narrowed deliberately, knowing that cost.

## Failure modes worth knowing

| What | Behavior |
|---|---|
| Watchdog kill | `killReason` names it: `ttft` (no transcript within `ttft_timeout_seconds`), `stall` (transcript frozen), `timeout` (absolute, from spawn), `manual` |
| Retry | Only `stalled`/`ttft`/`api_error`/spawn-failure retry, once. Timeouts and genuine job errors do not — a retry there costs a full run and repeats the same failure |
| Rate limit | Deferred to just after the quoted reset, capped; a recurring job with no parseable reset retries blind rather than surrendering the day |
| Daemon restart | Live runs are re-adopted, not re-fired. A dead pgid is scored from the run's own artifacts before being called orphaned |
| Misfire | Beyond the catch-up grace, the occurrence is skipped **and recorded in job state** — a journal-only skip reads green while the work never happened |
| Cap sizing | `max_concurrent_runs` must admit every job the schedule stacks concurrently, not the average; a low cap silently defers the tail |

## Surfaces

- **CLI** — `python3 -m scheduler.cli` — `add-once`, `add-recurring`, `list`, `next`, `rm`, `enable`/`disable`, `lock`/`unlock`, `run-now`, `kill`, `feed-token`
- **Control API** — `127.0.0.1:9091`, loopback bind only, no auth: `GET /health` `/jobs` `/runs`, `POST /jobs/{id}/run-now` `/runs/{id}/kill` `/reload`
- **ICS feed** — `scheduler/ics.py` builds a VCALENDAR you can serve and subscribe to. The VTIMEZONE is derived from your configured zone's real transitions, so it is correct for DST, last-Sunday, and no-DST regions alike

**Locked jobs** (`locked: true`) cannot be removed or rescheduled by an automated session — `rm` refuses without `--force`. Since rescheduling is remove-plus-add, this makes a human-set time something an autonomous run physically cannot move.

**Subjects** (`--subject` on `add-once` / `add-recurring`) — a job's `name` says what a booking is called; its `subject` says what it covers. A second **enabled** job with the same `(agent_id, subject)` is refused with **exit 3**, whatever its schedule, so a one-shot cannot shadow a recurrence already covering the same surface and vice versa. Only the subject can catch this: two agents arranging coverage of one thing produce names that never collide, since a one-shot named for its fire time and a recurrence named for its purpose are different strings however identical the work. The scan runs inside the registry lock, in the same `mutate` that appends the job — check-then-add would leave open the window the guard exists to close. Comparison is case-folded with whitespace collapsed; a job with no subject claims nothing.

## Test

`tests/scheduler.sh` — runs the real package against a hermetic temp `SCHEDULER_HOME`, including a real daemon boot. Never touches a live registry, state dir, or running daemon.
