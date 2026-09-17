"""Account allowance — how much of each provider's window is spent. The
app's Usage bars.

This is NOT token accounting. `spend.py` answers "what did today cost" by
summing transcripts. This answers a different question with a different
source: **how close is the account to being cut off**, which is a percentage
of a provider-side allowance we do not compute and cannot derive from our own
transcripts. The two never merge — one is spend, one is headroom, and a
number that looked like both would be trusted as neither.

The package's own reader, the standalone counterpart of the dashboard's
`lib/usage_caps` (which the router prefers where it imports), producing the
same payload the app already decodes.

## Where the numbers come from

Two tiers, and a host has the first one the moment Claude Code is installed:

1. **The CLI's own cache.** Claude Code fetches the account's utilization for
   its `/usage` screen and keeps the answer in `~/.claude.json`
   (`cachedUsageUtilization`: `five_hour` and `seven_day`, each a percentage
   and a reset clock, stamped when fetched). Every interactive session
   refreshes it. Free, no auth, no hook to wire — the reason a fresh install
   shows bars without anyone configuring anything.
2. **Codex rollout events.** The CLI records account `rate_limits` alongside
   token counts. Their own timestamps, percentages, and reset clocks feed the
   Codex bars without another login or an API credential.
3. **A recorded sample.** `record()` stores what a sampler saw — a status-line
   hook handed `rate_limits` on every render, a poll of the provider's usage
   endpoint. Whichever tier's sample is newest is the one served.

Refusals are a third fact, deliberately not merged with either: the provider
actually turned a request away. jStack's scheduler already sees that from a
HEADLESS run — it parses "You've hit your limit · resets 9:40am" out of a
run's output and scores it `rate_limited` — so `sync_from_scheduler()` reads
its state as a free probe of exactly the sessions the cache cannot see.

## Staleness is a first-class answer

A sample older than `STALE_AFTER` is reported `stale: true` with its age. It
is NOT dropped and NOT rounded to a fresh-looking figure. An overnight stretch
legitimately has no interactive session in it, so the meter going stale is
the honest reading — "last seen 6h ago at 61%" is actionable, "61%" alone is a
lie about when it was true.

A provider that has never reported is **absent**, not zero: `read()` returns
`None` for it and the app draws "not connected". Zeroing an unsampled
provider would paint an unknown as healthy, which is the one answer a
headroom meter exists to prevent.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from . import hostenv

STATE = hostenv.state_dir() / "allowance.json"
LOCK = hostenv.state_dir() / ".allowance.lock"
CLI_CONFIG = Path.home() / ".claude.json"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"

#: Providers we know how to render. A provider listed here but never sampled
#: reads as None (not connected) — never as zero.
PROVIDERS = {
    "claude": "Claude",
    "codex": "Codex",
}

#: A sample older than this no longer describes now.
STALE_AFTER = 900
#: A refusal with no parseable reset clock still blocks for a while.
REFUSAL_ASSUMED_SECONDS = 2700
#: The furthest out a window rollover can honestly be. Claude quotes five
#: hours and seven days, Codex a week; nothing any provider calls an allowance
#: window is a month long, so a clock past this is a bad reading rather than a
#: patient one, and it is dropped instead of drawn.
MAX_RESET_HORIZON = 35 * 86_400
WARN_PCT = 80
CRITICAL_PCT = 95
SOURCES = ("statusline", "poll", "refusal", "rollout", "cli-cache")

#: Claude Code's window ids, and what the app calls them. Order is fixed so
#: the readout never reshuffles between fetches.
_CLI_WINDOWS = (("five_hour", "Session (5h)"), ("seven_day", "Week"))

#: Severity ladder. `capped` is a window MEASURED at 100; `refused` is the
#: provider actually turning a request away.
_BAND_ORDER = {None: 0, "warn": 1, "critical": 2, "capped": 3, "refused": 4}

#: Reset clocks already refused this process, so a cache that stays wrong is
#: reported once rather than on every poll.
_BAD_RESETS: set = set()


# ---------------------------------------------------------------- state io

def _now() -> float:
    return time.time()


def _read_raw() -> dict:
    try:
        d = json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        d = {}
    d.setdefault("providers", {})
    d.setdefault("scheduler_watermark_ms", 0)
    return d


def _write_raw(d: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=2, sort_keys=True))
    os.replace(tmp, STATE)


class _Lock:
    """Cross-process lock. Several samplers may write concurrently — one per
    live session, every render — so read-modify-write must not interleave."""

    def __enter__(self):
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(LOCK, "w")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()
        return False


def _provider_slot(d: dict, provider: str) -> dict:
    slot = d["providers"].setdefault(provider, {})
    slot["label"] = PROVIDERS.get(provider, provider)
    slot.setdefault("windows", [])
    slot.setdefault("refusal", None)
    return slot


# ---------------------------------------------------------------- recording

def _norm_reset(raw, source: str = "?", wid: str = "?") -> str | None:
    """A reset clock this host is prepared to defend, or None.

    **Neither tier's clock is ours.** The CLI cache is a read of a file another
    program owns, and a recorded sample is whatever a hook forwarded out of
    `rate_limits`. So the value is checked here rather than at the bar, because
    the app renders `resets_at` verbatim and a countdown is the one field where
    a wrong number still looks exactly like a right one — "resets in 1208d 21h"
    under a five-hour window, which is what the bars drew on 2026-09-09.

    Three shapes go in: an ISO instant (kept as written, so the payload does
    not churn), an epoch number in seconds or milliseconds (Claude Code hands
    the status line seconds), and junk. Two things come back out: a string, or
    None — and **None is the honest answer**, the same one a provider that
    quoted no clock gives. The bar then draws no line, which is what the app
    already does for a reset it cannot read.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):  # a bool is an int; a clock is not either
        return _note_bad_reset(raw, source, wid)
    if isinstance(raw, (int, float)):
        secs = float(raw) / 1000.0 if abs(float(raw)) > 1e11 else float(raw)
        try:
            iso = datetime.fromtimestamp(secs, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return _note_bad_reset(raw, source, wid)
    else:
        iso, secs = str(raw), _parse_iso(raw)
        if secs is None:
            return _note_bad_reset(raw, source, wid)
    if secs - _now() > MAX_RESET_HORIZON:
        return _note_bad_reset(raw, source, wid)
    return iso


def _note_bad_reset(raw, source: str, wid: str) -> None:
    """Drop a clock, and leave the raw value where the next person can read it.

    Deduped in memory rather than by file: a cache that stays wrong is read on
    every poll, and a line per poll would be a log that buries its own finding.
    Best-effort — a reader that cannot write must still answer.
    """
    key = (source, wid, repr(raw))
    if key in _BAD_RESETS:
        return None
    _BAD_RESETS.add(key)
    try:
        line = json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                           "source": source, "window": wid, "resets_at": raw})
        with open(hostenv.state_dir() / "allowance_rejects.jsonl", "a") as fh:
            fh.write(line + "\n")
    except (OSError, TypeError, ValueError):
        pass
    return None


