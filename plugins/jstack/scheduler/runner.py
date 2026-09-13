"""Spawn/watch/kill claude runs.

Watchdogs are OS threads, never asyncio timers — an asyncio timer has been
observed sleeping 3h+ past a 900s deadline under load, which silently disarms
every deadline in this module. Absolute timeout counts from SPAWN. Kill is
authoritative: process-group SIGTERM → 10s grace → SIGKILL, then reap via
wait() — no zombies.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, journal, spawn as spawnlib

_POLL_SECONDS = 1.0
_KILL_GRACE_SECONDS = 10.0

_KILL_STATUS = {"timeout": "timeout", "stall": "stalled", "ttft": "stalled", "manual": "killed"}

# The claude CLI prints this and exits 1 when an account usage window is
# exhausted. The qualifier names WHICH window ran out and the CLI owns that
# wording — "You've hit your session limit · resets 7am (America/Los_Angeles)"
# for the 5-hour window, "You've hit your weekly limit · resets 5am
# (America/Los_Angeles)" for the weekly cap — so match the family, not one
# literal. A single-literal match is a silent misclassifier: every unmatched
# variant scores `error`, which skips the defer, bumps consecutive_errors on
# recurring jobs until the health check pages, and PARKS one-shot wakes with
# their work dropped. The bounded gap (≤3 words) keeps it from matching prose.
# The condition is transient (self-heals at the stated reset), NOT a job or
# content fault, so it gets its own status and the engine defers instead of
# hard-failing.
_USAGE_LIMIT_RE = re.compile(r"hit your\s+(?:[\w.\-]+\s+){0,3}limit\b", re.IGNORECASE)
_RESET_RE = re.compile(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.IGNORECASE)
# The CLI quotes the reset clock in the machine's own zone (the message states
# it, e.g. "resets 7am (America/Los_Angeles)"), so read it in the install's tz.
def _reset_tz() -> ZoneInfo:
    return ZoneInfo(config.default_tz())


def is_usage_limit(output: str) -> bool:
    return bool(output) and bool(_USAGE_LIMIT_RE.search(output))


# The claude CLI surfaces a mid-run API/transport failure as its final text and
# exits nonzero — "API Error: Connection closed mid-response", "API Error: 529
# Overloaded", "Connection error." The model never finished the turn, so this is
# a transient infrastructure fault (self-heals on the next connection), NOT a job
# or content fault. Like a session limit it gets its own status so the engine
# retries instead of parking the wake and silently dropping its work. Without
# this arm, a one-shot killed by a dropped connection mid-task is lost outright:
# it parks, nothing retries it, and the work it was booked to do never happens.
_TRANSIENT_API_MARKERS = ("api error:", "connection error", "connection closed mid-response")


def is_transient_api_error(output: str) -> bool:
    if not output:
        return False
    low = output.lower()
    return any(m in low for m in _TRANSIENT_API_MARKERS)


# A run can also die BEFORE it ever reaches the model: the CLI fails to renew
# its stored OAuth credential, exits nonzero, and its whole output is
# "Failed to authenticate: OAuth session expired and could not be refreshed".
# The condition self-heals — observed twice on the same daily job (2026-09-07
# and 09-08, dead in 2-5s), then a one-shot through the SAME daemon, spawn path
# and keychain item finished ok hours later with no re-login and no credential
# change. So it is an infrastructure fault like the two families above, not a
# job or content fault, and scoring it `error` costs a daily job its whole day.
#
# It gets its OWN status rather than joining is_transient_api_error(): that
# predicate means "the API dropped us mid-turn" and earns an IMMEDIATE retry,
# which is wrong here — a session that could not be refreshed meets the same
# credential seconds later and dies identically. This family wants a deferred
# retry and a small cap (engine._defer_for_auth_expiry), because the other
# reading of the same failure is a login that is genuinely dead and needs a
# human at `claude /login`; that one must stop being retried and surface.
#
# Match the expiry FAMILY, not the one literal — the CLI owns the wording and
# prints it more than one way ("OAuth session expired and could not be
# refreshed" from the headless path, "OAuth token expired and refresh failed
# (re-login required)" elsewhere). Requiring the word "expired" is what keeps
# the CLI's plain "Please run /login" — a state no retry can fix — out of this
# arm, and the bounded gap keeps it from matching prose.
_AUTH_EXPIRED_RE = re.compile(r"\bo?auth\s+(?:session|token)\s+(?:has\s+)?expired\b",
                              re.IGNORECASE)


def is_auth_expired(output: str) -> bool:
    return bool(output) and bool(_AUTH_EXPIRED_RE.search(output))


def rate_limit_reset(output: str, now: datetime) -> "datetime|None":
    """The next reset datetime quoted in a usage-limit message, else None.

    'resets 7am' → the next 07:00 in the install's tz at/after `now` (today if
    still ahead, tomorrow if already past). The CLI also quotes minutes ('resets 9:10am'),
    and an unparsed reset is not a harmless miss: no reset means no defer, the
    job's next_run is never pinned, and the run comes back hours later on a
    stale due-time — duplicate work on a live account.

    A BARE clock is all this reads, and that is deliberate — do not "fix" it to
    parse the dated form. The CLI quotes a date once the reset is more than a
    day out, which in practice means the WEEKLY cap: "resets 5am" when the reset
    is tomorrow morning, "resets Aug 15 at 5am" when it is two days out. _RESET_RE
    requires the digits to follow "resets" immediately, so the dated form does not
    match and this returns None — which routes the job to the blind-retry arm in
    engine._defer_for_rate_limit (a fixed interval, bounded by the defer cap)
    instead of pinning next_run to the quoted moment.

    That is the right outcome, because the quoted weekly reset overstates the
    outage. On 2026-08-13 the message read "resets Aug 15 at 5am" and rate-limited
    19 runs fleet-wide between 05:31 and 07:06; every job from 08:00 on succeeded,
    so the cap in fact lifted in about two and a half hours, not two days. Honouring
    that date would have surrendered two days of every rate-limited job — for a
    publish wake, the post itself. The blind arm's runway covers a real outage of
    this length; the quoted one does not describe it.

    So the asymmetry is intended: an early retry costs a fast-failing spawn and is
    bounded by the defer cap, while surrendering the slot loses the work outright.
    Parse a same-day clock, distrust a multi-day date."""
    if not is_usage_limit(output):
        return None
    m = _RESET_RE.search(output)
    if not m:
        return None
    hour = int(m.group(1)) % 12
    minute = int(m.group(2) or 0)
    if m.group(3).lower() == "pm":
        hour += 12
    now_la = now.astimezone(_reset_tz())
    reset = now_la.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if reset <= now_la:
        reset += timedelta(days=1)
    return reset


def _slug(workspace: Path) -> str:
    return str(workspace).replace("/", "-")


def session_jsonl_path(workspace: Path, session_id: str) -> Path:
    return Path.home() / ".claude" / "projects" / _slug(workspace) / f"{session_id}.jsonl"


def fresh_session_id(workspace: Path) -> str:
    """uuid4, regenerated if the JSONL already exists (collision guard)."""
    while True:
        sid = str(uuid.uuid4())
        if not session_jsonl_path(workspace, sid).exists():
            return sid


def _is_stop_hook_turn(d: dict) -> bool:
    """A user turn injected by a Stop hook (harness prefixes 'Stop hook feedback:')."""
    content = d.get("message", {}).get("content")
    if isinstance(content, str):
        return content.startswith("Stop hook feedback:")
    if isinstance(content, list):
        return any(
            b.get("type") == "text"
            and (b.get("text") or "").startswith("Stop hook feedback:")
            for b in content
        )
    return False


def last_text_block(jsonl_path: Path) -> str:
    """Last assistant text block of the actual TASK from a session jsonl.

    `--output-format text` prints only the FINAL assistant turn — a run that
    ends on tool calls produces empty stdout even though reply text exists in
    the transcript.

    Stop hooks (e.g. the jstack timeline reminder, added 2026-07-02) append a
    trailing "Stop hook feedback:" user turn + assistant acknowledgement AFTER
    the deliverable. The summary must be the work's final text, not the hook
    epilogue — so the walk stops at the first Stop-hook turn.
    """
    try:
        lines = jsonl_path.read_text().splitlines()
    except OSError:
        return ""
    last_txt = ""
    for line in lines:
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") == "user" and _is_stop_hook_turn(d):
            break
        if d.get("type") != "assistant":
            continue
        for block in d.get("message", {}).get("content", []):
            if block.get("type") == "text":
                txt = (block.get("text") or "").strip()
                if txt:
                    last_txt = txt
    return last_txt


def reached_stop_hook(jsonl_path: Path) -> bool:
    """True if the session ran a Stop hook — i.e. it reached a normal stop.

    The harness fires Stop hooks when a turn ENDS cleanly; a process killed
    mid-turn never gets there. So the marker is positive evidence of a completed
    turn, which is the only completion signal available for a run whose exit code
    we cannot read (see AdoptedRun._exited_status).
    """
    try:
        lines = jsonl_path.read_text(errors="replace").splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") == "user" and _is_stop_hook_turn(d):
            return True
    return False


# ------------------------------------------------ what a dead run already did
#
# A retry re-sends the job's payload from the top, so it is only safe when the
# attempt it replaces took no action worth repeating. `api_error` is raised
# whenever the connection drops — including on the LAST turn of a run that had
# already done everything it was told to do — so "it failed" says nothing about
# whether it acted. The transcript does: a `tool_use` block is the only thing in
# a session that reaches outside the model.
#
# ALLOWLIST, never a denylist, and the asymmetry is the whole reason. Wrongly
# calling a run dirty costs one occurrence, loudly, with a human notified.
# Wrongly calling it clean is the defect this exists to end — a second push,
# publish or send, silently. A denylist scores every tool nobody enumerated as
# clean, which is exactly backwards on the surface that matters most here: the
# MCP publish/send tools an install adds long after this file ships.
#
# `Task` is absent on purpose — a subagent can do anything its parent could.
# `WebFetch` too: it reaches an external party, which is the category under
# review, and a run whose only action was a GET is not the case this arm exists
# to save.
_READ_ONLY_TOOLS = frozenset({
    "Read", "Glob", "Grep", "LS", "NotebookRead", "WebSearch",
    "TodoRead", "TodoWrite", "ExitPlanMode",
})

# `Bash` is the one tool whose effect is unknowable from its name, and excluding
# it outright would retire the retry arm in practice — nearly every run opens by
# shelling out. So it is read by its command line, strictly. The list is
# deliberately small: a command nobody thought about reads as mutating, which is
# the safe direction.
_READ_ONLY_COMMANDS = frozenset({
    "awk", "basename", "cat", "cut", "date", "df", "diff", "dirname", "du",
    "echo", "egrep", "fgrep", "file", "find", "grep", "head", "hostname", "id",
    "jq", "ls", "printf", "ps", "pwd", "readlink", "realpath", "rg", "sed",
    "sort", "stat", "tail", "tr", "uname", "uniq", "wc", "which", "whoami",
})

# git is read by its SUBcommand. Only the unambiguous readers are here: `config`
# sets as readily as it gets, a bare `stash` stashes, and `branch`/`tag`/`remote`
# mutate the moment they take an argument — none of them earn a place a blind
# retry would rely on.
_READ_ONLY_GIT = frozenset({
    "blame", "cat-file", "describe", "diff", "log", "ls-files", "ls-tree",
    "name-rev", "rev-list", "rev-parse", "shortlog", "show", "status",
    "symbolic-ref", "whatchanged",
})

# Tokens that put a segment beyond this reader: a redirect writes, a substitution
# runs a command this parse never sees, a stray `&` backgrounds one. Split on the
# separators FIRST, so `&&` and `||` never reach the `&` test.
_SEGMENT_SPLIT = re.compile(r"\|\||&&|;|\||\n")
_SHELL_DANGER = (">", "$(", "`", "<(", "&")

# Flags that turn a listed reader into a writer.
_MUTATING_FLAGS = {
    "sed": ("-i", "--in-place"),
    "find": ("-delete", "-exec", "-execdir", "-ok", "-okdir",
             "-fprint", "-fprintf", "-fls"),
}


def _git_subcommand(words: list) -> "str|None":
    """The verb in a `git` invocation, skipping the global flags that may
    precede it (`-C <path>`, `--no-pager`, `-c k=v`). None when there is no
    verb, which is `git` alone — a usage dump, and harmless, but not worth a
    special case."""
    i = 1
    while i < len(words):
        w = words[i]
        if w in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            i += 2
            continue
        if w.startswith("-"):
            i += 1
            continue
        return w
    return None


def is_read_only_bash(command: str) -> bool:
    """True only when every segment of `command` provably reads and nothing
    else. Anything this parse cannot account for — a redirect, a substitution,
    an unlisted command, a quote it cannot balance — is False."""
    if not command or not command.strip():
        return True
    for segment in _SEGMENT_SPLIT.split(command):
        segment = segment.strip()
        if not segment:
            continue
        if any(tok in segment for tok in _SHELL_DANGER):
            return False
        try:
            words = shlex.split(segment)
        except ValueError:          # unbalanced quotes — unparseable, so unknown
            return False
        if not words:
            continue
        head = os.path.basename(words[0])
        if head == "git":
            if _git_subcommand(words) not in _READ_ONLY_GIT:
                return False
            continue
        if head not in _READ_ONLY_COMMANDS:
            return False
        flags = _MUTATING_FLAGS.get(head)
        if flags and any(w.startswith(flags) for w in words[1:]):
            return False
    return True


def first_action(jsonl_path: Path) -> "str|None":
    """What in this session's transcript shows the run already acted — the first
    tool call that is not provably read-only — or None when every call in it was
    a read, or there were none at all.

    A transcript that is MISSING answers None, deliberately: the CLI writes the
    opening user turn as the session starts, so no file at all means the run
    never reached the model and cannot have done anything. A file that exists but
    cannot be read answers "unreadable transcript" — something was written, and
    we cannot say what.

    None is NOT proof of innocence and must not be read as such. The child writes
    this file, so a connection death can lose its last buffered lines. That is
    why the retry banner rides on every retry, not only the doubtful ones.
    """
    if not jsonl_path.exists():
        return None
    try:
        lines = jsonl_path.read_text(errors="replace").splitlines()
    except OSError:
        return "unreadable transcript"
    for line in lines:
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") != "assistant":
            continue
        for block in d.get("message", {}).get("content", []) or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name") or "an unnamed tool"
            if name in _READ_ONLY_TOOLS:
                continue
            if name == "Bash":
                cmd = str((block.get("input") or {}).get("command") or "").strip()
                if is_read_only_bash(cmd):
                    continue
                return f"Bash: {cmd.splitlines()[0][:120]}" if cmd else "Bash"
            return name
    return None


def retry_banner(retry_of: str, reason: str = "") -> str:
    """The preamble a re-spawned run is given so it knows a sibling already ran.

    Without this, a retry receives the message a FIRST attempt gets and has no
    way to know it is a retry — `retry_of` was recorded on the run and in the
    journal and reached nothing the session could read. Everything the session
    needs to self-check is here: which run it replaces, what killed it, and the
    instruction to establish current state before repeating any action. The
    session is the only actor that can tell a booked cron from a pushed merge.
    """
    return (
        f"[RETRY of run {retry_of}, which died with {reason or 'a transient fault'}] "
        f"A previous session was given this exact instruction and may already have "
        f"carried out part or all of it — the fault that killed it says nothing "
        f"about how far it got. Before you take any outward action (a push, a "
        f"publish, a send, a merge, a booked wake, a file written), check whether "
        f"it is already done and skip it if it is. Do only what is left, and say "
        f"in your answer what you found already done."
    )


def build_argv(claude_bin: str, model: str, session_id: str, message: str,
               resume_session_id: "str|None" = None,
               permission_mode: str = "bypassPermissions") -> list:
    """claude invocation for a run. With `resume_session_id`, the run
    fork-resumes that conversation instead of starting fresh:
    `--resume <sid> --fork-session --session-id <fresh>` writes the forked
    transcript to the FRESH pinned id, so the TTFT/stall watchdogs and
    summary() read the same JSONL path either way. The source transcript must
    live under the run workspace's project slug — schedule_self pins the job's
    `workspace` to the calling session's cwd for exactly this reason.

    `permission_mode` resolves through the job → category → default chain and
    defaults to bypassPermissions: a scheduled run has nobody present to answer
    a prompt, so anything stricter trades autonomy for a run that blocks until
    its watchdog kills it. Narrow it per job or per category, knowing that.
    """
    argv = [
        claude_bin,
        "--print",
        "--output-format", "text",
        "--model", model,
        "--permission-mode", permission_mode,
    ]
    if resume_session_id:
        argv += ["--resume", resume_session_id, "--fork-session"]
    argv += ["--session-id", session_id, "-p", message]
    return argv


# Where a run is spawned — the install owns the policy (scheduler.json:
# workspace_resolver / agent_registry / agent_root / seat_rules). Re-exported
# here because the engine, the reconciler, and the tests all call it by this
# name.
resolve_workspace = spawnlib.resolve_workspace


def pgid_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _killpg_graceful(pgid: int, proc: "subprocess.Popen|None" = None) -> None:
    """SIGTERM the group, 10s grace, SIGKILL. Reap our own child if given."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return
    deadline = time.time() + _KILL_GRACE_SECONDS
    while time.time() < deadline:
        if proc is not None:
            try:
                proc.wait(timeout=deadline - time.time())
                return
            except subprocess.TimeoutExpired:
                break
        else:
            if not pgid_alive(pgid):
                return
            time.sleep(0.2)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


