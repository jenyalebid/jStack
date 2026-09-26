"""Durable hub update jobs and observed inventory, not installation code.

SQLite serializes requests across the API and the independent supervisor.
Offline machines keep queued intent. A report alone cannot complete a job:
the hub must independently observe the running host after reconnection.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import hostenv, release_manifest as releases

ACTIVE = {"pending", "downloading", "applying", "verifying"}
TERMINAL = {"current", "failed", "rolled_back", "cancelled"}
TRANSITIONS = {
    "pending": {"downloading", "failed", "cancelled"},
    # The leaf can cross into applying just as its parent restarts. Its local
    # rollback is then the first durable state the parent hears after still
    # holding "downloading"; accepting that report releases the stuck job.
    "downloading": {"applying", "failed", "rolled_back", "cancelled"},
    "applying": {"verifying", "failed", "rolled_back"},
    "verifying": {"current", "failed", "rolled_back"},
}
STALE_SECONDS = 90


def root() -> Path:
    return hostenv.state_dir() / "updates"


def config() -> dict:
    path = root() / "config.json"
    if not path.exists():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise releases.ReleaseError("invalid updater configuration")
    return value


def feed_dir() -> Path:
    from . import releases as app_releases
    return app_releases.RELEASE_DIR.parent / "fleet"


def latest_path(line: str = releases.STABLE_CHANNEL) -> Path:
    """Where a line's offer sits in this hub's feed: `latest.json` for main —
    the file every leaf that predates lines already reads — and
    `latest-dev.json` for dev."""
    from .build_source import latest_name
    return feed_dir() / latest_name(line)


def machine_line(observation: dict | None) -> str:
    """The line a machine reported in its heartbeat; main when it names none,
    which is every machine whose updater predates lines."""
    value = (observation or {}).get("line")
    if value == "stable":
        return releases.STABLE_CHANNEL
    return value if value in releases.LINES else releases.STABLE_CHANNEL


def pre_lines_offer(line: str) -> Path | None:
    """A hub that predated lines wrote whatever it built, dev included, into
    the one `latest.json`. When such a hub moves onto lines while following
    dev, that file is still dev's offer — its manifest names the line it was
    built from — until the hub builds again and dev gets its own file. Read
    where it is, so the hub's own row is not "not_published" the moment it
    lands on this code (hub/update, 2026-09-26)."""
    if line == releases.STABLE_CHANNEL:
        return None
    path = latest_path(releases.STABLE_CHANNEL)
    if not path.exists():
        return None
    try:
        envelope = json.loads(path.read_text())
    except ValueError:
        return None
    manifest = envelope.get("manifest") if isinstance(envelope, dict) else None
    channel = (manifest or {}).get("channel") or {}
    return path if isinstance(channel, dict) and channel.get("name") == line else None


def offer(line: str = releases.STABLE_CHANNEL) -> dict | None:
    path = latest_path(line)
    if not path.exists():
        path = pre_lines_offer(line)
        if path is None:
            return None
    envelope = json.loads(path.read_text())
    settings = config()
    manifest = releases.verify(envelope, settings.get("public_key", ""),
                               promoted=not settings.get("candidate_test", False))
    for item in manifest["components"].values():
        artifact = feed_dir() / manifest["release"] / item["file"]
        if not artifact.is_file() or artifact.stat().st_size != item["bytes"]:
            raise releases.ReleaseError("published release has missing or partial artifacts")
    return envelope


class FleetStore:
    def __init__(self, path: Path | None = None):
        self.path = path or root() / "fleet.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connection() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS reports (
                machine TEXT PRIMARY KEY, seen REAL NOT NULL, report TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, request TEXT NOT NULL, machine TEXT NOT NULL,
                authority TEXT NOT NULL, release TEXT NOT NULL, envelope TEXT NOT NULL,
                state TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL, updated REAL NOT NULL,
                UNIQUE(request, machine));
              CREATE TABLE IF NOT EXISTS requests (
                request TEXT NOT NULL, machine TEXT NOT NULL, job TEXT NOT NULL,
                PRIMARY KEY(request, machine));
              INSERT OR IGNORE INTO requests SELECT request, machine, id FROM jobs;
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def queue(self, machine: str, authority: str, envelope: dict, request: str) -> dict:
        releases.identifier(request)
        release = envelope["manifest"]["release"]
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT jobs.* FROM jobs JOIN requests ON jobs.id=requests.job "
                             "WHERE requests.request=? AND requests.machine=?",
                             (request, machine)).fetchone()
            if old:
                if old["release"] != release or old["authority"] != authority:
                    raise releases.ReleaseError("request ID already names a different update")
                return dict(old)
            current = db.execute("SELECT * FROM jobs WHERE machine=? AND authority=? AND release=? "
                                 "AND state='current' ORDER BY created DESC LIMIT 1",
                                 (machine, authority, release)).fetchone()
            report = db.execute("SELECT * FROM reports WHERE machine=?", (machine,)).fetchone()
            if current and report and time.time() - report["seen"] < STALE_SECONDS:
                observation = json.loads(report["report"])
                if observation.get("verified") is True and observation.get("release") == release:
                    db.execute("INSERT INTO requests VALUES (?,?,?)", (request, machine, current["id"]))
                    return dict(current)
            busy = db.execute("SELECT * FROM jobs WHERE machine=? AND state IN "
                              "('pending','downloading','applying','verifying')", (machine,)).fetchone()
            if busy:
                if busy["release"] == release and busy["authority"] == authority:
                    db.execute("INSERT INTO requests VALUES (?,?,?)", (request, machine, busy["id"]))
                    return dict(busy)
                raise releases.ReleaseError("machine already has an active update")
            now = time.time()
            job = {"id": uuid.uuid4().hex, "request": request, "machine": machine,
                   "authority": authority, "release": release,
                   "envelope": json.dumps(envelope), "state": "pending", "detail": "",
                   "created": now, "updated": now}
            db.execute("INSERT INTO jobs VALUES (:id,:request,:machine,:authority,:release,"
                       ":envelope,:state,:detail,:created,:updated)", job)
            db.execute("INSERT INTO requests VALUES (?,?,?)", (request, machine, job["id"]))
            return job

    def latest(self, machine: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE machine=? ORDER BY created DESC LIMIT 1",
                             (machine,)).fetchone()
            return dict(row) if row else None

    def transition(self, job_id: str, machine: str, state: str, detail: str = "", *,
                   verified: bool = False) -> dict:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=? AND machine=?", (job_id, machine)).fetchone()
            if row is None:
                raise releases.ReleaseError("unknown update job")
            if state == "current" and not verified:
                raise releases.ReleaseError("hub verification required")
            if state != row["state"] and state not in TRANSITIONS.get(row["state"], set()):
                raise releases.ReleaseError(f"invalid update transition: {row['state']} to {state}")
            db.execute("UPDATE jobs SET state=?,detail=?,updated=? WHERE id=?",
                       (state, detail[:2000], time.time(), job_id))
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def report(self, machine: str, observation: dict) -> None:
        encoded = json.dumps(observation, allow_nan=False)
        if len(encoded) > 32768:
            raise releases.ReleaseError("inventory report too large")
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO reports VALUES (?,?,?)",
                       (machine, time.time(), encoded))

    def observed(self, machine: str) -> dict:
        """The last report a machine sent, or `{}` if it never sent one."""
        with self.connection() as db:
            row = db.execute("SELECT report FROM reports WHERE machine=?", (machine,)).fetchone()
        return json.loads(row["report"]) if row else {}

    def line(self, machine: str) -> str:
        """The line a machine is on, as its own last heartbeat said."""
        return machine_line(self.observed(machine))

    def inventory(self, machine: str, name: str, desired: str | None,
                  line: str | None = None) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT * FROM reports WHERE machine=?", (machine,)).fetchone()
        report = json.loads(row["report"]) if row else {}
        line = line or machine_line(report)
        job = self.latest(machine)
        if job and job["authority"] != "local" and job["state"] in {"pending", "downloading"}:
            from . import devices, managed_access
            authority = devices.row(job["authority"])
            leaf = managed_access.leaf_for_device(job["authority"])
            if (authority is None or authority.get("revoked_at") is not None or
                    leaf is None or leaf["deleted"] or leaf["key"] != machine):
                job = self.transition(job["id"], machine, "cancelled", "adoption authority revoked")
        fresh = bool(row and time.time() - row["seen"] < STALE_SECONDS)
        state = "not_published" if fresh and not desired else "unknown"
        # A machine with no job history that already reports the desired
        # release is current, not updatable: a fresh install arrives at the
        # release by the installer, never the updater, so it has no job and
        # no verification — and must not advertise an update to itself. The
        # verification gate below stays for job-claimed "current", where the
        # claim is a leaf's word about a transition rather than the release
        # stamp its host process observed on itself.
        installed_as_desired = False
        if job:
            state = job["state"]
        elif report.get("job", {}).get("state"):
            state = report["job"]["state"]
        elif fresh and desired:
            installed_as_desired = report.get("release") == desired
            state = "current" if installed_as_desired else "available"
        if not fresh and state not in {"cancelled", "failed", "rolled_back"}:
            state = "pending/offline" if job and job["state"] in ACTIVE else "unknown/offline"
        elif (state == "current" and not installed_as_desired
              and (report.get("release") != desired or
                   report.get("verified") is not True)):
            state = "available" if desired else "not_published"
        return {"machine": machine, "name": name, "line": line, "desired": desired, "state": state,
                "last_contact": row["seen"] if row else None, "observed": report,
                "job": public_job(job), "supervisor": report.get("supervisor") == 1,
                "contact_status": "online" if fresh else "offline" if row else "not_observed"}


def public_job(job: dict | None) -> dict | None:
    return {k: v for k, v in job.items() if k not in {"envelope", "authority"}} if job else None


def local_observation() -> dict:
    from . import sourcestamp
    path = root() / "observed.json"
    result = json.loads(path.read_text()) if path.exists() else {}
    # Served source is observed by the host process, never supplied by a client.
    return {**result, "host_source": sourcestamp.capture()}
