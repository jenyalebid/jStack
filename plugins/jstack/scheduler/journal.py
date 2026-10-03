"""Runs journal (state/scheduler/runs/{jobId}.jsonl) + state.json.

Record shapes are a CONTRACT — cron_outcome_validator parses `finished`
records by field name (ts epoch-ms, action, runAtMs, durationMs). Do not
rename fields.
"""

from __future__ import annotations

import json
import os
import tempfile
import time

from . import config


def _now_ms() -> int:
    return int(time.time() * 1000)


def append(job_id: str, record: dict) -> None:
    config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.RUNS_DIR / f"{job_id}.jsonl"
    with path.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def started_record(*, job_id: str, run_id: str, session_id: str, run_at_ms: int,
                   spawned_at_ms: int, pid: int, pgid: int,
                   job_name: str = "", booked_by: "dict|None" = None) -> dict:
    # `jobName` and `bookedBy` are copied off the job at spawn: a one-shot is
    # deleted from the registry once it fires, and this row is then the only
    # place that still links the run's session to the session that booked it.
    rec = {
        "ts": _now_ms(),
        "jobId": job_id,
        "action": "started",
        "runId": run_id,
        "sessionId": session_id,
        "runAtMs": run_at_ms,
        "spawnedAtMs": spawned_at_ms,
        "pid": pid,
        "pgid": pgid,
    }
    if job_name:
        rec["jobName"] = job_name
    if booked_by:
        rec["bookedBy"] = booked_by
    return rec


def finished_record(*, job_id: str, agent_id: str, status: str, summary: str,
                    session_id: str, run_at_ms: int, duration_ms: int,
                    next_run_at_ms: "int|None", model: str, run_id: str,
                    spawned_at_ms: int, exit_code: "int|None",
                    kill_reason: "str|None", retry_of: "str|None",
                    delivery: str = "not-requested") -> dict:
    # `delivery` is the failure-notifier outcome the engine computed for this
    # finish: "not-requested" (the default — opt-out, or an ok run), else
    # "delivered" / "failed" / "no-notifier". delivered mirrors it so a reader
    # need not know the vocabulary to answer "did anyone hear about this".
    return {
        "ts": _now_ms(),
        "jobId": job_id,
        "action": "finished",
        "status": status,
        "summary": summary,
        "delivered": delivery == "delivered",
        "deliveryStatus": delivery,
        "sessionId": session_id,
        "sessionKey": f"agent:{agent_id}:cron:{job_id}:run:{session_id}",
        "runAtMs": run_at_ms,
        "durationMs": duration_ms,
        "nextRunAtMs": next_run_at_ms,
        "model": model,
        "provider": "jstack-scheduler",
        "runId": run_id,
        "spawnedAtMs": spawned_at_ms,
        "exitCode": exit_code,
        "killReason": kill_reason,
        "retryOf": retry_of,
    }


def skipped_record(*, job_id: str, run_at_ms: int, reason: str) -> dict:
    return {
        "ts": _now_ms(),
        "jobId": job_id,
        "action": "skipped",
        "runAtMs": run_at_ms,
        "reason": reason,
    }


def read_history(limit: int = 50, job_id: "str|None" = None) -> "list[dict]":
    """Finished records, newest first."""
    if job_id is not None:
        paths = [config.RUNS_DIR / f"{job_id}.jsonl"]
    elif config.RUNS_DIR.is_dir():
        paths = sorted(config.RUNS_DIR.glob("*.jsonl"))
    else:
        paths = []
    records: list[dict] = []
    for path in paths:
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("action") == "finished":
                records.append(rec)
    records.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return records[:limit]


def load_state() -> dict:
    try:
        return json.loads(config.STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(config.STATE_DIR), prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, config.STATE_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