def _norm_window(w: dict, source: str = "?") -> dict:
    pct = w.get("pct")
    if pct is not None:
        pct = max(0.0, min(100.0, float(pct)))
    return {
        "id": str(w["id"]),
        "label": str(w.get("label") or w["id"]),
        "pct": pct,
        "resets_at": _norm_reset(w.get("resets_at"), source, str(w.get("id"))),
    }


def record(provider: str, windows: list[dict], source: str = "statusline",
           sampled_at: float | None = None) -> None:
    """Store a measured sample for `provider`.

    `windows` is a list of `{id, label, pct, resets_at}`. Whatever a provider
    calls its windows is the provider's business — nothing here reads an id.

    A recorded sample does NOT clear a standing refusal. The two are different
    observations and the refusal expires on its own clock.
    """
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r} (expected one of {SOURCES})")
    ts = sampled_at if sampled_at is not None else _now()
    norm = [_norm_window(w, source) for w in windows]
    with _Lock():
        d = _read_raw()
        slot = _provider_slot(d, provider)
        slot["source"] = source
        slot["sampled_at"] = ts
        slot["windows"] = norm
        _write_raw(d)


def note_refusal(provider: str, *, resets_at: str | None = None,
                 detail: str = "", at: float | None = None) -> None:
    """Record that the provider actually turned a request away.

    Provider-level and percentage-free by design. Merges into the existing
    sample rather than replacing it, so a refusal does not erase what we last
    knew about the windows.
    """
    ts = at if at is not None else _now()
    with _Lock():
        d = _read_raw()
        slot = _provider_slot(d, provider)
        prev = slot.get("refusal") or {}
        # Keep the earliest observation of a refusal that is still the same
        # outage: the reset clock identifies it, and the first sighting is
        # when the outage actually began.
        same = prev.get("resets_at") == resets_at and _refusal_active(prev)
        slot["refusal"] = {
            "at": prev.get("at") if same else ts,
            "last_seen": ts,
            "resets_at": resets_at,
            "detail": detail[:300],
        }
        slot.setdefault("source", "refusal")
        slot.setdefault("sampled_at", ts)
        _write_raw(d)


