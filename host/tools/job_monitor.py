#!/usr/bin/env python3
"""Durable shell jobs with one completion message to the originating Codex thread.

Standard-library only. CLI, MCP and the `run` wrapper use the same detached
supervisor. No model polling, terminal keystrokes, inferred recipient, or
concurrent thread resume.

A job ends when its command exits, never when its output pipe closes — those
are different moments whenever the command backgrounds something, and only the
first one is the job.

`start` is a decision — the model asks for a background job. `run` is not: it
wraps a command a session was going to execute anyway, waits a few seconds, and
only hands it over once it has PROVEN slow. That is the difference between a
tool an agent must remember and one it cannot fail to use, and it is the whole
reason this file has two entry points. See run() for the handover protocol.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid


TERMINAL = {"succeeded", "failed", "timed_out", "cancelled", "launch_failed"}
MAX_LOG = 16 * 1024 * 1024
# How long to keep reading stdout after the command itself has exited, and how
# often to ask whether it has. Both exist because output outlives the command:
# see leader_exit and settle.
DRAIN_GRACE = 5
EXIT_POLL = 0.05
# How long a command wrapped by run() may hold the turn before it is handed
# over. Codex's own exec yields at 30s with the command still RUNNING, and a
# session that meets a half-finished command starts polling it — nine polls of
# partial output was 70 minutes of one turn. Handing over before that yield is
# what makes the wait impossible rather than merely discouraged.
THRESHOLD_SECONDS = 20
# Extra time the supervisor allows the foreground caller to claim or release
# the report after the threshold; covers writing a large log out to stdout.
PROMOTE_GRACE = 30
# How often the supervisor reads the thread's rollout while it holds a finished
# job's event for the turn to end, and the longest it holds one. See idle().
IDLE_POLL = 2
IDLE_CAP = 24 * 3600
# The longest one wait() call holds. Codex cuts an MCP call off at the server's
# tool_timeout_sec (codex_setup writes WAIT_TOOL_TIMEOUT); stopping short of
# that returns a clean "still running" instead of a tool error.
WAIT_MAX = 1500
WAIT_TOOL_TIMEOUT = 1800
WAIT_POLL = 0.5


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f"Expected a private, owned directory: {path}")
    return path


def root():
    return private_dir(Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "jobs")


def job_dir(job_id):
    if uuid.UUID(job_id).hex != job_id:
        raise ValueError("job_id must be a canonical job identifier")
    return root() / job_id


def control_path(job_id):
    # AF_UNIX paths on macOS are limited to 104 bytes.
    return private_dir(Path(f"/tmp/jstack-jobs-{os.getuid()}")) / (job_id + ".sock")


def save(folder, row):
    tmp = folder / "status.tmp"
    tmp.write_text(json.dumps(row) + "\n")
    tmp.replace(folder / "status.json")


def read(job_id):
    return json.loads((job_dir(job_id) / "status.json").read_text())


def log_tail(folder, limit=2000):
    """The last `limit` bytes of a job's log, or None if it has none yet."""
    log = Path(folder) / "output.log"
    if not log.exists():
        return None
    with log.open("rb") as stream:
        stream.seek(max(0, log.stat().st_size - limit))
        return stream.read(limit).decode("utf-8", errors="replace")


def status(job_id):
    row = read(job_id)
    # A terminal result read here has reached the session that asked, so the
    # completion event the supervisor still holds would only repeat it. Its own
    # file, not a status.json field: the supervisor saves its in-memory row and
    # would erase a field written from this side. See worker().
    if row["state"] in TERMINAL:
        (job_dir(job_id) / "observed").touch()
    # Never present a stale saved running state as observed process liveness.
    if row["state"] not in TERMINAL:
        try:
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(1)
                client.connect(str(control_path(job_id)))
                client.sendall(b"status\n")
                if client.recv(32) != b"alive\n":
                    raise OSError("supervisor did not acknowledge")
        except OSError:
            row["state"] = "unknown"
            row["error"] = "Supervisor unavailable; completion is not verified."
    tail = log_tail(job_dir(job_id))
    if tail is not None:
        row["output_tail"] = tail
    return row


