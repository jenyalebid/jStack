"""Token spend — where the day's tokens actually went, by job. The app's
Spend section.

Every Claude session writes a JSONL transcript under `~/.claude/projects/`.
Each assistant message in it carries the exact `usage` block the API
returned: input, cache-creation, cache-read, output. That is the ground truth
for what a job cost — not an estimate, not a sample. This module reads it.

The one scanner, on every host. Same records, same `daily()` shape, same
trim on the wire — so the phone and the Mac never disagree about what a job
cost. Everything machine-flavoured comes through `hostenv`: the seat behind
a project dir, where a finer category map may sit, and which timezone a
"day" is.

Three facts shape the design:

**A session is the unit of attribution.** A job (a scheduled run, a chat) is
one session, and its category is decided by its FIRST user message — the
marker the spawner passed in. Later turns cannot change what a session was
started to do, so classification happens once and the whole transcript
inherits it.

**Subagents belong to their parent.** Agent-tool spawns write to
`<session>/subagents/agent-*.jsonl`, a sibling tree with its own usage blocks.
Those tokens are real and they are the parent job's cost, so they are
attributed to the parent's category and counted separately as `sub_tokens`.

**Cache reads dominate and that is the point.** A long session re-sends its
whole context every turn, so `cache_read` is typically >90% of a day.
Reporting only input+output would hide the entire cost structure. `total`
here means all four counters summed — the tokens the session actually moved.

Rescanning every transcript costs seconds and grows without bound, so results
are cached per file, keyed on (size, mtime). A live session's file changes and
is re-read; a finished one is read exactly once, ever.

**Categories.** jStack's scheduler opens every run with a `[cron:…]` marker
and a self-booked wake with `[wake…]` / `[scheduled…]`; anything else was
typed by a person. That split — autonomous against interactive — is the one
the Spend screen draws, and it is the same on every jStack machine, which is
why it is code rather than a file. A host that wants finer buckets puts a
`token_categories.json` where `hostenv.spend_categories_path()` answers (the
state dir by default; a profile may pin it elsewhere), shaped
`{"rules": [{id, label, kind, signature, catch_all?}, …]}`, last rule the
catch-all, and it replaces the built-in map wholesale.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from . import codex_transcript, hostenv

PROJECTS = Path.home() / ".claude" / "projects"
CONFIG = hostenv.spend_categories_path()
CACHE = hostenv.state_dir() / "token_usage" / "cache.json"
CACHE_VERSION = 5

USAGE_KEYS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)

# The built-in map. Autonomous first, because every marker a machine writes
# is a prefix; the interactive rule is the catch-all a person's prompt falls
# to. Both are catch-alls in the dashboard's sense — a day with nothing
# unexplained is the normal one here, so `catch_all_sessions` counts only the
# autonomous bucket, the way the dashboard's own map does.
_BUILTIN_RULES = [
    {"id": "scheduled", "label": "Scheduled runs", "kind": "autonomous",
     "signature": r"^\[(cron|wake|scheduled)[: ]", "catch_all": True},
    {"id": "chat", "label": "Chat", "kind": "interactive",
     "signature": r".*", "catch_all": True},
]


def _load_rules() -> list[dict]:
    rules_raw = _BUILTIN_RULES
    try:
        data = json.loads(CONFIG.read_text())
        custom = [r for r in data.get("rules", [])
                  if isinstance(r, dict) and r.get("id") and r.get("signature")]
        if custom:
            rules_raw = custom
    except (OSError, ValueError):
        pass
    rules = []
    for r in rules_raw:
        try:
            rules.append({**r, "_re": re.compile(r["signature"], re.I)})
        except re.error:
            continue
    return rules or [{**r, "_re": re.compile(r["signature"], re.I)}
                     for r in _BUILTIN_RULES]


def _first_user_message(path: Path) -> str:
    """The prompt the session was started with.

    Skips sidechain records (subagent turns interleaved into some transcripts)
    and empty/whitespace bodies — a spawner sometimes emits a blank turn before
    the real payload, and classifying on that would put every such job in the
    catch-all.
    """
    try:
        with path.open(errors="ignore") as fh:
            for line in fh:
                if '"user"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") == "response_item":
                    message = d.get("payload") or {}
                    if message.get("type") != "message" or message.get("role") != "user":
                        continue
                elif d.get("type") == "user" and not d.get("isSidechain"):
                    message = d.get("message") or {}
                else:
                    continue
                content = message.get("content")
                if isinstance(content, str):
                    text = content
                else:
                    text = " ".join(
                        c.get("text", "")
                        for c in (content or [])
                        if isinstance(c, dict) and c.get("type") in ("text", "input_text")
                    )
                text = " ".join((text or "").split())
                if text:
                    return text
    except OSError:
        pass
    return ""


def classify(first_message: str, rules: list[dict]) -> dict:
    for rule in rules:
        if rule["_re"].search(first_message):
            return rule
    return rules[-1]


def _agent_of(project_dir: str) -> str:
    """Human name for the seat that ran the job — `ops/chat` — through the
    profile, which is the one reader of Claude Code's project-dir encoding.
    Sandboxed spawns (/private/tmp/...) have no seat and report as the
    sandbox they ran in."""
    parsed = hostenv.project_dir_to_agent(project_dir)
    if parsed:
        base, mode = parsed
        return f"{base}/{mode}" if mode and mode != "default" else base
    if project_dir.startswith("-private-tmp") or project_dir.startswith("-tmp"):
        return "sandbox"
    return project_dir.strip("-").replace("-", "/")[:40] or "?"


def _scan_file(path: Path) -> dict:
    """Per-day usage totals for one transcript. No classification here.

    One API turn is written as SEVERAL JSONL lines — one per content block
    (thinking, text, each tool_use) — and every one of those lines repeats the
    same `message.id` and the same `usage` block verbatim. Summing per line
    therefore bills a turn once per block. Usage is accumulated once per
    `message.id`; lines without an id (older transcripts) fall back to
    counting each line, which is correct for the one-line-per-message shape
    they were written in.

    Days are bucketed in `hostenv.day_tz()` — the host's local clock unless
    the profile pins a zone so the split agrees with the machine's other
    day-labelled reports.
    """
    tz = hostenv.day_tz()
    days: dict[str, list[int]] = {}
    seen: set[str] = set()
    try:
        with path.open(errors="ignore") as fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") != "assistant":
                    continue
                ts = d.get("timestamp")
                if not ts:
                    continue
                try:
                    when = datetime.fromisoformat(
                        ts.replace("Z", "+00:00")).astimezone(tz)
                except ValueError:
                    continue
                message = d.get("message") or {}
                mid = message.get("id")
                if mid:
                    if mid in seen:
                        continue
                    seen.add(mid)
                usage = message.get("usage") or {}
                row = days.setdefault(when.strftime("%Y-%m-%d"), [0, 0, 0, 0, 0])
                for i, key in enumerate(USAGE_KEYS):
                    row[i] += usage.get(key, 0) or 0
                row[4] += 1
    except OSError:
        pass
    return days


def _scan_codex_file(path: Path, session_id: str) -> dict:
    """Per-response native usage; cached tokens are included in input_tokens.

    token_count repeats cumulative counters and is deliberately not summed.
    Forks contain copied history but must never charge the source's responses.
    """
    days, seen = {}, set()
    with path.open(errors="ignore") as stream:
        for line in stream:
            try:
                row = json.loads(line)
                if row.get("type") != "token_usage_record":
                    continue
                payload = row.get("payload") or {}
                if payload.get("thread_id") != session_id:
                    continue
                response_id = payload.get("response_id")
                if response_id and response_id in seen:
                    continue
                seen.add(response_id)
                day = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).astimezone(hostenv.day_tz()).strftime("%Y-%m-%d")
                usage = payload.get("usage") or {}
                cache_read = int(usage.get("cached_input_tokens") or 0)
                cache_write = int(usage.get("cache_write_input_tokens") or 0)
                values = [max(0, int(usage.get("input_tokens") or 0) - cache_read - cache_write),
                          cache_write, cache_read, int(usage.get("output_tokens") or 0), 1]
                totals = days.setdefault(day, [0, 0, 0, 0, 0])
                for i, value in enumerate(values):
                    totals[i] += value
            except (ValueError, KeyError, TypeError):
                continue
    return days


def _cache_load() -> dict:
    try:
        data = json.loads(CACHE.read_text())
        if data.get("version") == CACHE_VERSION:
            return data.get("files", {})
    except (OSError, ValueError):
        pass
    return {}


def _cache_save(files: dict) -> None:
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": CACHE_VERSION, "files": files}))
        tmp.replace(CACHE)
    except OSError:
        pass


def scan(use_cache: bool = True, projects: Path | None = None) -> list[dict]:
    """Every session on disk, classified, with per-day usage.

    Returns one record per (session, day) — a session that crosses midnight
    contributes to both days, each with only the tokens it spent there.
    """
    include_codex = projects is None
    projects = projects or PROJECTS
    rules = _load_rules()
    cache = _cache_load() if use_cache else {}
    fresh: dict = {}
    records: list[dict] = []
    sources = [(path, False) for path in sorted(projects.glob("*/*.jsonl"))]
    if include_codex:
        sources.extend((path, True) for folder in (codex_transcript.root(), codex_transcript.root().parent / "archived_sessions")
                       for path in sorted(folder.glob("**/*.jsonl")))
    for main, native in sources:
        project_dir = main.parent.name
        session_id = main.stem
        if native:
            meta = codex_transcript.metadata(main)
            if not meta.get("id") or not meta.get("cwd"):
                continue
            session_id = meta["id"]
            project_dir = meta["cwd"].replace("/", "-").replace(".", "-")
        sub_dir = main.with_suffix("")
        subs = sorted(sub_dir.glob("subagents/*.jsonl")) if sub_dir.is_dir() else []
        try:
            sig = [[main.stat().st_size, int(main.stat().st_mtime)]]
        except OSError:
            continue
        for s in subs:
            try:
                sig.append([s.stat().st_size, int(s.stat().st_mtime)])
            except OSError:
                pass
        key = f"{'codex/' if native else ''}{project_dir}/{session_id}"
        hit = cache.get(key)
        if hit and hit.get("sig") == sig:
            entry = hit
        else:
            first = _first_user_message(main)
            main_days = _scan_codex_file(main, session_id) if native else _scan_file(main)
            sub_days: dict[str, list[int]] = {}
            for s in subs:
                for day_key, row in _scan_file(s).items():
                    acc = sub_days.setdefault(day_key, [0, 0, 0, 0, 0])
                    for i in range(5):
                        acc[i] += row[i]
            entry = {
                "sig": sig,
                "first": first[:400],
                "main": main_days,
                "sub": sub_days,
                "n_sub": len(subs),
            }
        fresh[key] = entry
        rule = classify(entry.get("first", ""), rules)
        for day_key in set(entry["main"]) | set(entry["sub"]):
            m = entry["main"].get(day_key, [0, 0, 0, 0, 0])
            s = entry["sub"].get(day_key, [0, 0, 0, 0, 0])
            records.append({
                "day": day_key,
                "category": rule["id"],
                "label": rule["label"],
                "kind": rule["kind"],
                "catch_all": bool(rule.get("catch_all")),
                "agent": _agent_of(project_dir),
                "project": project_dir,
                "session": session_id,
                "n_sub": entry["n_sub"],
                "input": m[0] + s[0],
                "cache_write": m[1] + s[1],
                "cache_read": m[2] + s[2],
                "output": m[3] + s[3],
                "turns": m[4],
                "sub_tokens": sum(s[:4]),
                "total": sum(m[:4]) + sum(s[:4]),
                "first": entry.get("first", "")[:200],
            })
    if use_cache:
        _cache_save(fresh)
    return records


def daily(day: str, records: list[dict] | None = None) -> dict:
    """One day's spend, broken out by category — what the screens render."""
    records = records if records is not None else scan()
    rows = [r for r in records if r["day"] == day]
    total = sum(r["total"] for r in rows) or 1
    cats: dict[str, dict] = {}
    for r in rows:
        c = cats.setdefault(r["category"], {
            "category": r["category"], "label": r["label"], "kind": r["kind"],
            "catch_all": r["catch_all"], "total": 0, "output": 0,
            "cache_read": 0, "sub_tokens": 0, "turns": 0,
            "sessions": 0, "agents": defaultdict(int),
        })
        c["total"] += r["total"]
        c["output"] += r["output"]
        c["cache_read"] += r["cache_read"]
        c["sub_tokens"] += r["sub_tokens"]
        c["turns"] += r["turns"]
        c["sessions"] += 1
        c["agents"][r["agent"]] += r["total"]
    out = []
    for c in sorted(cats.values(), key=lambda x: -x["total"]):
        c["pct"] = round(100 * c["total"] / total, 1)
        c["agents"] = sorted(
            ({"agent": a, "total": t} for a, t in c["agents"].items()),
            key=lambda x: -x["total"])[:6]
        out.append(c)
    autonomous = sum(c["total"] for c in out if c["kind"] == "autonomous")
    return {
        "day": day,
        "total": sum(r["total"] for r in rows),
        "output": sum(r["output"] for r in rows),
        "sessions": len(rows),
        "autonomous": autonomous,
        "interactive": sum(c["total"] for c in out if c["kind"] == "interactive"),
        "autonomous_pct": round(100 * autonomous / total, 1),
        "categories": out,
        # Machine-started sessions no finer rule claimed. With the built-in
        # map that is every scheduled run, which is the honest count: a host
        # with one autonomous bucket has nothing unexplained in it.
        "catch_all_sessions": sum(
            c["sessions"] for c in out
            if c["catch_all"] and c["kind"] == "autonomous"),
    }


