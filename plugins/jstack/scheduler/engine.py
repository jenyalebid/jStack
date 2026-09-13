"""Tick loop: due jobs, misfire/catch-up, concurrency, schedule advance.

next_run_at is persisted (atomic state.json write) BEFORE spawning, so a
daemon restart immediately after a fire cannot double-fire. The next
occurrence always advances from the schedule (RRULE after the fired
occurrence), never from now.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, journal, occurrences, registry, runner, spawn


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


# occurrences enumerated per misfire resolution — bounds a pathological
# backlog (e.g. minutely job after a month-long outage)
_MAX_MISSED = 1000

# Rate-limit defer: when a run only hit the Claude session limit, the engine
# reschedules it to shortly after the stated reset instead of hard-failing.
# Buffer so the reset has definitely landed before we re-spawn.
_RATE_LIMIT_BUFFER_SECONDS = 300
# Backstop against all-day hammering if resets keep arriving already-exhausted:
# after this many consecutive rate-limit defers, stop pinning to reset and let
# the normal schedule take over (the next validator will surface a real gap).
_MAX_RATE_LIMIT_DEFERS = 6
# When the run WAS rate-limited but no reset time could be read out of its
# output, retry after this instead of surrendering the slot. An unparsed reset
# used to mean "the normal schedule stands" — on a daily job that is not a
# retry, it is skipping the entire day, which is how five accounts' control
# wakes died at 08:35 on 2026-07-28 and nothing published until someone noticed.
# A blind retry can be early (the limit is still up, we get rate-limited again
# and defer once more, capped); surrendering the day cannot be recovered at all.
_RATE_LIMIT_BLIND_RETRY_SECONDS = 2700
# Hard wall on how far a rate-limited job may chase its slot, measured from the
# time it was ORIGINALLY scheduled for. Past this the work is stale enough that
# running it is its own hazard — a 07:00 morning brief landing at 21:00 is not
# the brief, and a publish wake that drifts most of a day publishes into the
# wrong audience. The defer counter alone does not bound this: a quoted reset
# can sit five hours out, so six of them chain to over a day. `_MAX_RATE_LIMIT
# _DEFERS` bounds how many times we try; this bounds how long we keep trying.
_MAX_RATE_LIMIT_RECOVERY_SECONDS = 6 * 3600

# Auth-expiry defer: a run whose claude child died because its OAuth session
# could not be refreshed (status `auth_expired`) is transient the same way a
# rate limit is, but it must NOT take the immediate-retry arm below. The
# credential has not healed seconds later — a re-spawn meets the same one and
# dies the same way — so pin next_run a few minutes out instead, exactly the
# shape `_defer_for_rate_limit` uses.
_AUTH_RETRY_SECONDS = 300
# And stop after a handful. The observed fault self-heals, but the same output
# is also what a genuinely dead login prints, and no amount of retrying fixes
# that one. Past the cap the job takes the error streak and stops chasing, so
# the health surface can tell a human a re-login is owed.
_MAX_AUTH_RETRIES = 3
# The cap counts one EPISODE, not a job's lifetime. An episode is a chain of
# retries minutes apart; a failure arriving long after the previous one is a
# fresh fault, not a continuation of that chain, and gets its own retries.
# Without this the counter would be spent for good on the first bad morning and
# the SECOND morning would drop its work with no retry at all — the exact
# outage this change exists to close, just one day later. Any window past the
# chain's own span works; this is that span plus one retry.
_AUTH_EPISODE_SECONDS = _AUTH_RETRY_SECONDS * (_MAX_AUTH_RETRIES + 1)


def _schedule_fp(job: dict) -> str:
    canonical = json.dumps(job.get("schedule") or {}, sort_keys=True)
    return hashlib.md5(canonical.encode()).hexdigest()


class Engine:
    def __init__(self, now_fn=None, spawn_fn=None):
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._spawn_fn = spawn_fn or self._spawn_run
        self.lock = threading.RLock()
        self.runs: dict[str, object] = {}
        self.state: dict = journal.load_state()
        self.registry: "dict|None" = None
        self._registry_mtime: "float|None" = None
        self._queued: dict[str, datetime] = {}  # job_id -> queued occurrence (depth 1)
        self.started_at = time.time()
        self._stop = threading.Event()
        self._last_sweep_day: "str|None" = None

    # ---------------------------------------------------------------- registry

    def _reload_registry_if_changed(self) -> None:
        try:
            mtime = config.SCHEDULE_FILE.stat().st_mtime
        except OSError:
            mtime = None
        if self.registry is None or mtime != self._registry_mtime:
            self.registry = registry.load_registry()
            self._registry_mtime = mtime

    def reload(self) -> None:
        with self.lock:
            self._registry_mtime = None
            self._reload_registry_if_changed()

    def jobs(self) -> list[dict]:
        return (self.registry or {}).get("jobs", [])

    def defaults(self) -> dict:
        merged = dict(config.BUILTIN_DEFAULTS)
        for k, v in ((self.registry or {}).get("defaults") or {}).items():
            if v is not None:
                merged[k] = v
        return merged

    def categories(self) -> dict:
        """Run-category settings map (the job → category → default middle layer)."""
        return (self.registry or {}).get("categories") or {}

    def _job_by_id(self, job_id: str) -> "dict|None":
        for job in self.jobs():
            if job.get("id") == job_id:
                return job
        return None

    # ------------------------------------------------------------------- tick

    def tick(self) -> None:
        with self.lock:
            self._reload_registry_if_changed()
            defaults = self.defaults()
            now = self._now()
            now_ms = _ms(now)
            dirty = False
            fire_list: list[tuple[datetime, dict, dict]] = []

            for job in self.jobs():
                jid = job["id"]
                st = self.state.setdefault(jid, {})
                # Enabled-state transitions, observed HERE rather than stamped at
                # the mutation sites: the tick sees the registry however it was
                # written — CLI, dashboard, a hand edit, a git checkout — so no
                # path can flip a job silently. That record is what lets the cron
                # outcome validator tell "the cron stopped firing" (a real miss)
                # from "the job was switched off for that occurrence" (nothing to
                # judge); without it, the first morning after a pause false-fails
                # fired_within_window against an occurrence the job was off for
                # (a job renamed while its old spelling was still booked). First sight of a job
                # SEEDS the flag and claims no transition — a stamp means an
                # observed flip, never merely "the engine restarted".
                is_enabled = bool(job.get("enabled", True))
                if "enabled" not in st:
                    st["enabled"] = is_enabled
                    dirty = True
                elif st["enabled"] != is_enabled:
                    st["enabled"] = is_enabled
                    st["enabled_changed_at_ms"] = now_ms
                    dirty = True
                if not is_enabled:
                    # drop (not None-out) so a later re-enable re-anchors from
                    # then-now instead of triggering catch-up over the gap
                    if "next_run_at_ms" in st:
                        del st["next_run_at_ms"]
                        dirty = True
                    continue
                eff = registry.effective(job, defaults, self.categories())
                fp = _schedule_fp(job)
                if st.get("schedule_fp") != fp:
                    if "schedule_fp" in st and "next_run_at_ms" in st:
                        # schedule definition changed — the persisted anchor
                        # belongs to the old schedule; re-anchor from now
                        # (no fire at the old time, no catch-up over the edit)
                        del st["next_run_at_ms"]
                        self._queued.pop(jid, None)
                    st["schedule_fp"] = fp
                    dirty = True
                if "next_run_at_ms" not in st:
                    st["next_run_at_ms"] = self._initial_next_run(job, now)
                    dirty = True
                nra = st.get("next_run_at_ms")
                if nra is None:
                    continue
                if nra <= now_ms:
                    target, skipped = self._resolve_due(job, eff, nra, now)
                    for occ in skipped:
                        journal.append(jid, journal.skipped_record(
                            job_id=jid, run_at_ms=_ms(occ), reason="misfire"))
                    if skipped:
                        # A dropped occurrence is a MISSED RUN, and the health
                        # surface reads job state, not the journal — so a
                        # journal-only skip is invisible. An occurrence that
                        # dies against the concurrency cap minutes past its
                        # grace means the job's work never happened, while every
                        # status view still reads `ok` from the previous day.
                        # State carries it now; the next successful run clears
                        # it like any other error.
                        st["last_status"] = "misfire"
                        st["last_error"] = (
                            f"missed occurrence {_ms(skipped[-1])} "
                            f"(past catch-up grace)")
                        st["consecutive_errors"] = int(
                            st.get("consecutive_errors") or 0) + len(skipped)
                        dirty = True
                    if target is None:
                        self._advance(job, st, after=now)
                        dirty = True
                    else:
                        fire_list.append((target, job, eff))

            # queue-policy releases (depth 1) — fire once the active run ends;
            # schedule was already advanced when the occurrence was queued
            released: list[tuple[datetime, dict, dict]] = []
            for jid, when in list(self._queued.items()):
                if self._active_run_count(jid) == 0:
                    job = self._job_by_id(jid)
                    del self._queued[jid]
                    if job and job.get("enabled", True):
                        released.append((when, job, registry.effective(job, defaults, self.categories())))

            if dirty:
                journal.save_state(self.state)

            fire_list.sort(key=lambda t: t[0])  # FIFO by scheduled time
            for scheduled_for, job, eff in fire_list:
                self._try_fire(job, eff, scheduled_for, defaults)
            for scheduled_for, job, eff in released:
                self._try_fire(job, eff, scheduled_for, defaults, advance=False)

            self._sweep_logs(now)

    def _initial_next_run(self, job: dict, now: datetime) -> "int|None":
        sched = job.get("schedule") or {}
        if sched.get("kind") == "once":
            # a just-past one-shot must still be visible to misfire logic
            return _ms(occurrences.parse_dtstart(sched))
        fires = occurrences.next_fires(job, 1, after=now)
        return _ms(fires[0]) if fires else None

    def _resolve_due(self, job: dict, eff: dict, nra_ms: int, now: datetime):
        """Return (occurrence_to_fire_or_None, occurrences_to_journal_skipped)."""
        tz = occurrences.job_tz(job)
        occs = [datetime.fromtimestamp(nra_ms / 1000, tz=tz)]
        for _ in range(_MAX_MISSED):
            nxt = occurrences.next_fires(job, 1, after=occs[-1])
            if not nxt or nxt[0] > now:
                break
            occs.append(nxt[0])
        target, older = occs[-1], occs[:-1]
        late_s = (now - target).total_seconds()
        if late_s <= config.MISFIRE_THRESHOLD_SECONDS:
            return target, older  # on-time fire; anything older was a miss
        grace = eff.get("catch_up_grace_seconds") or self.defaults().get("catch_up_grace_seconds")
        if eff.get("catch_up", True) and late_s <= grace:
            return target, older  # catch up: fire the most recent miss ONCE
        return None, occs  # skip all

    def _advance(self, job: dict, st: dict, after: datetime) -> None:
        fires = occurrences.next_fires(job, 1, after=after)
        new_ms = _ms(fires[0]) if fires else None
        cur = st.get("next_run_at_ms")
        if new_ms is not None and cur is not None and new_ms < cur:
            return  # never regress the schedule
        st["next_run_at_ms"] = new_ms

    # ------------------------------------------------------------------- fire

    def _active_run_count(self, job_id: str) -> int:
        return sum(1 for r in self.runs.values() if r.job_id == job_id)

    def _try_fire(self, job: dict, eff: dict, scheduled_for: datetime,
                  defaults: dict, retry_of: "str|None" = None,
                  advance: bool = True, retry_reason: str = "") -> None:
        jid = job["id"]
        st = self.state.setdefault(jid, {})
        policy = eff.get("concurrency", "skip")
        if retry_of is None and self._active_run_count(jid) > 0 and policy != "parallel":
            if policy == "queue" and jid not in self._queued:
                self._queued[jid] = scheduled_for
            else:  # skip, or queue already holding one (depth 1)
                journal.append(jid, journal.skipped_record(
                    job_id=jid, run_at_ms=_ms(scheduled_for), reason="concurrency-skip"))
            if advance:
                self._advance(job, st, after=scheduled_for)
                journal.save_state(self.state)
            return
        if len(self.runs) >= int(defaults.get("max_concurrent_runs", 4)):
            # global cap: schedule NOT advanced — stays due, retried FIFO next tick
            return
        if advance:
            self._advance(job, st, after=scheduled_for)
        journal.save_state(self.state)  # persist BEFORE spawn — no double-fire
        if retry_reason:
            # Carried on `eff`, not on the spawn_fn signature. `eff` is the
            # per-fire view of the job and is already where Run reads every
            # other per-fire setting (model, claude_bin, permission_mode); the
            # spawn_fn arity, by contrast, is a seam an install may have
            # supplied its own callable for, and widening it would break that
            # install at runtime in the retry path — the least-exercised path
            # there is. Underscored so nobody reads it as a job setting.
            eff = dict(eff)
            eff["_retry_reason"] = retry_reason
        try:
            run = self._spawn_fn(job, eff, scheduled_for, retry_of)
        except Exception as e:
            _log(f"spawn failed for {jid}: {e!r}")
            self._spawn_failure(job, eff, scheduled_for, str(e), retry_of, defaults)
            return
        self.runs[run.run_id] = run
        st.setdefault("active_runs", []).append({
            "run_id": run.run_id,
            "session_id": run.session_id,
            "pid": run.pid,
            "pgid": run.pgid,
            "spawned_at_ms": run.spawned_at_ms,
            "scheduled_for_ms": run.scheduled_for_ms,
            "workspace": str(run.workspace) if getattr(run, "workspace", None) else None,
        })
        journal.save_state(self.state)
        _log(f"fired {jid} ({job.get('name', '')}) run={run.run_id} pid={run.pid}")

    def _spawn_run(self, job: dict, eff: dict, scheduled_for: datetime,
                   retry_of: "str|None"):
        run = runner.Run(job=eff, defaults=self.defaults(), scheduled_for=scheduled_for,
                         on_finish=self._on_run_finish, retry_of=retry_of,
                         retry_reason=eff.get("_retry_reason", ""))
        return run.spawn()

    def _spawn_failure(self, job: dict, eff: dict, scheduled_for: datetime,
                       error: str, retry_of: "str|None", defaults: dict) -> None:
        jid = job["id"]
        st = self.state.setdefault(jid, {})
        failed_run_id = f"spawnfail-{int(time.time() * 1000)}"
        now_ms = _ms(self._now())
        st["last_status"] = "error"
        st["last_error"] = f"spawn failure: {error}"
        st["consecutive_errors"] = int(st.get("consecutive_errors") or 0) + 1
        # A spawn failure that will be retried below is not terminal; one that
        # will not (a retry that itself failed, or retry disabled) is the run
        # dying for good, so it delivers on the same terms as a finished run.
        will_retry = retry_of is None and eff.get("retry_on_stall", True)
        delivery = ("not-requested" if will_retry
                    else self._deliver_failure(job, eff, st, None))
        journal.append(jid, journal.finished_record(
            job_id=jid, agent_id=job.get("agent_id", ""), status="error",
            summary=f"spawn failure: {error}", session_id="", run_at_ms=_ms(scheduled_for),
            duration_ms=0, next_run_at_ms=st.get("next_run_at_ms"),
            model=eff.get("model") or "", run_id=failed_run_id, spawned_at_ms=now_ms,
            exit_code=None, kill_reason=None, retry_of=retry_of, delivery=delivery))
        journal.save_state(self.state)
        if retry_of is None and eff.get("retry_on_stall", True):
            _log(f"retrying {jid} after spawn failure")
            # No gate here, and none is needed: the spawn never happened, so
            # there is no session and nothing it could have done. The banner
            # still rides — it names the cause, and a run told a sibling failed
            # to start loses nothing by checking.
            self._try_fire(job, eff, scheduled_for, defaults,
                           retry_of=failed_run_id, advance=False,
                           retry_reason="a spawn failure")
        elif (job.get("schedule") or {}).get("kind") == "once":
            self._finalize_once(job, "error")

    # ----------------------------------------------------------------- finish

    def _on_run_finish(self, run, status: str, exit_code: "int|None",
                       kill_reason: "str|None", summary: str) -> None:
        with self.lock:
            self.runs.pop(run.run_id, None)
            jid = run.job_id
            st = self.state.setdefault(jid, {})
            st["active_runs"] = [
                r for r in st.get("active_runs", []) if r.get("run_id") != run.run_id
            ]
            now_ms = _ms(self._now())
            duration_ms = max(0, now_ms - run.spawned_at_ms)
            job = self._job_by_id(jid) or run.job
            eff = registry.effective(job, self.defaults(), self.categories())
            st["last_run_at_ms"] = run.scheduled_for_ms
            st["last_status"] = status
            st["last_duration_ms"] = duration_ms
            st["last_session_id"] = run.session_id
            # An auth expiry is transient only while retries remain. Roll the
            # episode FIRST so a failure long after the last one starts its own
            # chain, then read the counter — the answer decides both whether the
            # error streak moves and whether a defer is booked below.
            if status == "auth_expired":
                self._roll_auth_episode(st, now_ms)
            auth_retry_left = (status == "auth_expired"
                               and int(st.get("auth_retries") or 0) < _MAX_AUTH_RETRIES)
            if status == "ok":
                st["consecutive_errors"] = 0
                st["last_error"] = None
                st["rate_limit_defers"] = 0
                st["auth_retries"] = 0
                # The wall's anchor goes with the counter — a job that finally
                # got through has no outage left to be measured from, and a
                # stale anchor would expire its NEXT rate limit instantly.
                st.pop("rate_limit_anchor_ms", None)
                st.pop("auth_last_fail_ms", None)
            elif status == "rate_limited" or auth_retry_left:
                # transient usage-window exhaustion, or an auth expiry with a
                # deferred retry still coming — not a job fault, so it must not
                # increment the error streak that drives alerting.
                st["last_error"] = status
            else:
                st["consecutive_errors"] = int(st.get("consecutive_errors") or 0) + 1
                st["last_error"] = status + (f" ({kill_reason})" if kill_reason else "")

            # A finish that will not recover in-band is a TERMINAL failure —
            # deliver it if the job asked. rate_limited and an auth expiry with
            # retries left are transient (a defer is booked below); a stall /
            # ttft / api_error that WILL be retried this cycle is not terminal
            # either. Everything else that is not ok has nothing coming, and is
            # exactly the silent failure #17 exists to end. Computed here so the
            # record written below carries the delivery outcome, not a guess.
            transient = kill_reason in ("stall", "ttft") or status == "api_error"
            retry_wanted = (status != "ok" and transient
                            and eff.get("retry_on_stall", True)
                            and run.retry_of is None and job.get("enabled", True))
            # ...unless the run it would replace already acted. Read here rather
            # than at the arm below because `will_retry` decides `terminal_failure`
            # decides `delivery`: a gate consulted later would stamp every blocked
            # retry `not-requested` and the human it hands the decision to would
            # never be called.
            blocked_by = self._retry_blocker(run) if retry_wanted else None
            will_retry = retry_wanted and blocked_by is None
            if blocked_by:
                st["last_error"] = (f"{status} after acting ({blocked_by}) — "
                                    f"not retried, a re-run would repeat it")
            terminal_failure = (status not in ("ok", "rate_limited")
                                and not auth_retry_left and not will_retry)
            delivery = (self._deliver_failure(job, eff, st, run.session_id)
                        if terminal_failure else "not-requested")

            journal.append(jid, journal.finished_record(
                job_id=jid,
                agent_id=run.job.get("agent_id", ""),
                status=status,
                summary=summary,
                session_id=run.session_id or "",
                run_at_ms=run.scheduled_for_ms,
                duration_ms=duration_ms,
                next_run_at_ms=st.get("next_run_at_ms"),
                model=run.model,
                run_id=run.run_id,
                spawned_at_ms=run.spawned_at_ms,
                exit_code=exit_code,
                kill_reason=kill_reason,
                retry_of=run.retry_of,
                delivery=delivery,
            ))
            journal.save_state(self.state)
            _log(f"finished {jid} run={run.run_id} status={status}")

            # Rate-limited: reschedule to just after the reset (once/recurring
            # alike — a rate-limited once job must be retried, never parked).
            if status == "rate_limited":
                deferred = self._defer_for_rate_limit(job, st, run)
                # A once job that couldn't be deferred (no reset time parsed, or
                # defer cap reached) has no recurring schedule to fall back on —
                # left as-is it dangles enabled with next_run=null, a zombie that
                # reads red forever and never fires again. Finalize → park.
                if not deferred and (job.get("schedule") or {}).get("kind") == "once":
                    self._finalize_once(job, status)
                return

            # Auth expiry: deferred retry while the episode has retries left,
            # then a hard park. Placed above the immediate-retry arm because the
            # whole point is that this fault must NOT be re-spawned seconds
            # later into the same unrefreshable credential.
            if status == "auth_expired":
                if auth_retry_left:
                    self._defer_for_auth_expiry(job, st, run)
                    return
                # Cap spent. The streak was already bumped above; name the cause
                # so the surface that reads it says what a human has to do.
                st["last_error"] = (f"auth_expired (retry cap {_MAX_AUTH_RETRIES} "
                                    f"reached — re-login may be needed)")
                journal.save_state(self.state)
                _log(f"auth-expired {jid} run={run.run_id} — retry cap "
                     f"({_MAX_AUTH_RETRIES}) reached; parking as a hard failure")
                if (job.get("schedule") or {}).get("kind") == "once":
                    self._finalize_once(job, status)
                return

            # Retry transient faults once (same slot, tagged retry_of): a hung
            # session (stall/ttft) or a dropped/failed API connection mid-run
            # (api_error). Spawn failures retry above; rate limits defer above. A
            # genuine job error (real exception, bad content) is NOT retried — it
            # parks. Without the api_error arm a one-shot publish wake killed by a
            # transient "Connection closed mid-response" was lost silently.
            #
            # `will_retry` above already withheld the retry from a run whose
            # transcript shows it acted — `api_error` fires on the last turn of a
            # finished run as readily as on the first turn of an untouched one,
            # and a blind re-send repeats every action the dead run took. That
            # case gets a terminal failure and a human instead, which the
            # delivery above has already sent.
            retried = False
            if will_retry:
                _log(f"retrying {jid} after {kill_reason or status}")
                self._try_fire(job, eff, run.scheduled_for, self.defaults(),
                               retry_of=run.run_id, advance=False,
                               retry_reason=(kill_reason or status))
                retried = True
            elif blocked_by:
                _log(f"NOT retrying {jid} after {kill_reason or status} — run "
                     f"{run.run_id} already acted ({blocked_by}); a re-run would "
                     f"repeat it. Terminal failure delivered instead.")
            if (job.get("schedule") or {}).get("kind") == "once" and not retried:
                self._finalize_once(job, status)

    def _retry_blocker(self, run) -> "str|None":
        """What the failed run already did that a blind re-send would repeat, or
        None when nothing in its transcript says it acted.

        A run with no session never reached the model, so there is nothing to
        repeat — that is the `ttft` kill and the fast spawn death, the cases the
        retry arm exists for and must keep serving. A run that HAD a session but
        no recorded workspace is the opposite reading: it ran, and we cannot find
        what it wrote. `Run.spawn` sets the workspace before anything else, so in
        practice this is an adopted run from a state entry written before the
        field existed — it gets refused, not waved through.

        Any fault reading the transcript blocks the retry for the same reason: we
        went looking precisely because we could not afford to guess.
        """
        session_id = getattr(run, "session_id", None)
        workspace = getattr(run, "workspace", None)
        if not session_id:
            return None
        if not workspace:
            return "transcript location unknown"
        try:
            return runner.first_action(
                runner.session_jsonl_path(Path(workspace), session_id))
        except Exception as e:
            _log(f"retry gate: could not read {session_id}'s transcript: {e!r}")
            return "unreadable transcript"

    def _deliver_failure(self, job: dict, eff: dict, st: dict,
                         session_id: "str|None") -> str:
        """Push a terminal failure through the install's failure_notifier when
        the job opted in, and return the deliveryStatus to stamp on the record:
        'delivered', 'failed' (the notifier raised), 'no-notifier' (opted in but
        the install ships none), or 'not-requested' (opt-out).

        A notifier that raises is logged and swallowed. A delivery fault must
        never become a scheduler fault — turning one job's failed alert into
        every job's stalled daemon would be strictly worse than the silence
        this exists to end."""
        if not eff.get("notify_on_failure"):
            return "not-requested"
        spec = config.install().get("failure_notifier")
        if not spec:
            _log(f"notify: {job.get('id')} opted into failure delivery but the "
                 f"install ships no failure_notifier")
            return "no-notifier"
        payload = {
            "job_id": job.get("id"),
            "agent_id": job.get("agent_id", ""),
            "status": st.get("last_status"),
            "error": st.get("last_error"),
            "consecutive_errors": int(st.get("consecutive_errors") or 0),
            "session_id": session_id,
        }
        try:
            spawn._load_hook(spec)(payload)
            return "delivered"
        except Exception as e:
            _log(f"notify: failure_notifier {spec!r} raised for "
                 f"{job.get('id')}: {e!r}")
            return "failed"

    def _defer_for_rate_limit(self, job: dict, st: dict, run) -> bool:
        """Pin next_run to just after the quoted reset so the job actually runs
        and writes its artifact. Capped so repeated same-day exhaustion doesn't
        hammer — past the cap, the already-advanced normal schedule stands.

        Returns True iff a retry was actually scheduled (next_run pinned). False
        means recovery is over — the caller must decide the terminal outcome
        (recurring: normal schedule stands; once: finalize, since there is no
        schedule to fall back on). Recovery ends on EITHER bound: the defer
        count, or the elapsed-time wall below. A missing reset time is not a
        False on its own: it retries blind, once and recurring alike."""
        jid = job["id"]
        defers = int(st.get("rate_limit_defers") or 0)
        reset_at = getattr(run, "rate_limit_reset_at", None)
        # The wall is measured from the slot the job was ORIGINALLY due for, so
        # it cannot be walked forward by the defers themselves — each deferred
        # run is scheduled for its deferred time, and anchoring on that would
        # re-arm the full runway at every hop and never expire.
        anchor_ms = int(st.get("rate_limit_anchor_ms")
                        or getattr(run, "scheduled_for_ms", None) or _ms(self._now()))
        deadline_ms = anchor_ms + _MAX_RATE_LIMIT_RECOVERY_SECONDS * 1000
        if defers < _MAX_RATE_LIMIT_DEFERS:
            if reset_at is not None:
                reset_ms = int(reset_at.timestamp() * 1000) + _RATE_LIMIT_BUFFER_SECONDS * 1000
                why = "reset"
            else:
                # Rate-limited but the reset time never made it out of the run's
                # output. Retry blind rather than surrender the slot — a daily
                # job that "falls back to the normal schedule" here is not
                # retrying, it is skipping the whole day.
                #
                # A once job takes the same arm, and used to be excluded on the
                # reasoning that a blind retry "recreates the dangling-zombie
                # failure". It does not: the zombie was a job left enabled with
                # next_run NULL, and this arm pins a real next_run before it
                # returns. Parking was never the only alternative to a zombie,
                # and treating it as one turned every unparseable rate limit on
                # a one-shot into dropped work — a one-shot publish wake fired
                # exactly on time, was rate-limited seconds in, and its post was
                # never published, with the terminal park reached before a
                # single retry. The cap below is what bounds this; past it a
                # once job still finalizes, which is the honest terminal state.
                reset_ms = _ms(self._now()) + _RATE_LIMIT_BLIND_RETRY_SECONDS * 1000
                why = "blind (no reset time parsed)"
            if reset_ms > deadline_ms:
                # The retry the outage is asking for lands past the wall. Give
                # up here rather than book it: the point of the wall is that
                # work this stale should not run, and a defer that honoured the
                # quoted reset anyway would walk straight through it — a weekly
                # cap quotes a reset days out, which is the exact case the wall
                # exists for.
                _log(f"rate-limited {jid} run={run.run_id} — retry at "
                     f"{datetime.fromtimestamp(reset_ms / 1000, tz=timezone.utc).isoformat()} "
                     f"is past the {_MAX_RATE_LIMIT_RECOVERY_SECONDS // 3600}h "
                     f"recovery wall; giving up on this slot")
                journal.save_state(self.state)
                return False
            st["next_run_at_ms"] = reset_ms
            st["rate_limit_defers"] = defers + 1
            st["rate_limit_anchor_ms"] = anchor_ms
            _log(f"rate-limited {jid} run={run.run_id} — deferred to "
                 f"{datetime.fromtimestamp(reset_ms / 1000, tz=timezone.utc).isoformat()} "
                 f"({why}, defer {defers + 1}/{_MAX_RATE_LIMIT_DEFERS})")
            journal.save_state(self.state)
            return True
        _log(f"rate-limited {jid} run={run.run_id} — defer cap "
             f"({_MAX_RATE_LIMIT_DEFERS}) reached; normal schedule stands")
        journal.save_state(self.state)
        return False

    def _roll_auth_episode(self, st: dict, now_ms: int) -> None:
        """Start a fresh retry budget when this auth failure is a new episode.

        The retry chain runs minutes apart, so two failures separated by much
        more than that are unrelated faults. Zeroing the counter for the second
        one is what keeps the cap from being a once-per-job-lifetime allowance:
        a job that spent it on Monday and never succeeded afterwards would meet
        Tuesday's transient expiry with no retries at all."""
        last_ms = int(st.get("auth_last_fail_ms") or 0)
        if now_ms - last_ms > _AUTH_EPISODE_SECONDS * 1000:
            st["auth_retries"] = 0
        st["auth_last_fail_ms"] = now_ms

    def _defer_for_auth_expiry(self, job: dict, st: dict, run) -> None:
        """Pin next_run a few minutes out after an auth expiry.

        Deferred, not immediate: the child died before reaching the model
        because the CLI could not refresh its stored OAuth session, and a
        re-spawn in the same breath meets that same credential. A few minutes
        gives the refresh a different attempt to make.

        Unlike the rate-limit defer there is no elapsed-time wall, because there
        is nothing to read a reset out of and nothing to chase: this books a
        FIXED delay a bounded number of times, so the drift is capped at
        `_MAX_AUTH_RETRIES * _AUTH_RETRY_SECONDS` by construction — a quarter of
        an hour, well inside the staleness the rate-limit wall exists to bound.

        Always books a retry; the caller checks the cap first (`auth_retry_left`)
        and takes the terminal branch itself when it is spent."""
        jid = job["id"]
        retries = int(st.get("auth_retries") or 0)
        retry_ms = _ms(self._now()) + _AUTH_RETRY_SECONDS * 1000
        st["next_run_at_ms"] = retry_ms
        st["auth_retries"] = retries + 1
        _log(f"auth-expired {jid} run={run.run_id} — deferred to "
             f"{datetime.fromtimestamp(retry_ms / 1000, tz=timezone.utc).isoformat()} "
             f"(retry {retries + 1}/{_MAX_AUTH_RETRIES})")
        journal.save_state(self.state)

    def _finalize_once(self, job: dict, status: str) -> None:
        jid = job["id"]
        sched = job.get("schedule") or {}
        try:
            if status == "ok" and sched.get("delete_after_run"):
                self.registry = registry.mutate(
                    lambda reg: reg.update(
                        jobs=[j for j in reg["jobs"] if j.get("id") != jid]) or reg)
                self.state.pop(jid, None)
                journal.save_state(self.state)
                _log(f"one-shot {jid} completed — removed from registry")
            else:
                # A one-shot is spent the moment it finishes, either way. Its
                # dtstart is in the past, so leaving it enabled parks a corpse
                # that can never fire again but ages forever — every staleness
                # check reads it as a dead job. delete_after_run chooses
                # removal vs retention; it does not mean "stay enabled".
                def _park(reg):
                    for j in reg["jobs"]:
                        if j.get("id") == jid:
                            j["enabled"] = False
                    return reg
                self.registry = registry.mutate(_park)
                _log(f"one-shot {jid} {'completed' if status == 'ok' else 'failed'}"
                     f" — parked (enabled:false)")
        except (OSError, ValueError) as e:
            _log(f"one-shot finalize failed for {jid}: {e!r}")
        try:
            self._registry_mtime = config.SCHEDULE_FILE.stat().st_mtime
        except OSError:
            self._registry_mtime = None

    # -------------------------------------------------------------- reconcile

    def reconcile(self) -> None:
        """Daemon start: re-own recorded active runs; dead pgid → orphaned."""
        with self.lock:
            self._reload_registry_if_changed()
            defaults = self.defaults()
            dirty = False
            for jid, st in self.state.items():
                if not isinstance(st, dict):
                    continue
                remaining = []
                for entry in st.get("active_runs", []):
                    pgid = entry.get("pgid")
                    job = self._job_by_id(jid) or {"id": jid}
                    if pgid and runner.pgid_alive(pgid):
                        adopted = runner.AdoptedRun(
                            jid, entry, registry.effective(job, defaults, self.categories()),
                            defaults, self._on_run_finish).start()
                        self.runs[adopted.run_id] = adopted
                        remaining.append(entry)
                        _log(f"adopted live run {entry.get('run_id')} (pgid {pgid})")
                        continue
                    now_ms = _ms(self._now())
                    spawned = int(entry.get("spawned_at_ms") or now_ms)
                    summary = ""
                    if entry.get("workspace") and entry.get("session_id"):
                        summary = runner.last_text_block(runner.session_jsonl_path(
                            runner.Path(entry["workspace"]), entry["session_id"]))
                    journal.append(jid, journal.finished_record(
                        job_id=jid, agent_id=job.get("agent_id", ""),
                        status="orphaned", summary=summary,
                        session_id=entry.get("session_id") or "",
                        run_at_ms=int(entry.get("scheduled_for_ms") or spawned),
                        duration_ms=max(0, now_ms - spawned),
                        next_run_at_ms=st.get("next_run_at_ms"),
                        model=job.get("model") or defaults.get("model", ""),
                        run_id=entry.get("run_id") or "",
                        spawned_at_ms=spawned, exit_code=None,
                        kill_reason="orphaned", retry_of=None))
                    st["last_status"] = "orphaned"
                    dirty = True
                    _log(f"orphaned dead run {entry.get('run_id')} (pgid {pgid})")
                if len(remaining) != len(st.get("active_runs", [])):
                    st["active_runs"] = remaining
                    dirty = True
            if dirty:
                journal.save_state(self.state)

    # ------------------------------------------------------------ api surface

    def health(self) -> dict:
        with self.lock:
            self._reload_registry_if_changed()
            enabled = [j for j in self.jobs() if j.get("enabled", True)]
            next_ms = [
                self.state.get(j["id"], {}).get("next_run_at_ms") for j in enabled
            ]
            next_ms = [m for m in next_ms if m]
            next_fire = (
                datetime.fromtimestamp(min(next_ms) / 1000, tz=timezone.utc).isoformat()
                if next_ms else None
            )
            return {
                "ok": True,
                "uptime_s": int(time.time() - self.started_at),
                "active_runs": len(self.runs),
                "jobs_enabled": len(enabled),
                "next_fire_at": next_fire,
            }

    def jobs_view(self) -> list[dict]:
        with self.lock:
            self._reload_registry_if_changed()
            out = []
            for job in self.jobs():
                st = self.state.get(job["id"], {})
                merged = dict(job)
                merged["state"] = {k: v for k, v in st.items() if k != "active_runs"}
                merged["active_runs"] = st.get("active_runs", [])
                out.append(merged)
            return out

    def runs_view(self) -> list[dict]:
        with self.lock:
            return [
                {
                    "run_id": r.run_id,
                    "job_id": r.job_id,
                    "session_id": r.session_id,
                    "pid": r.pid,
                    "pgid": r.pgid,
                    "spawned_at_ms": r.spawned_at_ms,
                    "scheduled_for_ms": r.scheduled_for_ms,
                    "retry_of": r.retry_of,
                }
                for r in self.runs.values()
            ]

    def run_now(self, job_id: str) -> bool:
        with self.lock:
            self._reload_registry_if_changed()
            job = self._job_by_id(job_id)
            if job is None:
                return False
            defaults = self.defaults()
            if len(self.runs) >= int(defaults.get("max_concurrent_runs", 4)):
                return False
            eff = dict(registry.effective(job, defaults, self.categories()))
            # manual fire: schedule untouched (advance=False), policy bypassed
            eff["concurrency"] = "parallel"
            self._try_fire(job, eff, self._now(), defaults,
                           retry_of=None, advance=False)
            return True

    def kill_run(self, run_id: str) -> bool:
        with self.lock:
            run = self.runs.get(run_id)
        if run is None:
            return False
        # grace-and-escalate runs in a thread so the API replies immediately
        threading.Thread(target=run.kill, args=("manual",), daemon=True).start()
        return True

    # -------------------------------------------------------------- lifecycle

    def _sweep_logs(self, now: datetime) -> None:
        day = now.strftime("%Y-%m-%d")
        if self._last_sweep_day == day:
            return
        self._last_sweep_day = day
        cutoff = time.time() - config.LOG_RETENTION_DAYS * 86400
        if not config.LOGS_DIR.is_dir():
            return
        for f in config.LOGS_DIR.glob("*.out"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass

    def run_forever(self) -> None:
        self.reconcile()
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                _log(f"tick error: {e!r}")
            self._stop.wait(config.TICK_SECONDS)

    def stop(self) -> None:
        self._stop.set()