def wait(job_id, timeout_seconds=WAIT_MAX):
    """Block until the job ends, then return its status — inside the caller's turn.

    This is how a Codex session should take a job's result. Waiting here costs
    no model calls and the answer arrives as a TOOL RESULT, which Codex drops at
    compaction. The alternative — ending the turn and being woken by a queued
    completion — opens a whole new turn at full context and leaves a user
    message Codex keeps through every compaction for the rest of the thread:
    on 2026-10-01 that was 147 of a run's 211 turns. Reading the result here
    marks it observed (see status), so the supervisor sends no wake at all.

    Past timeout_seconds it returns the running row; call it again.
    """
    if isinstance(timeout_seconds, bool) or not 1 <= int(timeout_seconds) <= WAIT_MAX:
        raise ValueError(f"timeout_seconds must be between 1 and {WAIT_MAX}")
    read(job_id)  # an unknown job fails here, not after the whole wait
    deadline = time.monotonic() + int(timeout_seconds)
    while time.monotonic() < deadline:
        if read(job_id)["state"] in TERMINAL:
            break
        time.sleep(WAIT_POLL)
    return status(job_id)


def rollout(thread_id):
    """The native rollout file for a Codex thread, or None when there is none."""
    sessions = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
    found = sorted(sessions.glob(f"*/*/*/rollout-*-{thread_id}.jsonl"))
    return found[-1] if found else None


def idle(path, window=262144):
    """True unless the rollout's newest turn event says a turn is running.

    Codex writes `task_started` when a turn begins and `task_complete` or
    `turn_aborted` when it ends. A file that cannot be read says nothing, so it
    answers idle: holding an event on a missing signal would be the silence
    this tool exists to end.
    """
    try:
        with open(path, "rb") as stream:
            stream.seek(max(0, os.path.getsize(path) - window))
            lines = stream.read().splitlines()
    except OSError:
        return True
    for line in reversed(lines):
        try:
            payload = json.loads(line).get("payload") or {}
        except ValueError:
            continue
        kind = payload.get("type")
        if kind == "task_started":
            return False
        if kind in ("task_complete", "task_completed", "turn_aborted"):
            return True
    return True


def start(command, cwd, thread_id, timeout_seconds=3600, promoter_deadline=None):
    thread_id = str(uuid.UUID(thread_id))  # exact native thread, never a seat/name
    directory = Path(cwd)
    if not directory.is_absolute() or not directory.is_dir():
        raise ValueError("cwd must be an existing absolute directory")
    if not isinstance(command, str) or not command.strip() or len(command) > 32768:
        raise ValueError("command must contain 1–32768 characters")
    if isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= 86400:
        raise ValueError("timeout_seconds must be between 1 and 86400")
    codex = shutil.which("codex")
    if not codex:
        raise ValueError("codex is required for completion delivery")
    job_id = uuid.uuid4().hex
    folder = private_dir(root() / job_id)
    row = {"job_id": job_id, "thread_id": thread_id, "command": command,
           "cwd": str(directory), "timeout_seconds": timeout_seconds,
           "codex": codex, "state": "starting", "created_at": time.time(),
           "notification": "pending", "log_path": str(folder / "output.log")}
    # Set only by run(): a foreground caller is watching this job, so the
    # supervisor must let it speak first. Absent for every other caller, whose
    # delivery is therefore unchanged.
    if promoter_deadline is not None:
        row["promoter_deadline"] = float(promoter_deadline)
    save(folder, row)
    with (folder / "supervisor.log").open("ab") as log:
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                 "worker", job_id], start_new_session=True,
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    # Only the tool waits here, not the model. Bound startup acknowledgement.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = read(job_id)
        if current["state"] != "starting":
            return {k: current[k] for k in ("job_id", "thread_id", "state", "log_path")}
        if proc.poll() is not None:
            raise RuntimeError(f"Supervisor failed to start; see {folder / 'supervisor.log'}")
        time.sleep(0.02)
    raise RuntimeError(f"Supervisor startup unconfirmed; inspect job {job_id} before retrying")


def cancel(job_id):
    row = read(job_id)
    if row["state"] in TERMINAL:
        return {"job_id": job_id, "state": row["state"]}
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(2)
        client.connect(str(control_path(job_id)))
        client.sendall(b"cancel\n")
        if client.recv(32) != b"accepted\n":
            raise RuntimeError("Supervisor did not accept cancellation")
    return {"job_id": job_id, "state": "cancellation_requested"}


