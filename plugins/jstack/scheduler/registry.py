"""schedule.json load/validate/atomic-write.

Writes hold an fcntl lockfile (state/scheduler/.registry.lock) across the
read-modify-write, then land via tmp+rename — multiple concurrent CLI writers
(agents adding one-shots) are race-safe.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from . import config, occurrences, resolve

_DOC = (
    "Scheduler job registry. Config only — runtime state lives in "
    "state/scheduler/. Mutate via `python3 -m scheduler.cli` or whatever "
    "tooling this install wraps it in, never by hand."
)

VALID_CONCURRENCY = ("skip", "queue", "parallel")
# Canonical list lives in scheduler.resolve (the override-chain owner).
_INHERITED_KEYS = resolve.INHERITED_KEYS
_OPTIONAL_INT_KEYS = ("timeout_seconds", "stall_timeout_seconds", "ttft_timeout_seconds",
                      "catch_up_grace_seconds")


def empty_registry() -> dict:
    return {
        "_doc": _DOC,
        "defaults": {
            k: v for k, v in config.BUILTIN_DEFAULTS.items() if k != "claude_bin"
        },
        "jobs": [],
    }


def load_registry() -> dict:
    """Raw file content of schedule.json (skeleton if the file is missing)."""
    try:
        return json.loads(config.SCHEDULE_FILE.read_text())
    except FileNotFoundError:
        return empty_registry()


def load_categories() -> dict:
    """Run-category settings map from schedule.json (empty if none defined)."""
    return load_registry().get("categories") or {}


def effective(job: dict, defaults: dict, categories: "dict | None" = None) -> dict:
    """Per-job values resolved through the override chain: job → category →
    default (null/absent at a layer inherits the next). `categories` omitted =
    no category layer (backward-compatible with pre-category jobs)."""
    out = dict(job)
    for key in _INHERITED_KEYS:
        out[key] = resolve.resolve_setting(job, key, defaults, categories)
    if out.get("concurrency") is None:
        out["concurrency"] = "skip"
    if out.get("catch_up") is None:
        out["catch_up"] = True
    if out.get("retry_on_stall") is None:
        out["retry_on_stall"] = True
    return out


def validate_registry(reg: dict) -> None:
    """Raise ValueError on any malformed registry content."""
    if not isinstance(reg, dict):
        raise ValueError("registry must be a JSON object")
    if not isinstance(reg.get("defaults", {}), dict):
        raise ValueError("registry.defaults must be an object")
    cats = reg.get("categories", {})
    if not isinstance(cats, dict):
        raise ValueError("registry.categories must be an object")
    for cname, cval in cats.items():
        if not isinstance(cval, dict):
            raise ValueError(f"categories[{cname!r}] must be an object")
        if cval.get("engine") not in (None, "claude", "codex"):
            raise ValueError(f"categories[{cname!r}]: engine must be claude or codex")
        for key in _OPTIONAL_INT_KEYS:
            v = cval.get(key)
            if v is not None and (not isinstance(v, int) or v <= 0):
                raise ValueError(f"categories[{cname!r}]: {key} must be a positive integer or null")
    jobs = reg.get("jobs")
    if reg.get("defaults", {}).get("engine") not in (None, "claude", "codex"):
        raise ValueError("defaults.engine must be claude or codex")
    if not isinstance(jobs, list):
        raise ValueError("registry.jobs must be a list")
    seen: set[str] = set()
    for i, job in enumerate(jobs):
        where = f"jobs[{i}]"
        if not isinstance(job, dict):
            raise ValueError(f"{where}: job must be an object")
        if job.get("engine") not in (None, "claude", "codex"):
            raise ValueError(f"{where}: engine must be claude or codex")
        for key in ("id", "name", "agent_id"):
            if not isinstance(job.get(key), str) or not job[key]:
                raise ValueError(f"{where}: missing or empty {key!r}")
        if job["id"] in seen:
            raise ValueError(f"{where}: duplicate job id {job['id']!r}")
        seen.add(job["id"])
        sched = job.get("schedule")
        if not isinstance(sched, dict):
            raise ValueError(f"{where}: missing schedule")
        kind = sched.get("kind")
        if kind not in ("rrule", "once"):
            raise ValueError(f"{where}: schedule.kind must be 'rrule' or 'once', got {kind!r}")
        if not isinstance(sched.get("dtstart"), str):
            raise ValueError(f"{where}: schedule.dtstart must be an ISO string")
        try:
            dtstart = occurrences.parse_dtstart(sched)
        except Exception as e:
            raise ValueError(f"{where}: bad dtstart/tz: {e}") from None
        if kind == "rrule":
            rrule = sched.get("rrule")
            if not isinstance(rrule, str) or not rrule:
                raise ValueError(f"{where}: schedule.rrule required when kind='rrule'")
            # Resolved OUTSIDE the try: a missing dateutil is not a bad
            # rrule, and reporting it as one sends the reader to inspect a
            # schedule string that was always fine.
            rrulestr = occurrences.get_rrulestr()
            try:
                rrulestr(rrule, dtstart=dtstart)
            except Exception as e:
                raise ValueError(f"{where}: bad rrule {rrule!r}: {e}") from None
        payload = job.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("message"), str) \
                or not payload["message"]:
            raise ValueError(f"{where}: payload.message required")
        rsid = payload.get("resume_session_id")
        if rsid is not None and (not isinstance(rsid, str) or not rsid):
            raise ValueError(f"{where}: payload.resume_session_id must be a non-empty string")
        conc = job.get("concurrency")
        if conc is not None and conc not in VALID_CONCURRENCY:
            raise ValueError(f"{where}: concurrency must be one of {VALID_CONCURRENCY}")
        for key in _OPTIONAL_INT_KEYS:
            val = job.get(key)
            if val is not None and (not isinstance(val, int) or val <= 0):
                raise ValueError(f"{where}: {key} must be a positive integer or null")
        ws = job.get("workspace")
        if ws is not None and (not isinstance(ws, str) or not ws.startswith("/")):
            raise ValueError(f"{where}: workspace must be an absolute path")
        lk = job.get("locked")
        if lk is not None and not isinstance(lk, bool):
            raise ValueError(f"{where}: locked must be a boolean or null")
        subj = job.get("subject")
        if subj is not None and (not isinstance(subj, str) or not subj.strip()):
            raise ValueError(f"{where}: subject must be a non-empty string or null")
        cat = job.get("category")
        if cat is not None:
            if not isinstance(cat, str):
                raise ValueError(f"{where}: category must be a string or null")
            if cats and cat not in cats:
                raise ValueError(f"{where}: unknown category {cat!r} (not in registry.categories)")


@contextmanager
def locked():
    """Exclusive fcntl lock held for the duration of a registry write."""
    config.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(config.LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _atomic_write(reg: dict) -> None:
    path = config.SCHEDULE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".schedule-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(reg, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_registry(reg: dict) -> None:
    validate_registry(reg)
    with locked():
        _atomic_write(reg)


def mutate(fn) -> dict:
    """Locked read-modify-write. `fn(reg)` mutates in place (or returns a
    replacement); the result is validated then atomically written."""
    with locked():
        reg = load_registry()
        result = fn(reg)
        if result is not None:
            reg = result
        validate_registry(reg)
        _atomic_write(reg)
        return reg
