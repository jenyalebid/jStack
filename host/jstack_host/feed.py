"""The day feed — everything this host did, on a day scale, as one stream.

The app's Timeline tab. A machine running jStack already records what it does,
in stores that never talk to each other: the timeline narrates (`log_event`),
the session index knows what spawned and what it was asked to do, git knows
what shipped, the scheduler knows how its wakes ended, the inbox carries
agent-to-agent traffic. Each answers a slice of "what happened today"; this
merges them into one ordered stream, so a phone can read a day.

This is the one reader, on every host. It asks `hostenv` for every machine
fact — where the timeline is, which checkouts are ours, which project and
agent a checkout files under, where the scheduler journals, whether a ping
lane exists — so the same code serves a host with nothing but jStack
installed and a dashboard-embedded host alike.

**One event shape, six producers.** Every source flattens to the same dict,
so a reader never branches on where a row came from:

    id          str    "source:key" — stable across refetch, the client's identity
    ts          str    local ISO microseconds — the host's universal stamp shape
    source      str    which producer (the filter facet)
    kind        str    finer subtype within the source
    agent       str    agent id, "" when the event has no agent
    seat        str    "ops/chat", "" when unseated
    project     str    project slug, "" when the event belongs to none
    title       str    one line, the log line itself
    body        str    optional depth, empty when there is none
    session_id  str    optional — what a tap opens
    meta        dict   source-specific extras (sha, repo, status, verdict…)

**What is indexed and what is read live.** Only the two expensive producers
are indexed here: git (a subprocess per changed repo) and the scheduler
journal (one file per job). The other four already live in indexed sqlite
and are queried at request time, read-only, from the store that owns them.

**Reads never write.** Every foreign store is opened `mode=ro` through a URI —
"the timeline has exactly one writer" is structural here, not a rule someone
has to remember. The only db this module writes is its own, in the state dir.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import hostenv

DB_PATH = hostenv.state_dir() / "feed.sqlite"

# Absolute, because the host must never depend on PATH for this. `git log` is a
# pure read; `git status` is NOT (it writes the index stat cache) and is never
# called from here.
GIT = "/usr/bin/git"

# How often the indexer looks for new commits and finished runs. Tuned for
# what it watches — a dozen git repos, not a tailing transcript.
TICK = 45.0

# The furthest back a first pass walks. The feed is a day lens; the cap is
# what stops the first run from spending minutes indexing years nobody will
# scroll to.
BACKFILL_DAYS = 14

_SCHEMA = """
CREATE TABLE IF NOT EXISTS commits (
  id TEXT PRIMARY KEY,          -- "<repo>:<sha>", stable across reindex
  repo TEXT NOT NULL DEFAULT '',
  repo_path TEXT NOT NULL DEFAULT '',
  sha TEXT NOT NULL DEFAULT '',
  ts TEXT NOT NULL DEFAULT '',  -- local ISO, sortable, same shape as every other source
  day TEXT NOT NULL DEFAULT '',
  author TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  project TEXT NOT NULL DEFAULT '',
  agent TEXT NOT NULL DEFAULT '',
  files INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS commits_day ON commits(day, ts DESC);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,          -- "<job>:<run>:<action>"
  job_id TEXT NOT NULL DEFAULT '',
  run_id TEXT NOT NULL DEFAULT '',
  ts TEXT NOT NULL DEFAULT '',
  day TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '',
  agent TEXT NOT NULL DEFAULT '',
  label TEXT NOT NULL DEFAULT '',
  session_id TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  duration_ms INTEGER NOT NULL DEFAULT 0,
  exit_code INTEGER NOT NULL DEFAULT 0,
  kill_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS runs_day ON runs(day, ts DESC);
-- What the indexer already folded in, so an unchanged tick costs stats only.
CREATE TABLE IF NOT EXISTS watermarks (
  key TEXT PRIMARY KEY,
  stamp TEXT NOT NULL DEFAULT ''
);
"""


# ── stamps ──

def _local_iso(epoch: float) -> str:
    """The host's universal stamp shape. Every source lands on this, so the
    merge is one sort and clients compare stamps lexicographically."""
    return datetime.fromtimestamp(epoch).isoformat(timespec="microseconds")


def _day_bounds(day: str) -> tuple[str, str]:
    """[start, end) as local ISO strings for a YYYY-MM-DD local day."""
    start = datetime.strptime(day, "%Y-%m-%d")
    return (start.isoformat(timespec="microseconds"),
            (start + timedelta(days=1)).isoformat(timespec="microseconds"))


def _utc_to_local_iso(ts_utc: str) -> str:
    """Ping rows stamp UTC; every other source is local. Converting on read
    keeps one timeline rather than a feed that jumps hours mid-scroll."""
    if not ts_utc:
        return ""
    try:
        parsed = datetime.fromisoformat(ts_utc.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().isoformat(timespec="microseconds")


# ── foreign stores, opened read-only ──

def _ro(path: Path | None) -> sqlite3.Connection | None:
    """A read-only connection, or None when the store isn't there.

    `mode=ro` is the structural half of "this module never writes a foreign
    store" — a stray INSERT raises instead of corrupting someone's source of
    truth. The timeout means a WAL checkpoint on the writer's side delays a
    read rather than failing it."""
    if path is None or not path.exists():
        return None
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        return db
    except sqlite3.Error:
        return None


def _seat(agent: str, submode: str) -> str:
    return f"{agent}/{submode}" if agent and submode else (agent or "")


def _one_line(text: str, cap: int = 140) -> str:
    """A log line is one line. Collapse and clip, never wrap."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= cap else flat[:cap - 1].rstrip() + "…"


def _entry_commits(raw: str) -> list[dict]:
    """The shas an entry says it shipped, as `[{sha, repo, subject}, ...]`.

    Shortened here rather than in the client, because the same eight characters
    identify a commit in the commit rows this feed already serves — an entry
    naming a full sha and a commit row naming a short one would not join, and
    the join is the whole point of carrying them."""
    try:
        val = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return [{"sha": c["sha"][:8], "repo": c.get("repo") or "",
             "subject": c.get("subject") or ""}
            for c in val if isinstance(c, dict) and c.get("sha")]


# ── producers ──
#
# Each takes the day's [start, end) and returns events. A producer that cannot
# read its store returns [] — the feed degrades to the sources that answered,
# and `sources` in the payload reports which ones those were.

def _timeline_events(day: str, lo: str, hi: str) -> list[dict]:
    """The narrative: what each seat says it did. The one hand-written source
    in the feed, and the only one with an editorial verdict attached.

    An entry may name the commits it shipped (`log_event --commit`), which is
    what lets a reader see one event instead of a narration and a commit row
    that never reference each other. The column is asked for only once it
    exists: a host whose timeline predates it would otherwise fail the whole
    SELECT and blank the narrative source — the reader would take "nothing
    happened today" from "this store is one migration behind"."""
    db = _ro(hostenv.timeline_db())
    if db is None:
        return []
    out = []
    try:
        has_commits = any(r["name"] == "commits"
                          for r in db.execute("PRAGMA table_info(entries)"))
        rows = db.execute(
            "SELECT id, time, agent, submode, headline, details, origin, "
            "       verdict, verdict_note, session_id, pipeline_task, context"
            + (", commits" if has_commits else ", '[]' AS commits") +
            " FROM entries WHERE date = ? ORDER BY time DESC", (day,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()
    for r in rows:
        try:
            details = json.loads(r["details"] or "[]")
        except (json.JSONDecodeError, TypeError):
            details = []
        out.append({
            "id": f"timeline:{r['id']}",
            # entries store date and time separately, to the minute.
            "ts": f"{day}T{(r['time'] or '00:00')}:00.000000",
            "source": "timeline",
            # The origin split is the whole reason a day-scale feed stays
            # readable: human-driven work and autonomous machinery are
            # different kinds of news, and mixing them reads as a flood.
            "kind": r["origin"] or "direct",
            "agent": r["agent"] or "",
            "seat": _seat(r["agent"] or "", r["submode"] or ""),
            "project": "",
            "title": r["headline"] or "",
            "body": "\n".join(f"• {d}" for d in details if d),
            "session_id": r["session_id"] or "",
            "meta": {k: v for k, v in (
                ("verdict", r["verdict"] or ""),
                ("verdict_note", r["verdict_note"] or ""),
                ("pipeline_task", r["pipeline_task"] or ""),
                ("has_context", bool(r["context"])),
                ("commits", _entry_commits(r["commits"])),
            ) if v},
        })
    return out


def _session_events(day: str, lo: str, hi: str) -> list[dict]:
    """Spawn history: every session that started today and what it was asked
    to do.

    The "what for" is the session's own first real message — which for a
    machine spawn is wrapped in a routing marker (`[cron:… label] …`). That
    marker is stripped through the one seam that owns the reading, so a cron
    wake reads as its task rather than as its plumbing."""
    from . import store as jstore
    from .messages import spawn_task
    db = _ro(Path(jstore.DB_PATH))
    if db is None:
        return []
    try:
        rows = db.execute(
            "SELECT session_id, agent_id, sub_mode, spawned, entrypoint, path, "
            "       first_real_user_msg, first_msg, ai_title, custom_title "
            "FROM sessions WHERE spawned >= ? AND spawned < ? AND deleted = 0 "
            "ORDER BY spawned DESC", (lo, hi)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()
    out = []
    for r in rows:
        raw = (r["first_real_user_msg"] or r["first_msg"] or "").strip()
        task = spawn_task(raw) or raw
        title = (r["custom_title"] or r["ai_title"] or "").strip()
        out.append({
            "id": f"session:{r['session_id']}",
            "ts": r["spawned"] or "",
            "source": "session",
            # Codex rollouts and Claude transcripts both land in this index;
            # the entrypoint is what actually distinguishes a typed session
            # from one the machine started, which is the split worth filtering.
            "kind": r["entrypoint"] or "cli",
            "agent": r["agent_id"] or "",
            "seat": _seat(r["agent_id"] or "", r["sub_mode"] or ""),
            "project": "",
            "title": title or _one_line(task) or "New session",
            "body": task if title else "",
            "session_id": r["session_id"] or "",
            "meta": {"sub_mode": r["sub_mode"] or ""},
        })
    return out


def _commit_events(day: str, lo: str, hi: str) -> list[dict]:
    """What shipped, across every checkout this host owns. Served from this
    module's own index — the git walk happens on the indexer thread, never on
    a request, so one slow repo can never hang a live view."""
    db = _ro(DB_PATH)
    if db is None:
        return []
    try:
        rows = db.execute(
            "SELECT * FROM commits WHERE day = ? ORDER BY ts DESC", (day,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()
    return [{
        "id": f"commit:{r['id']}",
        "ts": r["ts"],
        "source": "commit",
        "kind": r["repo"],
        # The agent the registry says owns the repo — so a commit filters
        # under the seat that shipped it, not as an orphan of the whole day.
        "agent": r["agent"] or "",
        "seat": "",
        "project": r["project"] or "",
        "title": r["subject"] or "",
        "body": r["body"] or "",
        "session_id": "",
        "meta": {"repo": r["repo"], "sha": r["sha"][:8], "author": r["author"],
                 "files": r["files"], "path": r["repo_path"]},
    } for r in rows]


def _run_events(day: str, lo: str, hi: str) -> list[dict]:
    """How the scheduler's wakes ended. Deliberately outcomes only — a run's
    *reason* comes from the session it spawned (see `_session_events`), which
    carries it inline and has no orphan problem when a job is later deleted."""
    db = _ro(DB_PATH)
    if db is None:
        return []
    try:
        rows = db.execute(
            "SELECT * FROM runs WHERE day = ? ORDER BY ts DESC", (day,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()
    out = []
    for r in rows:
        status = r["status"] or r["action"]
        label = r["label"] or "unknown job"
        out.append({
            "id": f"run:{r['id']}",
            "ts": r["ts"],
            "source": "run",
            "kind": status,
            "agent": r["agent"] or "",
            "seat": "",
            "project": "",
            "title": f"{label} — {status}",
            "body": r["summary"] or "",
            "session_id": r["session_id"] or "",
            "meta": {k: v for k, v in (
                ("duration_ms", r["duration_ms"]),
                ("exit_code", r["exit_code"]),
                ("kill_reason", r["kill_reason"] or ""),
                ("job_id", r["job_id"]),
            ) if v},
        })
    return out


def _message_events(day: str, lo: str, hi: str) -> list[dict]:
    """Agent-to-agent traffic — who asked whom for what, and whether it was a
    blocking task or an update that obliges nobody. jStack's inbox keeps the
    `messages` table beside the timeline's entries."""
    db = _ro(hostenv.timeline_db())
    if db is None:
        return []
    try:
        rows = db.execute(
            "SELECT id, created_at, from_seat, to_seat, subject, body, wake, "
            "       state, reply_to FROM messages "
            "WHERE created_at >= ? AND created_at < ? ORDER BY created_at DESC",
            (lo[:19], hi[:19])).fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()
    out = []
    for r in rows:
        sender = (r["from_seat"] or "")
        out.append({
            "id": f"message:{r['id']}",
            "ts": (r["created_at"] or "") + ".000000",
            "source": "message",
            "kind": "reply" if r["reply_to"] else ("task" if r["wake"] else "update"),
            "agent": sender.split("/")[0],
            "seat": sender,
            "project": "",
            "title": f"{sender} → {r['to_seat']}: {_one_line(r['subject'])}",
            "body": r["body"] or "",
            "session_id": "",
            "meta": {"to": r["to_seat"] or "", "state": r["state"] or ""},
        })
    return out


def _ping_events(day: str, lo: str, hi: str) -> list[dict]:
    """What reached the user on a chat lane, where the host has one. A
    standalone host answers `pings_db() is None` and this source stays quiet;
    the dashboard Mac's record is read the way it is written, one row per
    delivery, stamped UTC."""
    db = _ro(hostenv.pings_db())
    if db is None:
        return []
    try:
        rows = db.execute(
            "SELECT * FROM pings ORDER BY ts_utc DESC LIMIT 400").fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()
    out = []
    for row in rows:
        r = dict(row)
        ts = _utc_to_local_iso(str(r.get("ts_utc") or ""))
        if not (lo <= ts < hi):
            continue
        out.append({
            "id": f"ping:{r.get('id')}",
            "ts": ts,
            "source": "ping",
            "kind": r.get("type") or "info",
            "agent": r.get("agent") or "",
            "seat": "",
            "project": "",
            "title": _one_line(r.get("body") or ""),
            "body": r.get("body") or "",
            "session_id": r.get("session_id") or "",
            "meta": {"via": r.get("via") or "", "source": r.get("source") or ""},
        })
    return out


# The registry. Order here is the order the filter chips render in: the two
# narrative sources first, then the machine's own record of itself.
SOURCES: list[dict] = [
    {"id": "timeline", "label": "Timeline", "glyph": "text.alignleft", "fetch": _timeline_events},
    {"id": "session", "label": "Sessions", "glyph": "bubble.left.and.bubble.right", "fetch": _session_events},
    {"id": "commit", "label": "Commits", "glyph": "arrow.triangle.branch", "fetch": _commit_events},
    {"id": "run", "label": "Runs", "glyph": "clock.arrow.circlepath", "fetch": _run_events},
    {"id": "message", "label": "Messages", "glyph": "tray.full", "fetch": _message_events},
    {"id": "ping", "label": "Pings", "glyph": "bell", "fetch": _ping_events},
]


# ── the day ──

def day(date: str = "", limit: int = 1500) -> dict:
    """The whole day, merged and newest-first.

    The full day ships in one payload and the client filters in memory: a day
    is a few hundred events, and a round trip per filter toggle would make the
    cheapest interaction the slowest one. `sources`, `agents` and `projects`
    ride along as the facets actually present, so the client's filter UI is
    built from what the day contains rather than from a hardcoded list.
    """
    date = date or datetime.now().strftime("%Y-%m-%d")
    lo, hi = _day_bounds(date)
    events: list[dict] = []
    served: list[dict] = []
    for src in SOURCES:
        try:
            rows = src["fetch"](date, lo, hi)
            ok = True
        except Exception as e:                              # noqa: BLE001
            # A source that cannot read must say so, not contribute silence:
            # an empty feed and an unreadable store look identical otherwise,
            # and the reader would take "nothing happened" from "couldn't see".
            print(f"feed: source {src['id']} failed "
                  f"({type(e).__name__}: {e})", flush=True)
            rows, ok = [], False
        events.extend(rows)
        served.append({"id": src["id"], "label": src["label"],
                       "glyph": src["glyph"], "count": len(rows), "ok": ok})
    events.sort(key=lambda e: e.get("ts") or "", reverse=True)
    # Facets come off the WHOLE day, not the page. Computing them after the
    # cut would drop an agent from the filter menu precisely on the busy days
    # when filtering is the reason the menu exists.
    agents = sorted({e["agent"] for e in events if e.get("agent")})
    projects = sorted({e["project"] for e in events if e.get("project")})
    truncated = len(events) > limit
    if truncated:
        events = events[:limit]
    return {
        "date": date,
        "events": events,
        "sources": served,
        "agents": agents,
        "projects": projects,
        "truncated": truncated,
        "signature": signature(date),
    }


def signature(date: str = "") -> str:
    """A cheap fingerprint of the day, for the push stream to compare.

    Every term is an indexed count or max over a store that already has an
    index on the thing being counted — cheap enough to run on a tick, and it
    moves when anything the feed would show moves. Not memoized on held
    connections: a handle onto someone else's WAL is a lock this module has
    no business owning for the life of a stream.
    """
    date = date or datetime.now().strftime("%Y-%m-%d")
    lo, hi = _day_bounds(date)
    parts: list[str] = []

    def probe(path: Path | None, sql: str, args: tuple) -> None:
        db = _ro(path)
        if db is None:
            parts.append("-")
            return
        try:
            parts.append("/".join(str(v) for v in db.execute(sql, args).fetchone()))
        except sqlite3.Error:
            parts.append("?")
        finally:
            db.close()

    tl = hostenv.timeline_db()
    probe(tl, "SELECT COUNT(*), COALESCE(MAX(id),0), COALESCE(MAX(created_at),'') "
              "FROM entries WHERE date = ?", (date,))
    probe(tl, "SELECT COUNT(*), COALESCE(MAX(id),0) FROM messages "
              "WHERE created_at >= ? AND created_at < ?", (lo[:19], hi[:19]))
    probe(DB_PATH, "SELECT COUNT(*) FROM commits WHERE day = ?", (date,))
    probe(DB_PATH, "SELECT COUNT(*), COALESCE(MAX(ts),'') FROM runs WHERE day = ?", (date,))
    try:
        from . import store as jstore
        probe(Path(jstore.DB_PATH),
              "SELECT COUNT(*), COALESCE(MAX(last_activity),'') FROM sessions "
              "WHERE spawned >= ? AND spawned < ? AND deleted = 0", (lo, hi))
    except Exception:                                       # noqa: BLE001
        parts.append("-")
    return "|".join(parts)


# ── the indexer: the two expensive producers ──

def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    return db


def _ref_stamp(repo: Path) -> str:
    """The cheap change gate: a repo whose ref log hasn't moved has no new
    commits, so it costs two stats instead of a subprocess."""
    marks = []
    for rel in (".git/logs/HEAD", ".git/HEAD"):
        try:
            marks.append(str((repo / rel).stat().st_mtime_ns))
        except OSError:
            marks.append("-")
    return ":".join(marks)


def index_commits(db: sqlite3.Connection, since_days: int = BACKFILL_DAYS) -> int:
    """Fold new commits from every changed checkout into the index.

    Bounded by `since_days` rather than by "everything": the feed is a day
    lens, and a first run that walked every repo's full history would spend
    minutes to index years nobody will scroll to.
    """
    floor = (datetime.now() - timedelta(days=since_days)).strftime("%Y-%m-%d")
    written = 0
    for repo in hostenv.repos():
        stamp = _ref_stamp(repo)
        key = f"repo:{repo}"
        row = db.execute("SELECT stamp FROM watermarks WHERE key = ?", (key,)).fetchone()
        if row and row["stamp"] == stamp:
            continue
        agent = hostenv.repo_agent(repo)
        project = hostenv.repo_project(repo)
        try:
            proc = subprocess.run(
                [GIT, "-C", str(repo), "log", f"--since={floor} 00:00",
                 "--no-merges", "--pretty=format:%H%x1f%at%x1f%an%x1f%s%x1f%b%x1e"],
                capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        for chunk in proc.stdout.split("\x1e"):
            chunk = chunk.strip("\n")
            if not chunk:
                continue
            bits = chunk.split("\x1f")
            if len(bits) < 4:
                continue
            sha, at, author, subject = bits[0], bits[1], bits[2], bits[3]
            body = bits[4] if len(bits) > 4 else ""
            try:
                ts = _local_iso(float(at))
            except ValueError:
                continue
            cur = db.execute(
                "INSERT INTO commits(id, repo, repo_path, sha, ts, day, author, "
                " subject, body, project, agent, files) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,0) ON CONFLICT(id) DO NOTHING",
                (f"{repo.name}:{sha}", repo.name, str(repo), sha, ts, ts[:10],
                 author, subject.strip(), body.strip(), project, agent))
            written += cur.rowcount if cur.rowcount > 0 else 0
        db.execute("INSERT INTO watermarks(key, stamp) VALUES(?,?) "
                   "ON CONFLICT(key) DO UPDATE SET stamp = excluded.stamp",
                   (key, stamp))
    return written


def _job_labels() -> dict[str, dict]:
    """jobId → its registry entry. The registry holds only *current* jobs:
    a deleted job, and every `delete_after_run` one-shot by design, leaves run
    rows with nothing to join. Those render as unknown rather than blank."""
    path = hostenv.scheduler_config_dir() / "schedule.json"
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    jobs = raw.get("jobs") if isinstance(raw, dict) else raw
    out = {}
    for job in jobs or []:
        if isinstance(job, dict) and job.get("id"):
            out[job["id"]] = job
    return out


def index_runs(db: sqlite3.Connection, since_days: int = BACKFILL_DAYS) -> int:
    """Fold the scheduler's journal in — one append-only file per job, so the
    gate is the file's mtime and only recently-touched files are read."""
    root = hostenv.scheduler_state_dir() / "runs"
    if not root.is_dir():
        return 0
    floor_epoch = time.time() - since_days * 86400
    labels = _job_labels()
    written = 0
    for f in root.glob("*.jsonl"):
        try:
            st = f.stat()
        except OSError:
            continue
        if st.st_mtime < floor_epoch:
            continue
        key = f"run:{f.name}"
        stamp = f"{st.st_mtime_ns}:{st.st_size}"
        row = db.execute("SELECT stamp FROM watermarks WHERE key = ?", (key,)).fetchone()
        if row and row["stamp"] == stamp:
            continue
        try:
            lines = f.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            # A start is the session row's job; only the outcome is news here.
            if rec.get("action") not in ("finished", "skipped"):
                continue
            ms = rec.get("ts")
            if not isinstance(ms, (int, float)):
                continue
            ts = _local_iso(ms / 1000.0)
            if ts[:10] < datetime.fromtimestamp(floor_epoch).strftime("%Y-%m-%d"):
                continue
            job = labels.get(rec.get("jobId") or "", {})
            cur = db.execute(
                "INSERT INTO runs(id, job_id, run_id, ts, day, action, status, "
                " agent, label, session_id, summary, duration_ms, exit_code, kill_reason) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                (f"{rec.get('jobId','')}:{rec.get('runId','')}:{rec.get('action')}",
                 rec.get("jobId") or "", rec.get("runId") or "", ts, ts[:10],
                 rec.get("action") or "", rec.get("status") or "",
                 (job.get("agent_id") or "").split("-")[0], job.get("name") or "",
                 rec.get("sessionId") or "", _one_line(rec.get("summary") or "", 400),
                 int(rec.get("durationMs") or 0), int(rec.get("exitCode") or 0),
                 rec.get("killReason") or ""))
            written += cur.rowcount if cur.rowcount > 0 else 0
        db.execute("INSERT INTO watermarks(key, stamp) VALUES(?,?) "
                   "ON CONFLICT(key) DO UPDATE SET stamp = excluded.stamp",
                   (key, stamp))
    return written


def refresh() -> int:
    """One indexing pass — the number of rows that are new to the index.
    Safe to call directly (tests, a warm-up)."""
    db = _conn()
    try:
        db.executescript(_SCHEMA)
        n = index_commits(db) + index_runs(db)
        db.commit()
        return n
    finally:
        db.close()


_indexer: threading.Thread | None = None


def start_indexer() -> None:
    """Start the background pass. Called from the host's startup hook, never
    at import — an import (a test, a CLI, a script) must not kick off
    subprocess and filesystem work as a side effect."""
    global _indexer
    if _indexer is not None and _indexer.is_alive():
        return

    def _run() -> None:
        while True:
            try:
                refresh()
            except Exception as e:                          # noqa: BLE001
                # The thread outlives a bad pass: a feed that stops indexing
                # because one repo was mid-rebase is worse than a stale tick.
                print(f"feed: index pass failed "
                      f"({type(e).__name__}: {e})", flush=True)
            time.sleep(TICK)

    _indexer = threading.Thread(target=_run, name="jremote-feed-index", daemon=True)
    _indexer.start()