def run(command, cwd=None, thread_id=None, threshold_seconds=THRESHOLD_SECONDS,
        timeout_seconds=3600):
    """Run a command in the foreground, and hand it over once it proves long.

    Under the threshold this IS the bare command: same merged output, same exit
    code, no job to settle, nothing about the session changed. Over it the wait
    simply ends — the command keeps running under the supervisor that has owned
    it since the first millisecond, the caller gets three lines instead of a
    stalled turn, and the outcome arrives later as its own small event.

    Nothing here may be the reason a command does not run. No thread to notify,
    no codex binary, no private job directory: exec the command in the
    foreground and behave exactly like the plain shell.

    Two deliberate differences from running the command yourself: stdout and
    stderr arrive merged, because the log is one stream; and stdin is
    /dev/null, so a command that wants a passphrase fails instead of hanging a
    detached process nobody can type into.
    """
    cwd = cwd or os.getcwd()
    thread_id = thread_id or os.environ.get("CODEX_THREAD_ID")
    plain = ["/bin/sh", "-c", command]  # the same shell the supervisor uses
    if not thread_id:
        # A job whose completion can reach nobody is worse than a wait: the
        # session would keep working with a push it never learns the result of.
        os.execv(plain[0], plain)
    try:
        row = start(command, cwd, thread_id, timeout_seconds,
                    promoter_deadline=time.time() + threshold_seconds + PROMOTE_GRACE)
    except Exception as exc:
        print(f"job-monitor: {exc}; running in the foreground", file=sys.stderr)
        os.execv(plain[0], plain)

    folder = job_dir(row["job_id"])
    deadline = time.monotonic() + threshold_seconds
    while time.monotonic() < deadline:
        current = read(row["job_id"])
        if current["state"] in TERMINAL:
            # Claim the report BEFORE writing anything out. The supervisor is
            # holding its message on this exact file, and it must not have to
            # wait out however long a large log takes to reach stdout.
            (folder / "reported").write_text("")
            log = folder / "output.log"
            if log.exists():
                with log.open("rb") as stream:
                    shutil.copyfileobj(stream, sys.stdout.buffer)
                sys.stdout.buffer.flush()
            code = current.get("exit_code")
            if code is None:
                print(f"job-monitor: {current.get('error', 'command did not run')}",
                      file=sys.stderr)
                return 127
            return code
        time.sleep(EXIT_POLL)

    (folder / "handoff").write_text("")
    print(f"[job-monitor:{row['job_id']}] Still running after {threshold_seconds}s, so "
          f"the shell gave it up: it keeps running under the background monitor.\n"
          f"Do other work if there is any, then take the result with the job_monitor "
          f"`wait` tool (job_id {row['job_id']}). Do not poll, rerun it, or end the turn "
          f"to be woken — a wake costs a whole turn.\nLog: {row['log_path']}")
    return 0