# ---------------------------------------------------------------- the CLI's cache

def cli_cache_sample(path: Path | None = None) -> dict | None:
    """Claude Code's own reading, as a provider slot — or None when the CLI
    has never fetched one on this machine.

    `~/.claude.json` is the CLI's file and this is a read of it, nothing
    more: the key, the window names and the stamp are whatever it wrote. A
    file that is missing, unreadable, or carries no utilization block answers
    None, which `read()` renders as "not connected" — never as zero.
    """
    path = path or CLI_CONFIG
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    cached = raw.get("cachedUsageUtilization") if isinstance(raw, dict) else None
    if not isinstance(cached, dict):
        return None
    util = cached.get("utilization")
    if not isinstance(util, dict):
        return None
    windows = []
    for wid, label in _CLI_WINDOWS:
        w = util.get(wid)
        if not isinstance(w, dict):
            continue
        pct = w.get("utilization")
        if pct is None:
            continue
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            continue
        windows.append({"id": wid, "label": label, "pct": pct,
                        "resets_at": w.get("resets_at")})
    if not windows:
        return None
    fetched = cached.get("fetchedAtMs")
    try:
        sampled_at = float(fetched) / 1000.0
    except (TypeError, ValueError):
        sampled_at = 0.0
    return {"label": PROVIDERS["claude"], "source": "cli-cache",
            "sampled_at": sampled_at,
            "windows": [_norm_window(w, "cli-cache") for w in windows],
            "refusal": None}


# ---------------------------------------------------------------- reading

def _parse_iso(s) -> float | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _refusal_active(refusal: dict | None) -> bool:
    """Is the refusal still presumed to be blocking? Decided by the clock,
    never by "did something newer arrive"."""
    if not refusal:
        return False
    reset = _parse_iso(refusal.get("resets_at"))
    if reset is not None:
        return _now() < reset
    return _now() - float(refusal.get("at") or 0) < REFUSAL_ASSUMED_SECONDS


def _window_band(w: dict) -> str | None:
    pct = w.get("pct")
    if pct is None:
        return None
    if pct >= 100:
        return "capped"
    if pct >= CRITICAL_PCT:
        return "critical"
    if pct >= WARN_PCT:
        return "warn"
    return None


def _worst(bands: list) -> str | None:
    return max(bands, key=lambda b: _BAND_ORDER.get(b, 0)) if bands else None


def _merged_providers() -> dict:
    """Newest provider sample from recorded data or the CLI's own files.

    Recorded refusals survive either source; they are separate from usage.
    """
    d = _read_raw()
    providers = dict(d["providers"])
    for pid, cli in (("claude", cli_cache_sample()), ("codex", codex_rollout_sample())):
        if cli is None:
            continue
        have = providers.get(pid) or {}
        recorded = have.get("sampled_at") if have.get("windows") else None
        if float(cli["sampled_at"]) >= float(recorded or 0):
            merged = dict(have, **cli)
            merged["refusal"] = have.get("refusal")
            providers[pid] = merged
    return providers


