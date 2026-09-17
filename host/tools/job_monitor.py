#!/usr/bin/env python3
"""Durable shell jobs with one completion message to the originating Codex thread.

Standard-library only. CLI and MCP use the same detached supervisor. No model
polling, terminal keystrokes, inferred recipient, or concurrent thread resume.

A job ends when its command exits, never when its output pipe closes — those
are different moments whenever the command backgrounds something, and only the
first one is the job.
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
import time
import uuid


TERMINAL = {"succeeded", "failed", "timed_out", "cancelled", "launch_failed"}
MAX_LOG = 16 * 1024 * 1024
# How long to keep reading stdout after the command itself has exited, and how
# often to ask whether it has. Both exist because output outlives the command:
# see leader_exit and settle.
DRAIN_GRACE = 5
EXIT_POLL = 0.05


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


def status(job_id):
    row = read(job_id)
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
    log = job_dir(job_id) / "output.log"
    if log.exists():
        with log.open("rb") as stream:
            stream.seek(max(0, log.stat().st_size - 2000))
            row["output_tail"] = stream.read(2000).decode("utf-8", errors="replace")
    return row


def start(command, cwd, thread_id, timeout_seconds=3600):
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

    # Output is deliberately NOT injected as instructions. Retrieve only the
    # relevant log after this small event, using status or the returned path.
    message = (f"[job-monitor:{job_id}] Background job {row['state']}; "
               f"exit_code={row.get('exit_code')}. Log: {row['log_path']}. "
               "This is a completion event for work you started. Inspect its result "
               "and continue the existing task; do not rerun the command automatically.")
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
         "Returns a job ID immediately; one completion message is queued to the exact native "
         "Codex thread when it ends. Do other work or yield the turn; do not poll. "
         "Use the same authorization as a normal shell command. No interactive stdin. "
         "Anything the command leaves running in the background keeps running but stops "
         "being logged shortly after the command exits.",
         "inputSchema": {"type": "object", "properties": {
             "command": {"type": "string"}, "cwd": {"type": "string"},
             "thread_id": {"type": "string", "description": "Your native CODEX_THREAD_ID; never guess."},
             "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 86400, "default": 3600}},
             "required": ["command", "cwd", "thread_id"], "additionalProperties": False}},
        {"name": "status", "description": "Read a job result, notification receipt and bounded log tail. "
         "Use after its completion event or for an explicit status request; do not poll.",
         "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}},
                         "required": ["job_id"], "additionalProperties": False}},
        {"name": "cancel", "description": "Explicitly cancel a monitored job and its process group. "
         "A completion event follows. Do not cancel merely because work is slow.",
         "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}},
                         "required": ["job_id"], "additionalProperties": False}}]


def mcp():
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "jstack-job-monitor", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": tool_specs()}
        elif method == "tools/call":
            params = request.get("params", {})
            try:
                functions = {"start": start, "status": status, "cancel": cancel}
                value = functions[params["name"]](**params.get("arguments", {}))
                result = {"content": [{"type": "text", "text": json.dumps(value)}]}
            except Exception as exc:
                result = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
        elif method == "ping":
            result = {}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"],
                              "error": {"code": -32601, "message": "Unknown method"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action", required=True)
    run = subs.add_parser("start")
    run.add_argument("--command", required=True)
    run.add_argument("--cwd", default=os.getcwd())
    run.add_argument("--thread-id", default=os.environ.get("CODEX_THREAD_ID"))
    run.add_argument("--timeout-seconds", type=int, default=3600)
    for name in ("status", "cancel", "worker"):
        subs.add_parser(name).add_argument("job_id")
    subs.add_parser("mcp")
    args = vars(parser.parse_args())
    action = args.pop("action")
    if action == "worker":
        asyncio.run(worker(**args))
    elif action == "mcp":
        mcp()
    else:
        print(json.dumps({"start": start, "status": status, "cancel": cancel}[action](**args)))


if __name__ == "__main__":
    main()