async def worker(job_id):
    os.umask(0o077)
    folder, row = job_dir(job_id), read(job_id)
    cancelled = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, cancelled.set)

    async def control(reader, writer):
        try:
            request = await asyncio.wait_for(reader.read(32), timeout=2)
            if request == b"cancel\n":
                cancelled.set()
                writer.write(b"accepted\n")
            elif request == b"status\n":
                writer.write(b"alive\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def drain(reader):
        # Published per chunk, not at EOF: settle can stop this task early, and
        # a count that only exists on the clean path would be missing exactly
        # when the log is partial and the reader most needs to know how much.
        count = 0
        row["output_bytes"], row["log_truncated"] = 0, False
        with (folder / "output.log").open("wb") as output:
            while chunk := await reader.read(65536):
                remaining = max(0, MAX_LOG - count)
                output.write(chunk[:remaining])
                output.flush()
                count += len(chunk)
                row["output_bytes"], row["log_truncated"] = count, count > MAX_LOG

    async def leader_exit():
        """Wait for the command to exit — not for its output pipe to close.

        asyncio resolves Process.wait() from the subprocess TRANSPORT, which is
        not finished until every pipe reaches EOF. A descendant that inherits
        stdout and outlives the shell therefore keeps wait() pending long after
        the command is gone, and the job burns its entire timeout before being
        recorded as timed_out — an hour, at the default, for a command that
        succeeded in milliseconds. returncode is set the moment the child is
        reaped, independently of any pipe, so read that instead.

        Measured: on 3.12 wait() lags the exit by exactly as long as the pipe
        is held; on 3.14 it does not. This machine runs job_monitor under
        .venv/bin/python3 (3.12), so the lag is live, and on 3.14 this loop
        resolves at the same instant wait() would. Do not drop it for a newer
        interpreter without checking which one Codex actually launches.
        """
        while proc.returncode is None:
            await asyncio.sleep(EXIT_POLL)
        return proc.returncode

    async def settle(output):
        """Collect what is left of the output, then stop waiting for it.

        The command has exited, so anything still holding the write end of
        stdout is a descendant that outlived it. Give it a short grace to
        flush, then stop reading and say so. The job is NOT killed here: a
        deliberately backgrounded process is allowed to keep running, it just
        stops being logged.
        """
        try:
            await asyncio.wait_for(asyncio.shield(output), timeout=DRAIN_GRACE)
        except asyncio.TimeoutError:
            row["log_incomplete"] = True
            output.cancel()
            await asyncio.gather(output, return_exceptions=True)

    sock = control_path(job_id)
    server = await asyncio.start_unix_server(control, path=str(sock))
    proc = None
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                "/bin/sh", "-c", row["command"], cwd=row["cwd"],
                stdin=subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            row.update(state="launch_failed", error=str(exc), exit_code=None)
        else:
            row.update(state="running", pid=proc.pid, started_at=time.time())
            save(folder, row)
            output = asyncio.create_task(drain(proc.stdout))
            exited = asyncio.create_task(leader_exit())
            stopped = asyncio.create_task(cancelled.wait())
            done, _ = await asyncio.wait([exited, stopped], timeout=row["timeout_seconds"],
                                         return_when=asyncio.FIRST_COMPLETED)
            if exited in done:
                row["state"] = "succeeded" if proc.returncode == 0 else "failed"
            else:
                row["state"] = "cancelled" if stopped in done else "timed_out"
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(asyncio.shield(exited), timeout=2)
                except asyncio.TimeoutError:
                    pass
                # A shell may exit on TERM while a descendant ignores it and
                # has already closed stdout. Reap the whole owned group even
                # when the leader's wait completed during the grace period.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await exited
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)
            await settle(output)
            row["exit_code"] = proc.returncode
        row["finished_at"] = time.time()
        save(folder, row)  # Job result survives notification failure.
    finally:
        server.close()
        await server.wait_closed()
        sock.unlink(missing_ok=True)

    # A foreground caller may still own the right to report this outcome: run()
    # claims it by writing `reported` before it prints, or gives the job up by
    # writing `handoff`. Until one of them appears the result has an audience
    # already, and queueing here would duplicate it. Nothing by the deadline =
    # notify: a promoter killed mid-wait leaves a session that never heard of
    # this job at all, and an unheard job is the failure this file exists for.
    if row.get("promoter_deadline"):
        while (not (folder / "reported").exists() and not (folder / "handoff").exists()
               and time.time() < row["promoter_deadline"]):
            await asyncio.sleep(EXIT_POLL)
        if (folder / "reported").exists():
            row["notification"] = "reported_inline"
            save(folder, row)
            return

    # Hold the event while the thread is mid-turn. Codex queues a message for a
    # busy thread and hands it over only after the turn ends, one per turn, and
    # by then the session has usually read the result through status already:
    # on 2026-10-01 one thread received fifteen such events nine to twelve
    # minutes late, each one a whole turn spent confirming a result it already
    # had, and each one landing on the boundary compact-on-delivery was about to
    # use (jStack#334). Waiting here instead lets status() cancel the event.
    path = rollout(row["thread_id"])
    if path is not None:
        deadline = time.time() + IDLE_CAP
        while not idle(path) and time.time() < deadline:
            if (folder / "observed").exists():
                break
            await asyncio.sleep(IDLE_POLL)
    if (folder / "observed").exists():
        row["notification"] = "observed"
        save(folder, row)
        return

    # The notice is a stub, and stays one. Codex keeps every user message it is
    # sent through every compaction, so each byte here is paid on every call for
    # the rest of the thread: one session carried 149 notices (94k chars) into
    # its 18th compaction and woke ~39k tokens heavier than a fresh one. No log
    # tail, success or failure — status returns it on demand, once.
    message = (f"[job-monitor:{job_id}] {row['state']}, exit {row.get('exit_code')}. "
               "Read it with status; do not rerun.")
    try:
        delivered = await asyncio.create_subprocess_exec(
            row["codex"], "queue", "--thread", row["thread_id"], "--message", message,
            stdin=subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
        try:
            receipt, _ = await asyncio.wait_for(delivered.communicate(), timeout=30)
        except asyncio.TimeoutError:
            delivered.kill()
            await delivered.wait()
            raise RuntimeError("Queue acknowledgement timed out; delivery is uncertain")
        row["notification"] = "queued" if delivered.returncode == 0 else "failed"
        row["notification_receipt"] = receipt.decode(errors="replace")[-2000:]
    except (OSError, RuntimeError) as exc:
        row.update(notification="failed", notification_receipt=str(exc))
    save(folder, row)


def tool_specs():
    return [
        {"name": "start", "description": "Run a long shell command under a detached monitor. "
         "Returns a job ID immediately. Do other work, then take the result with `wait` "
         "inside this turn. Never end the turn to be woken by its completion: a wake "
         "opens a new turn at full context and is kept through every compaction. "
         "Use the same authorization as a normal shell command. No interactive stdin. "
         "Anything the command leaves running in the background keeps running but stops "
         "being logged shortly after the command exits.",
         "inputSchema": {"type": "object", "properties": {
             "command": {"type": "string"}, "cwd": {"type": "string"},
             "thread_id": {"type": "string", "description": "Your native CODEX_THREAD_ID; never guess."},
             "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 86400, "default": 3600}},
             "required": ["command", "cwd", "thread_id"], "additionalProperties": False}},
        {"name": "wait", "description": "Block until a job ends and return its result, "
         "notification receipt and bounded log tail. The way to take a job's result: costs "
         "nothing while it waits and sends no completion event. Returns the running row "
         f"after timeout_seconds (max {WAIT_MAX}); call it again if it is still running.",
         "inputSchema": {"type": "object", "properties": {
             "job_id": {"type": "string"},
             "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": WAIT_MAX,
                                 "default": WAIT_MAX}},
             "required": ["job_id"], "additionalProperties": False}},
        {"name": "status", "description": "Read a job result, notification receipt and bounded log tail "
         "without waiting. For an explicit status request; do not poll — use wait.",
         "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}},
                         "required": ["job_id"], "additionalProperties": False}},
        {"name": "cancel", "description": "Explicitly cancel a monitored job and its process group. "
         "A completion event follows. Do not cancel merely because work is slow.",
         "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}},
                         "required": ["job_id"], "additionalProperties": False}}]