def read() -> dict:
    """Full state, with staleness and refusal expiry resolved at read time.

    Every provider in `PROVIDERS` appears. One never sampled is `None` — the
    caller must render "not connected", never a zero meter."""
    providers = _merged_providers()
    now = _now()
    out: dict = {"generated_at": datetime.now(timezone.utc).isoformat(),
                 "stale_after_seconds": STALE_AFTER,
                 "thresholds": {"warn": WARN_PCT, "critical": CRITICAL_PCT},
                 "providers": {}}
    for pid, label in PROVIDERS.items():
        p = providers.get(pid)
        if not p or (not p.get("windows") and not p.get("refusal")):
            out["providers"][pid] = None
            continue
        age = now - float(p.get("sampled_at") or 0)
        # Normalised again on the way out, not just on the way in: a sample
        # recorded before this host learned to check clocks is still sitting in
        # the file, and a horizon is a judgment about NOW — the same stored
        # value is fine today and nonsense once its window has been and gone.
        windows = [dict(w, band=_window_band(w))
                   for w in (_norm_window(w, p.get("source") or "?")
                             for w in p.get("windows") or [])]
        refusal = p.get("refusal")
        active = _refusal_active(refusal)
        bands = [w.get("band") for w in windows]
        if active:
            bands.append("refused")
        out["providers"][pid] = {
            "label": p.get("label") or label,
            "source": p.get("source"),
            "sampled_at": p.get("sampled_at"),
            "age_seconds": round(age, 1),
            "stale": age > STALE_AFTER,
            "windows": windows,
            "refusal": (dict(refusal, active=active) if refusal else None),
            "worst_band": _worst(bands),
        }
    return out


def available() -> bool:
    """Can this host say anything at all — a sample on file, or a CLI cache
    to read. Nothing on either is the one case the screen is honest to hide."""
    if cli_cache_sample() is not None or codex_rollout_sample() is not None:
        return True
    return any(p and (p.get("windows") or p.get("refusal"))
               for p in _read_raw()["providers"].values())


# ------------------------------------------------- headless tier: scheduler

def _reset_from_next_run(state: dict) -> str | None:
    ms = state.get("next_run_at_ms")
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat() \
            if ms else None
    except (TypeError, ValueError, OverflowError):
        return None


def sync_from_scheduler(state_path: Path | None = None) -> bool:
    """Turn the scheduler's `rate_limited` runs into a refusal. True if one
    new outage was folded in.

    jStack's scheduler is the one component that already sees a cap from a
    HEADLESS session. Its per-job state carries `last_status` and
    `last_run_at_ms`; a watermark on the latter keeps one outage from being
    re-recorded on every read. The state file is either a map of jobs or a
    `{"jobs": {...}}` wrapper, depending on the scheduler's vintage.
    """
    path = state_path or (hostenv.scheduler_state_dir() / "state.json")
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    jobs = raw.get("jobs") if isinstance(raw, dict) and isinstance(raw.get("jobs"), dict) \
        else raw
    if not isinstance(jobs, dict):
        return False
    with _Lock():
        mark = int(_read_raw().get("scheduler_watermark_ms") or 0)
    newest, hit = mark, None
    for st in jobs.values():
        if not isinstance(st, dict) or st.get("last_status") != "rate_limited":
            continue
        ran = int(st.get("last_run_at_ms") or 0)
        if ran > newest:
            newest, hit = ran, st
    if hit is None:
        return False
    note_refusal("claude", resets_at=_reset_from_next_run(hit),
                 detail=f"scheduler run rate-limited (last_error={hit.get('last_error')})",
                 at=newest / 1000)
    with _Lock():
        d = _read_raw()
        d["scheduler_watermark_ms"] = newest
        _write_raw(d)
    return True


def codex_rollout_sample() -> dict | None:
    """Newest account quota actually reported by Codex; no separate login."""
    from .codex_transcript import summary
    newest = None
    for path in CODEX_SESSIONS.glob("**/rollout-*.jsonl"):
        sample = summary(path).get("rate_sample")
        if sample and (newest is None or sample["sampled_at"] > newest["sampled_at"]):
            newest = sample
    return newest
