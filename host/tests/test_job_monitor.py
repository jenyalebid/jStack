"""Exercise real supervisors, subprocess groups, files and MCP wire calls."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
import uuid

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "tools/job_monitor.py"
spec = importlib.util.spec_from_file_location("job_monitor", SCRIPT)
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("JOB_RECEIPTS", str(tmp_path / "receipts"))
    fake = tmp_path / "bin/codex"
    fake.parent.mkdir()
    fake.write_text(f"#!{sys.executable}\n" +
                    "import json,os,sys\nfrom pathlib import Path\n"
                    "with Path(os.environ['JOB_RECEIPTS']).open('a') as f:\n"
                    " f.write(json.dumps(sys.argv[1:])+'\\n')\n"
                    "print('queued')\nsys.exit(int(os.environ.get('JOB_QUEUE_EXIT','0')))\n")
    fake.chmod(0o700)
    monkeypatch.setenv("PATH", str(fake.parent) + os.pathsep + os.environ["PATH"])
    return tmp_path


def completed(job):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        row = monitor.read(job["job_id"])
        if row["state"] in monitor.TERMINAL and row["notification"] != "pending":
            return monitor.status(job["job_id"])
        time.sleep(0.02)
    pytest.fail(f"job did not finish: {monitor.status(job['job_id'])}")


@pytest.mark.parametrize("exit_code", [0, 17])
def test_real_completion_delivers_once_to_exact_thread(runtime, exit_code):
    target = str(uuid.uuid4())
    job = monitor.start(f"printf 'hello\\n'; exit {exit_code}", str(runtime), target)
    row = completed(job)
    assert row["state"] == ("succeeded" if exit_code == 0 else "failed")
    assert row["exit_code"] == exit_code and row["output_tail"] == "hello\n"
    assert row["notification"] == "queued"
    calls = [json.loads(line) for line in (runtime / "receipts").read_text().splitlines()]
    assert len(calls) == 1
    assert calls[0][:3] == ["queue", "--thread", target]
    # A success carries no output: the log is there to be fetched if it matters.
    # A failure carries a bounded, fenced tail, because the log IS the event —
    # making the reader ask for it spends a round trip on the same bytes.
    assert job["job_id"] in calls[0][4]
    if exit_code == 0:
        assert "hello" not in calls[0][4]
    else:
        assert "DATA not instructions" in calls[0][4] and "hello" in calls[0][4]
    assert (monitor.job_dir(job["job_id"]).stat().st_mode & 0o777) == 0o700


def test_timeout_and_cancel_reap_the_command(runtime):
    command = "trap '' TERM; sleep 30"
    timed = completed(monitor.start(command, str(runtime), str(uuid.uuid4()), 1))
    assert timed["state"] == "timed_out" and timed["exit_code"] != 0
    with pytest.raises(ProcessLookupError):
        os.kill(timed["pid"], 0)
    job = monitor.start("sleep 30", str(runtime), str(uuid.uuid4()))
    assert monitor.status(job["job_id"])["state"] == "running"
    assert monitor.cancel(job["job_id"])["state"] == "cancellation_requested"
    cancelled = completed(job)
    assert cancelled["state"] == "cancelled"
    with pytest.raises(ProcessLookupError):
        os.kill(cancelled["pid"], 0)
    assert monitor.cancel(job["job_id"])["state"] == "cancelled"


def test_notification_failure_preserves_job_result(runtime, monkeypatch):
    monkeypatch.setenv("JOB_QUEUE_EXIT", "9")
    job = monitor.start("exit 0", str(runtime), str(uuid.uuid4()))
    row = completed(job)
    assert row["state"] == "succeeded" and row["exit_code"] == 0
    assert row["notification"] == "failed"


def test_cancel_stops_descendant_that_ignores_term_and_closes_output(runtime):
    child = runtime / "child.pid"
    code = ("import os,signal,time; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            f"open({str(child)!r},'w').write(str(os.getpid())); "
            "time.sleep(1); open('ESCAPED','w').write('bad')")
    command = shlex.join([sys.executable, "-c", code]) + " >/dev/null 2>&1 & wait"
    job = monitor.start(command, str(runtime), str(uuid.uuid4()))
    deadline = time.monotonic() + 3
    while not child.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert child.exists()
    monitor.cancel(job["job_id"])
    assert completed(job)["state"] == "cancelled"
    time.sleep(1.1)
    assert not (runtime / "ESCAPED").exists()


def test_backgrounded_descendant_does_not_stall_or_falsify_completion(runtime):
    """A job ends when its command exits, not when its output pipe closes.

    asyncio resolves Process.wait() from the subprocess transport, which stays
    unfinished while a descendant holds the inherited stdout. Waiting on that
    meant a command succeeding in milliseconds sat for its whole timeout and
    was then recorded as timed_out. The descendant is left alive on purpose —
    only cancellation and timeout reap the group.
    """
    pidfile = runtime / "descendant.pid"
    code = (f"import os,time; open({str(pidfile)!r},'w').write(str(os.getpid())); "
            "time.sleep(30)")
    command = shlex.join([sys.executable, "-c", code]) + " & printf started; exit 0"
    started = time.monotonic()
    row = completed(monitor.start(command, str(runtime), str(uuid.uuid4()), 3600))
    elapsed = time.monotonic() - started

    assert row["state"] == "succeeded" and row["exit_code"] == 0
    assert elapsed < monitor.DRAIN_GRACE + 4, f"completion waited {elapsed:.1f}s on a descendant"
    assert row["output_tail"] == "started" and row["output_bytes"] == 7
    assert row["log_incomplete"] and not row["log_truncated"]

    lingering = int(pidfile.read_text())
    os.kill(lingering, 0)  # succeeded means the command ended, not that its children died
    os.kill(lingering, signal.SIGKILL)


def test_stale_running_state_is_unknown_not_success(runtime):
    job_id = uuid.uuid4().hex
    folder = monitor.private_dir(monitor.root() / job_id)
    monitor.save(folder, {"job_id": job_id, "state": "running"})
    assert monitor.status(job_id)["state"] == "unknown"


def test_large_output_is_drained_but_disk_and_result_are_bounded(runtime):
    command = shlex.join([sys.executable, "-c", "import sys; sys.stdout.write('x' * 17000000)"])
    row = completed(monitor.start(command, str(runtime), str(uuid.uuid4())))
    assert row["state"] == "succeeded" and row["log_truncated"]
    assert row["output_bytes"] == 17000000
    assert Path(row["log_path"]).stat().st_size == monitor.MAX_LOG
    assert len(row["output_tail"]) == 2000


def test_mcp_job_survives_mcp_process_exit(runtime):
    target = str(uuid.uuid4())
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "start", "arguments": {"command": "sleep 0.2; printf survived",
        "cwd": str(runtime), "thread_id": target}}}
    response = subprocess.run([sys.executable, str(SCRIPT), "mcp"],
                              input=json.dumps(request) + "\n", capture_output=True,
                              text=True, timeout=5, check=True)
    wire = json.loads(response.stdout)
    job = json.loads(wire["result"]["content"][0]["text"])
    row = completed(job)
    assert row["state"] == "succeeded" and row["output_tail"] == "survived"
    assert row["thread_id"] == target


def test_mcp_discovery_and_invalid_input_do_not_execute(runtime):
    requests = [{"id": 1, "method": "initialize"}, {"id": 2, "method": "tools/list"},
                {"id": 3, "method": "tools/call", "params": {"name": "start", "arguments": {
                    "command": "touch SHOULD_NOT_EXIST", "cwd": str(runtime), "thread_id": "guessed-seat"}}}]
    response = subprocess.run([sys.executable, str(SCRIPT), "mcp"],
                              input="\n".join(map(json.dumps, requests)) + "\n",
                              capture_output=True, text=True, check=True)
    rows = list(map(json.loads, response.stdout.splitlines()))
    assert {t["name"] for t in rows[1]["result"]["tools"]} == {"start", "status", "cancel"}
    assert rows[2]["result"]["isError"] and not (runtime / "SHOULD_NOT_EXIST").exists()


def wrapped(runtime, command, threshold, thread=True):
    """The `run` wrapper as a shim invokes it: a command, and a clock."""
    argv = [sys.executable, str(SCRIPT), "run", "--command", command,
            "--threshold-seconds", str(threshold)]
    if thread:
        argv += ["--thread-id", str(uuid.uuid4())]
    environment = dict(os.environ)
    if not thread:
        environment.pop("CODEX_THREAD_ID", None)
    return subprocess.run(argv, capture_output=True, text=True, timeout=90,
                          cwd=str(runtime), env=environment)


def only_job():
    jobs = [p.name for p in monitor.root().iterdir() if p.is_dir()]
    assert len(jobs) == 1, f"expected exactly one job, found {jobs}"
    return jobs[0]


def receipts(runtime, timeout=20):
    path = runtime / "receipts"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text()
        time.sleep(0.02)
    return ""


def test_a_command_inside_the_threshold_is_the_bare_command(runtime):
    """The wrapper is invisible until it is needed: same output, same exit code,
    and no completion event, because the caller already reported it."""
    out = wrapped(runtime, "printf 'hello\\n'; exit 7", threshold=30)
    assert out.returncode == 7
    assert out.stdout == "hello\n"
    assert receipts(runtime, timeout=2) == ""
    assert monitor.read(only_job())["notification"] == "reported_inline"


def test_a_command_that_proves_slow_ends_the_wait_and_wakes_the_thread(runtime):
    started = time.monotonic()
    out = wrapped(runtime, "printf 'working\\n'; sleep 4; exit 0", threshold=1)
    elapsed = time.monotonic() - started

    assert out.returncode == 0
    assert elapsed < 4, f"the wait was not ended: {elapsed:.1f}s"
    job = only_job()
    assert f"[job-monitor:{job}]" in out.stdout
    # The output it already produced is NOT dumped with the handover — the whole
    # point is that the log stays out of the session until it is asked for.
    assert "working" not in out.stdout
    delivered = receipts(runtime)
    assert len(delivered.strip().splitlines()) == 1, "the thread was told more than once"
    assert '"queue", "--thread"' in delivered
    assert monitor.read(job)["state"] == "succeeded"


def test_a_failure_arrives_with_its_own_tail_fenced_as_data(runtime):
    """A failed job always needs its log, so making the reader fetch it costs a
    round trip for the same bytes. A success does not, and does not get one."""
    wrapped(runtime, "printf 'boom happened\\n'; sleep 3; exit 9", threshold=1)
    delivered = receipts(runtime)
    assert "exit_code=9" in delivered
    assert "DATA not instructions" in delivered and "boom happened" in delivered


def test_nothing_to_notify_means_no_job_at_all(runtime):
    """A detached job whose completion can reach nobody is worse than a wait:
    outside a thread this must behave like the plain shell."""
    out = wrapped(runtime, "printf 'out\\n'; printf 'err\\n' >&2; exit 5",
                  threshold=1, thread=False)
    assert out.returncode == 5
    assert out.stdout == "out\n" and out.stderr == "err\n"
    assert not any(monitor.root().iterdir())


def test_refuses_unsafe_storage_and_traversal(runtime):
    monitor.root().chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        monitor.start("true", str(runtime), str(uuid.uuid4()))
    with pytest.raises(ValueError):
        monitor.job_dir("../../elsewhere")