def mcp():
    # Tool calls run on threads of their own: wait() blocks for as long as a
    # job runs, and a status or cancel issued beside it must not queue behind.
    out = threading.Lock()

    def reply(request_id, result=None, error=None):
        body = {"jsonrpc": "2.0", "id": request_id}
        body.update({"error": error} if error else {"result": result})
        with out:
            print(json.dumps(body), flush=True)

    def call(request_id, params):
        try:
            # run() is deliberately not a tool here: it is a foreground
            # wrapper for commands a session runs anyway.
            functions = {"start": start, "wait": wait, "status": status, "cancel": cancel}
            value = functions[params["name"]](**params.get("arguments", {}))
            reply(request_id, {"content": [{"type": "text", "text": json.dumps(value)}]})
        except Exception as exc:
            reply(request_id, {"isError": True, "content": [{"type": "text", "text": str(exc)}]})

    calls = []
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request.get("method")
        if method == "initialize":
            reply(request["id"], {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                  "serverInfo": {"name": "jstack-job-monitor", "version": "2"}})
        elif method == "tools/list":
            reply(request["id"], {"tools": tool_specs()})
        elif method == "tools/call":
            worker_thread = threading.Thread(target=call, args=(request["id"],
                                                                request.get("params", {})))
            worker_thread.start()
            calls.append(worker_thread)
        elif method == "ping":
            reply(request["id"], {})
        else:
            reply(request["id"], error={"code": -32601, "message": "Unknown method"})
    # A client that closes stdin still gets every answer it asked for: a call
    # in flight finishes and replies before the server exits. wait is bounded.
    for worker_thread in calls:
        worker_thread.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action", required=True)
    started = subs.add_parser("start")
    started.add_argument("--command", required=True)
    started.add_argument("--cwd", default=os.getcwd())
    started.add_argument("--thread-id", default=os.environ.get("CODEX_THREAD_ID"))
    started.add_argument("--timeout-seconds", type=int, default=3600)
    # Wrap a command that might be slow. Shell-level on purpose: the caller is
    # a shim or a script, not a model choosing a tool. See run().
    wrapped = subs.add_parser("run")
    wrapped.add_argument("--command", required=True)
    wrapped.add_argument("--cwd", default=os.getcwd())
    wrapped.add_argument("--thread-id", default=os.environ.get("CODEX_THREAD_ID"))
    wrapped.add_argument("--threshold-seconds", type=int, default=THRESHOLD_SECONDS)
    wrapped.add_argument("--timeout-seconds", type=int, default=3600)
    for name in ("status", "cancel", "worker"):
        subs.add_parser(name).add_argument("job_id")
    waited = subs.add_parser("wait")
    waited.add_argument("job_id")
    waited.add_argument("--timeout-seconds", type=int, default=WAIT_MAX)
    subs.add_parser("mcp")
    args = vars(parser.parse_args())
    action = args.pop("action")
    if action == "worker":
        asyncio.run(worker(**args))
    elif action == "mcp":
        mcp()
    elif action == "run":
        raise SystemExit(run(**args))
    else:
        print(json.dumps({"start": start, "wait": wait, "status": status,
                          "cancel": cancel}[action](**args)))


if __name__ == "__main__":
    main()
