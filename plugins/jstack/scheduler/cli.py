"""`python3 -m scheduler.cli` — all registry mutations.

Mutation verbs may be guarded externally (an install can gate them behind a
PreToolUse hook so agents go through its own tooling); the CLI just does the work.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import sys
import uuid
from datetime import datetime
from urllib import error as urlerror
from urllib import request as urlrequest
from zoneinfo import ZoneInfo

from . import config, journal, occurrences, registry, runner

# Wall-clock zone for job times — the install's, not a fixed region. The
# label beside a printed time is derived from it for the same reason: a
# hardcoded region abbreviation lies to every install outside that region.
LOCAL_TZ = ZoneInfo(config.DEFAULT_TZ)


def _tz_label(at: "datetime|None" = None) -> str:
    """The zone abbreviation for the instant being named — not for now.

    A label reads as a fact about the time beside it, so it has to be computed
    at that time. Booked from `now` instead, a wake for August written in
    January is named PST and fires in PDT: an hour of apparent drift that is
    really just the name being wrong, and the kind of thing a reader spends an
    afternoon on before noticing the job fired exactly when it said."""
    return (at or datetime.now(LOCAL_TZ)).replace(tzinfo=LOCAL_TZ).tzname() or config.DEFAULT_TZ


_MIN_PREFIX = 8


def _fail(msg: str) -> "None":
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _api(method: str, path: str, timeout: float = 10.0) -> dict:
    url = f"http://127.0.0.1:{config.API_PORT}{path}"
    req = urlrequest.Request(url, method=method, data=b"" if method == "POST" else None)
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _match_job(reg: dict, prefix: str) -> dict:
    if len(prefix) < _MIN_PREFIX:
        _fail(f"job id prefix must be at least {_MIN_PREFIX} chars")
    matches = [j for j in reg.get("jobs", []) if j.get("id", "").startswith(prefix)]
    if not matches:
        _fail(f"no job matches id prefix {prefix!r}")
    if len(matches) > 1:
        names = ", ".join(f"{j['id'][:12]}… ({j.get('name', '')})" for j in matches)
        _fail(f"ambiguous id prefix {prefix!r}: {names}")
    return matches[0]


def _emit(args, payload: dict, human: str) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(human)


# ------------------------------------------------------------------ add


def _base_job(agent: str, name: str, message: str, timeout_seconds: "int|None",
              workspace: "str|None") -> dict:
    job = {
        "id": str(uuid.uuid4()),
        "name": name,
        "agent_id": agent,
        "enabled": True,
        "payload": {"message": message},
        "timeout_seconds": timeout_seconds,
        "created_at": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
        "description": "",
    }
    if workspace:
        job["workspace"] = workspace
    return job


# --------------------------------------------------------------- subjects
#
# A job's `name` identifies the BOOKING; its `subject` identifies the WORK the
# booking covers. Two sessions that independently arrange coverage for one
# surface produce two bookings whose names cannot collide — a one-shot named
# for its fire time and a recurrence named for its purpose are different
# strings however identical the work — so a name-keyed dedupe catches a literal
# double-submit and nothing else. Keying on the subject is what makes a
# one-shot unable to shadow a recurrence already covering the same surface.

SUBJECT_COLLISION_EXIT = 3


def norm_subject(subject: "str|None") -> str:
    """Comparison form of a subject: whitespace-collapsed, case-folded.

    Near-miss spellings of one surface ('Meta token leak' / 'meta  token leak')
    are the same claim, and a dedupe they slip past is not a dedupe.
    """
    return " ".join((subject or "").split()).casefold()


class _SubjectClaimed(Exception):
    """(agent, subject) is already covered — raised inside the registry lock."""


def _add_job(job: dict) -> "dict|None":
    """Append `job` under the registry lock. Returns the colliding job, or None.

    The collision check runs INSIDE registry.mutate rather than before it. A
    subject is a claim on coverage, and check-then-append leaves open exactly
    the window this guard exists to close: two sessions both read a registry
    that does not yet cover the surface, and both write. Under the lock the
    later writer sees the earlier one.
    """
    subject = norm_subject(job.get("subject"))
    collision: dict = {}

    def _do(reg):
        if subject:
            for j in reg.get("jobs", []):
                if (j.get("agent_id") == job["agent_id"]
                        and j.get("enabled", True)
                        and norm_subject(j.get("subject")) == subject):
                    collision.update(j)
                    raise _SubjectClaimed()
        reg.setdefault("jobs", []).append(job)

    try:
        registry.mutate(_do)
    except _SubjectClaimed:
        return collision
    return None


def _refuse_collision(job: dict, existing: dict) -> "None":
    """Report a refused booking and exit non-zero. Nothing was written.

    Stderr only, in every output mode: the caller relays this text to whoever
    tried to book, and a JSON body beside it would bury the one line that
    matters — which job already owns the surface.
    """
    sched = existing.get("schedule") or {}
    how = sched.get("rrule") or f"once {sched.get('dtstart', '?')}"
    print(
        f"refused: {job['agent_id']} already covers subject "
        f"{job.get('subject')!r} — nothing booked.\n"
        f"  covered by {existing.get('id', '')[:8]} ({existing.get('name', '')}) "
        f"· {how} · next {_next_fire_str(existing)}\n"
        f"  correct that job instead, or book under a different --subject.",
        file=sys.stderr,
    )
    raise SystemExit(SUBJECT_COLLISION_EXIT)


def cmd_add_once(args) -> None:
    try:
        dt = datetime.strptime(args.at, "%Y-%m-%d %H:%M")
    except ValueError:
        _fail(f"--at must be 'YYYY-MM-DD HH:MM' ({config.DEFAULT_TZ} wall clock), "
              f"got {args.at!r}")
    name = args.name or f"{args.agent} wake {args.at} {_tz_label(dt)}"
    job = _base_job(args.agent, name, args.message, args.timeout_seconds, args.workspace)
    if getattr(args, "subject", None):
        job["subject"] = args.subject
    if args.category:
        job["category"] = args.category
    if getattr(args, "engine", None):
        job["engine"] = args.engine
    if getattr(args, "codex_model", None):
        job["codex_model"] = args.codex_model
    if args.resume_session:
        job["payload"]["resume_session_id"] = args.resume_session
    job["schedule"] = {
        "kind": "once",
        "dtstart": dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "tz": config.DEFAULT_TZ,
        "delete_after_run": bool(args.delete_after_run),
    }
    if getattr(args, "locked", False):
        job["locked"] = True
    existing = _add_job(job)
    if existing:
        _refuse_collision(job, existing)
    _emit(args, job, f"added once job {job['id']} — fires {args.at} {_tz_label(dt)}")


def cmd_add_recurring(args) -> None:
    if bool(args.cron) == bool(args.rrule):
        _fail("exactly one of --cron or --rrule is required")
    if args.cron:
        rrule = occurrences.cron_to_rrule(args.cron)
        dtstart = datetime.now(LOCAL_TZ).replace(second=0, microsecond=0)
    else:
        if not args.at_time:
            _fail("--rrule requires --at-time HH:MM")
        try:
            hh, mm = (int(x) for x in args.at_time.split(":"))
        except ValueError:
            _fail(f"--at-time must be HH:MM, got {args.at_time!r}")
        rrule = args.rrule
        dtstart = datetime.now(LOCAL_TZ).replace(hour=hh, minute=mm, second=0, microsecond=0)
    job = _base_job(args.agent, args.name, args.message, args.timeout_seconds, args.workspace)
    if getattr(args, "subject", None):
        job["subject"] = args.subject
    if args.category:
        job["category"] = args.category
    if getattr(args, "engine", None):
        job["engine"] = args.engine
    if getattr(args, "codex_model", None):
        job["codex_model"] = args.codex_model
    job["schedule"] = {
        "kind": "rrule",
        "dtstart": dtstart.strftime("%Y-%m-%dT%H:%M:%S"),
        "tz": config.DEFAULT_TZ,
        "rrule": rrule,
        "delete_after_run": False,
    }
    existing = _add_job(job)
    if existing:
        _refuse_collision(job, existing)
    nxt = occurrences.next_fires(job, 1)
    when = nxt[0].strftime("%Y-%m-%d %H:%M %Z") if nxt else "never"
    _emit(args, job, f"added recurring job {job['id']} — rrule {rrule}, next {when}")


# ------------------------------------------------------------------ list / next


def _next_fire_str(job: dict, n: int = 1) -> str:
    try:
        fires = occurrences.next_fires(job, n)
    except (KeyError, ValueError):
        return "invalid schedule"
    if not fires:
        return "—"
    return ", ".join(f.strftime("%Y-%m-%d %H:%M %Z") for f in fires)


def cmd_list(args) -> None:
    reg = registry.load_registry()
    jobs = reg.get("jobs", [])
    if args.agent:
        jobs = [j for j in jobs if j.get("agent_id") == args.agent]
    if not args.all:
        jobs = [j for j in jobs if j.get("enabled", True)]
    if args.json:
        out = []
        for job in jobs:
            fires = []
            try:
                fires = [f.isoformat() for f in occurrences.next_fires(job, 1)]
            except (KeyError, ValueError):
                pass
            out.append({**job, "next_fire_at": fires[0] if fires else None})
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return
    if not jobs:
        print("no jobs")
        return
    for job in jobs:
        flag = " " if job.get("enabled", True) else "✗"
        kind = (job.get("schedule") or {}).get("kind", "?")
        print(f"{flag} {job['id'][:8]}  {job.get('agent_id', ''):<20} {kind:<5} "
              f"next {_next_fire_str(job):<24} {job.get('name', '')}")


def cmd_next(args) -> None:
    reg = registry.load_registry()
    now = datetime.now(LOCAL_TZ)
    horizon = now.timestamp() + args.days * 86400
    if args.job:
        jobs = [_match_job(reg, args.job)]
        per_job = args.count
    else:
        jobs = [j for j in reg.get("jobs", []) if j.get("enabled", True)]
        per_job = args.count
    rows = []
    for job in jobs:
        try:
            fires = occurrences.next_fires(job, per_job, after=now)
        except (KeyError, ValueError):
            continue
        for f in fires:
            if f.timestamp() <= horizon:
                rows.append((f, job))
    rows.sort(key=lambda r: r[0])
    if args.json:
        print(json.dumps([
            {
                "job_id": job["id"],
                "name": job.get("name", ""),
                "agent_id": job.get("agent_id", ""),
                "fire_at": f.isoformat(),
                "fire_at_ms": int(f.timestamp() * 1000),
            }
            for f, job in rows
        ], indent=2, ensure_ascii=False))
        return
    if not rows:
        print(f"no fires within {args.days} day(s)")
        return
    for f, job in rows:
        print(f"{f.strftime('%Y-%m-%d %H:%M %Z')}  {job.get('agent_id', ''):<20} "
              f"{job.get('name', '')} ({job['id'][:8]})")


# ------------------------------------------------------------------ mutate


def cmd_rm(args) -> None:
    removed = {}

    def _do(reg):
        job = _match_job(reg, args.id_prefix)
        if job.get("locked") and not getattr(args, "force", False):
            _fail(
                f"job {job['id'][:8]} ({job.get('name', '')}) is LOCKED — a "
                f"human-set time that automated sessions must not move or delete. "
                f"Override with --force."
            )
        removed.update(job)
        reg["jobs"] = [j for j in reg["jobs"] if j["id"] != job["id"]]

    registry.mutate(_do)
    print(f"removed {removed['id']} ({removed.get('name', '')})")


def _set_locked(args, value: bool) -> None:
    target = {}

    def _do(reg):
        job = _match_job(reg, args.id_prefix)
        if value:
            job["locked"] = True
        else:
            job.pop("locked", None)
        target.update(job)

    registry.mutate(_do)
    print(f"{'locked' if value else 'unlocked'} {target['id']} ({target.get('name', '')})")


def cmd_lock(args) -> None:
    _set_locked(args, True)


def cmd_unlock(args) -> None:
    _set_locked(args, False)


def _set_enabled(args, enabled: bool) -> None:
    target = {}

    def _do(reg):
        job = _match_job(reg, args.id_prefix)
        job["enabled"] = enabled
        target.update(job)

    registry.mutate(_do)
    print(f"{'enabled' if enabled else 'disabled'} {target['id']} ({target.get('name', '')})")


def cmd_enable(args) -> None:
    _set_enabled(args, True)


def cmd_disable(args) -> None:
    _set_enabled(args, False)


# ------------------------------------------------------------------ daemon ops


def cmd_run_now(args) -> None:
    job = _match_job(registry.load_registry(), args.id_prefix)
    try:
        resp = _api("POST", f"/jobs/{job['id']}/run-now")
    except (urlerror.URLError, OSError) as e:
        _fail(f"scheduler daemon unreachable on :{config.API_PORT} ({e})")
    if not resp.get("ok"):
        _fail(f"daemon refused run-now for {job['id']}")
    print(f"fired {job['id']} ({job.get('name', '')})")


def cmd_kill(args) -> None:
    try:
        resp = _api("POST", f"/runs/{args.run_id}/kill")
        if resp.get("killed"):
            print(f"killed run {args.run_id}")
            return
        _fail(f"daemon does not know run {args.run_id}")
    except (urlerror.URLError, OSError):
        pass  # daemon down — fall back to recorded pgid
    for st in journal.load_state().values():
        if not isinstance(st, dict):
            continue
        for entry in st.get("active_runs", []):
            if entry.get("run_id") == args.run_id and entry.get("pgid"):
                try:
                    os.killpg(entry["pgid"], signal.SIGTERM)
                except (ProcessLookupError, OSError) as e:
                    _fail(f"killpg {entry['pgid']} failed: {e}")
                print(f"daemon down — sent SIGTERM to recorded pgid {entry['pgid']}")
                return
    _fail(f"no active run {args.run_id!r} in daemon or state.json")


# ------------------------------------------------------------------ feed token


def cmd_feed_token(args) -> None:
    path = config.FEED_TOKEN_FILE
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
    os.chmod(path, 0o600)
    print(path.read_text().strip())


# ------------------------------------------------------------------ parser


_SUBJECT_HELP = (
    "the surface this job covers. A second enabled job with the same "
    f"(agent, subject) is refused (exit {SUBJECT_COLLISION_EXIT}) whatever its "
    "fire time, so a one-shot cannot shadow a recurrence already covering it"
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scheduler.cli", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    ao = sub.add_parser("add-once", help="one-shot job at a local wall-clock time")
    ao.add_argument("--agent", required=True)
    ao.add_argument("--at", required=True, metavar='"YYYY-MM-DD HH:MM"')
    ao.add_argument("--message", required=True)
    ao.add_argument("--name")
    ao.add_argument("--timeout-seconds", type=int)
    ao.add_argument("--subject", help=_SUBJECT_HELP)
    ao.add_argument("--delete-after-run", action="store_true")
    ao.add_argument("--category", help="run-category (schedule.json categories map) for model/timeout resolution")
    ao.add_argument("--engine", choices=("claude", "codex"))
    ao.add_argument("--codex-model", help="native model override; otherwise use the Codex configuration")
    ao.add_argument("--workspace", help="absolute workspace override (testing)")
    ao.add_argument("--resume-session", metavar="SESSION_ID",
                    help="fork-resume this session's conversation instead of starting fresh "
                         "(pair with --workspace pinned to the session's cwd)")
    ao.add_argument("--locked", action="store_true",
                    help="mark as a human-set time: automated sessions cannot rm/reschedule it (rm needs --force)")
    ao.add_argument("--json", action="store_true")
    ao.set_defaults(func=cmd_add_once)

    ar = sub.add_parser("add-recurring", help="recurring job (cron expr or RRULE)")
    ar.add_argument("--agent", required=True)
    ar.add_argument("--cron", metavar='"EXPR"')
    ar.add_argument("--rrule", metavar='"RRULE"')
    ar.add_argument("--at-time", metavar="HH:MM")
    ar.add_argument("--message", required=True)
    ar.add_argument("--name", required=True)
    ar.add_argument("--timeout-seconds", type=int)
    ar.add_argument("--subject", help=_SUBJECT_HELP)
    ar.add_argument("--category", help="run-category (schedule.json categories map) for model/timeout resolution")
    ar.add_argument("--engine", choices=("claude", "codex"))
    ar.add_argument("--codex-model", help="native model override; otherwise use the Codex configuration")
    ar.add_argument("--workspace", help="absolute workspace override (testing)")
    ar.add_argument("--json", action="store_true")
    ar.set_defaults(func=cmd_add_recurring)

    ls = sub.add_parser("list", help="list jobs")
    ls.add_argument("--agent")
    ls.add_argument("--all", action="store_true", help="include disabled")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_list)

    nx = sub.add_parser("next", help="next-fire table")
    nx.add_argument("job", nargs="?", help="job id prefix (≥8 chars)")
    nx.add_argument("--days", type=int, default=7)
    nx.add_argument("-n", "--count", type=int, default=3)
    nx.add_argument("--json", action="store_true")
    nx.set_defaults(func=cmd_next)

    rm = sub.add_parser("rm", help="remove a job")
    rm.add_argument("id_prefix")
    rm.add_argument("--force", action="store_true",
                    help="remove even if the job is locked (human override)")
    rm.set_defaults(func=cmd_rm)

    lk = sub.add_parser("lock", help="lock a job (block automated rm/reschedule)")
    lk.add_argument("id_prefix")
    lk.set_defaults(func=cmd_lock)

    ul = sub.add_parser("unlock", help="unlock a job")
    ul.add_argument("id_prefix")
    ul.set_defaults(func=cmd_unlock)

    en = sub.add_parser("enable", help="enable a job")
    en.add_argument("id_prefix")
    en.set_defaults(func=cmd_enable)

    di = sub.add_parser("disable", help="disable a job")
    di.add_argument("id_prefix")
    di.set_defaults(func=cmd_disable)

    rn = sub.add_parser("run-now", help="fire a job immediately (via daemon)")
    rn.add_argument("id_prefix")
    rn.set_defaults(func=cmd_run_now)

    kl = sub.add_parser("kill", help="kill an active run")
    kl.add_argument("run_id")
    kl.set_defaults(func=cmd_kill)

    ft = sub.add_parser("feed-token", help="print (create-if-missing) the ics feed token")
    ft.set_defaults(func=cmd_feed_token)

    return p


def main(argv: "list[str]|None" = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