class Run:
    """One spawned claude run: process group owner + watchdog thread."""

    def __init__(self, job: dict, defaults: dict, scheduled_for: datetime,
                 on_finish, retry_of: "str|None" = None,
                 retry_reason: str = ""):
        self.job = job  # effective job dict (defaults already applied)
        self.defaults = defaults
        self.scheduled_for = scheduled_for
        self.on_finish = on_finish
        self.retry_of = retry_of
        self.retry_reason = retry_reason
        self.run_id = uuid.uuid4().hex[:12]
        self.job_id = job["id"]
        self.model = job.get("model") or defaults.get("model", "opus")
        self.workspace: "Path|None" = None
        self.session_id: "str|None" = None
        self.proc: "subprocess.Popen|None" = None
        self.pid: "int|None" = None
        self.pgid: "int|None" = None
        self.spawned_at: "float|None" = None
        self.kill_reason: "str|None" = None
        self.rate_limited: bool = False
        self.rate_limit_reset_at: "datetime|None" = None
        self.transient_api_error: bool = False
        self.auth_expired: bool = False
        self._kill_lock = threading.Lock()
        self._jsonl: "Path|None" = None
        self._log_path: "Path|None" = None
        self._log_fh = None

    @property
    def scheduled_for_ms(self) -> int:
        return int(self.scheduled_for.timestamp() * 1000)

    @property
    def spawned_at_ms(self) -> int:
        return int((self.spawned_at or 0) * 1000)

    def spawn(self) -> "Run":
        self.workspace = resolve_workspace(self.job)
        self.session_id = fresh_session_id(self.workspace)
        self._jsonl = session_jsonl_path(self.workspace, self.session_id)
        # EXACT first-message shape — thread classification depends on the
        # '[cron:' prefix, so it stays first and the retry banner goes after it,
        # ahead of the payload. Ahead, not appended: the payload is an
        # instruction, and a warning that arrives after the thing it qualifies
        # is a warning the session has already acted past.
        banner = (retry_banner(self.retry_of, self.retry_reason) + "\n\n"
                  if self.retry_of else "")
        message = (f"[cron:{self.job_id} {self.job.get('name', '')}] "
                   f"{banner}{self.job['payload']['message']}")
        claude_bin = self.job.get("claude_bin") or self.defaults.get("claude_bin", "claude")
        # Install-owned: the marker env a run carries (autonomy gates, timeline
        # origin) and the tool dirs on its PATH.
        env = spawnlib.run_env(os.environ.copy())
        # Popen resolves argv[0] against the PARENT process's os.environ PATH, NOT
        # env["PATH"] — so a bare "claude" fails under launchd's stripped PATH even
        # though the child env is correct. Resolve to an absolute path under the same
        # spawn PATH the child runs with. (Broke 2026-07-08 when claude moved
        # /opt/homebrew/bin → ~/.local/bin; the bare name stopped resolving.)
        if os.path.basename(claude_bin) == claude_bin:  # bare name, not a path
            claude_bin = spawnlib.claude_resolvable(env["PATH"]) or claude_bin
        argv = build_argv(claude_bin, self.model, self.session_id, message,
                          resume_session_id=self.job["payload"].get("resume_session_id"),
                          permission_mode=(self.job.get("permission_mode")
                                           or self.defaults.get("permission_mode")
                                           or "bypassPermissions"))
        # No SKIP_SESSION_HOOK: cron runs get post-session reviews (parity).
        config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self._log_path = config.LOGS_DIR / f"{self.run_id}.out"
        self._log_fh = self._log_path.open("w")
        self.spawned_at = time.time()
        self.proc = subprocess.Popen(
            argv,
            cwd=str(self.workspace),
            env=env,
            stdout=self._log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.pid = self.proc.pid
        self.pgid = os.getpgid(self.pid)
        journal.append(self.job_id, journal.started_record(
            job_id=self.job_id,
            run_id=self.run_id,
            session_id=self.session_id,
            run_at_ms=self.scheduled_for_ms,
            spawned_at_ms=self.spawned_at_ms,
            pid=self.pid,
            pgid=self.pgid,
        ))
        threading.Thread(target=self._watch, daemon=True, name=f"run-{self.run_id}").start()
        return self

    def kill(self, reason: str) -> bool:
        with self._kill_lock:
            if self.proc is None or self.proc.poll() is not None:
                return False
            if self.kill_reason is None:
                self.kill_reason = reason
        _killpg_graceful(self.pgid, self.proc)
        return True

    def _watch(self) -> None:
        timeout_s = self.job.get("timeout_seconds") or self.defaults["timeout_seconds"]
        stall_s = self.job.get("stall_timeout_seconds") or self.defaults["stall_timeout_seconds"]
        ttft_s = self.job.get("ttft_timeout_seconds") or self.defaults["ttft_timeout_seconds"]
        deadline = self.spawned_at + timeout_s
        while self.proc.poll() is None:
            now = time.time()
            if now >= deadline:
                self.kill("timeout")
                break
            try:
                mtime = self._jsonl.stat().st_mtime
            except OSError:
                mtime = None
            if mtime is None:
                if now - self.spawned_at >= ttft_s:
                    self.kill("ttft")
                    break
            elif now - max(mtime, self.spawned_at) >= stall_s:
                self.kill("stall")
                break
            time.sleep(_POLL_SECONDS)
        exit_code = self.proc.wait()  # reap — no zombies
        try:
            self._log_fh.close()
        except OSError:
            pass
        # A nonzero exit that is really an account usage limit ("You've hit your
        # session/weekly limit · resets Nam") is transient, not a job fault —
        # detect it from the captured output so _status can flag it distinctly.
        if not self.kill_reason and exit_code != 0:
            out = self._read_output()
            if is_usage_limit(out):
                self.rate_limited = True
                self.rate_limit_reset_at = rate_limit_reset(out, datetime.now(timezone.utc))
            elif is_auth_expired(out):
                self.auth_expired = True
            elif is_transient_api_error(out):
                self.transient_api_error = True
        status = self._status(exit_code)
        self.on_finish(self, status=status, exit_code=exit_code,
                       kill_reason=self.kill_reason, summary=self.summary())

    def _read_output(self) -> str:
        try:
            return self._log_path.read_text(errors="replace")
        except OSError:
            return ""

    def _status(self, exit_code: int) -> str:
        if self.kill_reason:
            return _KILL_STATUS.get(self.kill_reason, "killed")
        if self.rate_limited:
            return "rate_limited"
        # Ahead of api_error deliberately, and _exited_status below matches this
        # order: an auth failure can also print an "API Error: 401 …" line, and
        # the more specific family is the one whose retry timing is right.
        if self.auth_expired:
            return "auth_expired"
        if self.transient_api_error:
            return "api_error"
        return "ok" if exit_code == 0 else "error"

    def summary(self) -> str:
        """Final assistant text of the TASK: transcript walk first (Stop-hook
        aware — see last_text_block), stdout as fallback. stdout is
        `--output-format text`'s final turn, which a Stop hook epilogue
        replaces; the transcript walk skips that."""
        txt = last_text_block(self._jsonl)
        if txt:
            return txt[-4000:]
        try:
            out = self._log_path.read_text(errors="replace").strip()
        except OSError:
            out = ""
        return out[-4000:]


class AdoptedRun:
    """A run recorded by a previous daemon whose pgid is still alive.

    Not our child — cannot wait(); watch group existence and enforce the
    absolute timeout from the original spawn time.
    """

    def __init__(self, job_id: str, entry: dict, job: dict, defaults: dict, on_finish):
        self.job = job
        self.defaults = defaults
        self.on_finish = on_finish
        self.retry_of = None
        self.run_id = entry["run_id"]
        self.job_id = job_id
        self.session_id = entry.get("session_id") or ""
        self.pid = entry.get("pid")
        self.pgid = entry.get("pgid")
        self._spawned_at_ms = int(entry.get("spawned_at_ms") or 0)
        self._scheduled_for_ms = int(entry.get("scheduled_for_ms") or self._spawned_at_ms)
        self.workspace = Path(entry["workspace"]) if entry.get("workspace") else None
        self.model = job.get("model") or defaults.get("model", "opus")
        self.kill_reason: "str|None" = None
        self._kill_lock = threading.Lock()

    @property
    def scheduled_for(self) -> datetime:
        return datetime.fromtimestamp(self._scheduled_for_ms / 1000, tz=timezone.utc)

    @property
    def scheduled_for_ms(self) -> int:
        return self._scheduled_for_ms

    @property
    def spawned_at_ms(self) -> int:
        return self._spawned_at_ms

    def start(self) -> "AdoptedRun":
        threading.Thread(target=self._watch, daemon=True, name=f"adopted-{self.run_id}").start()
        return self

    def kill(self, reason: str) -> bool:
        with self._kill_lock:
            if self.kill_reason is None:
                self.kill_reason = reason
        _killpg_graceful(self.pgid)
        return True

    def _watch(self) -> None:
        timeout_s = self.job.get("timeout_seconds") or self.defaults["timeout_seconds"]
        deadline = self._spawned_at_ms / 1000 + timeout_s
        while pgid_alive(self.pgid):
            if time.time() >= deadline:
                self.kill("timeout")
                break
            time.sleep(_POLL_SECONDS)
        jsonl = (session_jsonl_path(self.workspace, self.session_id)
                 if self.workspace and self.session_id else None)
        summary = last_text_block(jsonl) if jsonl else ""
        if self.kill_reason:
            status, kill_reason = _KILL_STATUS.get(self.kill_reason, "killed"), self.kill_reason
        else:
            status = self._exited_status(jsonl)
            kill_reason = "orphaned" if status == "orphaned" else None
        self.on_finish(self, status=status, exit_code=None,
                       kill_reason=kill_reason, summary=summary)

    def _exited_status(self, jsonl: "Path|None") -> str:
        """Outcome of an adopted run that exited on its own.

        We are not the parent, so there is no exit code — but the run's own
        artifacts survive the restart (the child keeps writing the stdout file it
        inherited, and its session transcript), and they are the same artifacts
        SpawnedRun._status classifies on. Read them in the same order so an
        adopted run is scored like any other wherever the evidence exists.

        Defaulting the whole class to `orphaned` scored SUCCESSFUL work as a
        failure: it bumps consecutive_errors, reds the cron health check until the
        job's next run (a full day, for a daily control), and parks once-jobs as
        enabled:false instead of deleting them. That fired on 2026-07-25 when a
        3s deploy restart adopted two mid-flight runs and marked both failed after
        they finished cleanly. Only an exit with no evidence at all stays
        `orphaned`, which is the honest answer for that case.
        """
        try:
            out = (config.LOGS_DIR / f"{self.run_id}.out").read_text(errors="replace")
        except OSError:
            out = ""
        if is_usage_limit(out):
            self.rate_limit_reset_at = rate_limit_reset(out, datetime.now(timezone.utc))
            return "rate_limited"
        if is_auth_expired(out):
            return "auth_expired"
        if is_transient_api_error(out):
            return "api_error"
        # A Stop hook only fires on a turn that ENDED; stdout under
        # `--output-format text` only gets written on a final assistant turn.
        # Either one is positive evidence the run reached its own end.
        if (jsonl is not None and reached_stop_hook(jsonl)) or out.strip():
            return "ok"
        return "orphaned"