def series(days: int = 14, records: list[dict] | None = None) -> list[dict]:
    """Daily totals for the last N days, newest last."""
    records = records if records is not None else scan()
    today_d = datetime.now(hostenv.day_tz()).date()
    out = []
    for i in range(days - 1, -1, -1):
        day_key = (today_d - timedelta(days=i)).strftime("%Y-%m-%d")
        rows = [r for r in records if r["day"] == day_key]
        by_cat: dict[str, int] = defaultdict(int)
        for r in rows:
            by_cat[r["category"]] += r["total"]
        out.append({
            "day": day_key,
            "total": sum(r["total"] for r in rows),
            "by_category": dict(by_cat),
        })
    return out


def top_sessions(day: str, limit: int = 12,
                 records: list[dict] | None = None) -> list[dict]:
    """The day's most expensive individual jobs."""
    records = records if records is not None else scan()
    rows = sorted((r for r in records if r["day"] == day),
                  key=lambda r: -r["total"])[:limit]
    return [{
        "label": r["label"], "category": r["category"], "agent": r["agent"],
        "total": r["total"], "turns": r["turns"], "n_sub": r["n_sub"],
        "sub_tokens": r["sub_tokens"], "session": r["session"],
        "first": r["first"][:120],
    } for r in rows]


def today() -> str:
    return datetime.now(hostenv.day_tz()).strftime("%Y-%m-%d")
