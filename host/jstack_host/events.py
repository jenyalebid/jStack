"""Session notification events: derived from the transcript, logged thin.

The timeline the app shows (`GET /sessions/{sid}/timeline`) is a pure
function of the session's own JSONL — turn ends and mid-turn narration,
selected at the same cadence the pushes use, so it reads as "what you'd
have been told". Derivation works for any session, over its whole history,
with no recorder uptime to depend on: the transcript IS the record.

The log kept here is only for the two facts the transcript provably cannot
carry: a stop that was a dialog wait (the assistant line is buffered until
the dialog resolves — see the attention system), and whether each push
buzzed or was suppressed (a runtime decision). notify_watch appends one
line per detected event, pushed or not; `timeline()` overlays them onto
the derived events.

One global append-only JSONL, pruned by age on the first write of each
process. Recording failure never blocks the notify path.
"""

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from . import hostenv

_FILE = hostenv.state_dir() / "jremote_events.jsonl"
RETAIN_DAYS = 30

# A transcript written to this recently is a session still working — its
# newest narration is "happening now", not a finished turn. Matches the
# board's own live threshold.
LIVE_S = 90.0

_lock = threading.Lock()
_pruned = False


def record(session_id: str, agent_id: str, kind: str,
           title: str, body: str, pushed: bool) -> None:
    """Append one event. `kind` is done | waiting | error | progress;
    `pushed` is whether an APNs send was actually dispatched."""
    # Microseconds, the host's universal stamp shape — the app parses and
    # lexicographically compares these against every other stamp it holds.
    line = {"ts": datetime.now().isoformat(timespec="microseconds"),
            "session_id": session_id, "agent_id": agent_id, "kind": kind,
            "title": title, "body": body, "pushed": bool(pushed)}
    with _lock:
        try:
            _prune_once()
            _FILE.parent.mkdir(parents=True, exist_ok=True)
            with _FILE.open("a") as f:
                f.write(json.dumps(line) + "\n")
        except OSError:
            pass  # a full disk must not take the board watcher with it


def since(ts: str = "") -> list[dict]:
    """Events strictly newer than `ts` (ISO, lexicographic), oldest first;
    everything retained when `ts` is empty."""
    try:
        lines = _FILE.read_text().splitlines()
    except OSError:
        return []
    out = []
    for raw in lines:
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if ev.get("ts", "") > ts:
            out.append(ev)
    return out


def _prune_once() -> None:
    """Drop entries past RETAIN_DAYS — once per process, on the first write,
    so the file stays bounded without anyone owning a sweep."""
    global _pruned
    if _pruned:
        return
    _pruned = True
    cutoff = datetime.fromtimestamp(
        time.time() - RETAIN_DAYS * 86400).isoformat(timespec="microseconds")
    kept = [ev for ev in since("") if ev.get("ts", "") >= cutoff]
    if len(kept) < len(since("")):
        _FILE.write_text("".join(json.dumps(ev) + "\n" for ev in kept))


# ── The derived timeline ──

def _to_local(ts_utc: str) -> str:
    """Transcript stamps are UTC-Z; everything the app holds and compares is
    naive local ISO — convert on the way out so one sort key rules."""
    try:
        dt = datetime.fromisoformat(ts_utc.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return dt.astimezone().replace(tzinfo=None).isoformat(timespec="milliseconds")


def _secs(ts_local: str) -> float:
    try:
        return datetime.fromisoformat(ts_local).timestamp()
    except ValueError:
        return 0.0


def timeline(session_id: str) -> list[dict]:
    """The session's notification timeline, derived from its transcript.

    A pure function of the source, selected at the push layer's own cadence
    so it reads as "what you'd have been told": each turn that ran at least
    MIN_WORKING ends in a `done` event carrying its reply; a turn older than
    PROGRESS_MIN_TURN contributes its interstitial narration, at most one
    line per PROGRESS_COOLDOWN. A transcript still being written treats its
    newest narration as `progress` — the turn isn't over.

    The event log then overlays the two facts the transcript cannot carry:
    `waiting` stops insert as their own entries, and pushed/error flags land
    on their body-matching derived twin. Log entries whose fact the
    transcript already tells are dropped, never duplicated.
    """
    from . import notify_watch
    from .messages import _find_session_file, parse_session

    msgs = parse_session(session_id).get("messages", [])
    # Turns split on real user prompts (parse_session has already dropped
    # tool_result carriers and machine-injected noise).
    turns: list[dict] = []
    for m in msgs:
        ts = _to_local(m.get("timestamp", ""))
        if not ts:
            continue
        if m["role"] == "user" and m["text"]:
            turns.append({"start": ts, "assistant": []})
        elif m["role"] == "assistant" and m["text"]:
            if not turns:  # a transcript that opens mid-work still has a turn
                turns.append({"start": ts, "assistant": []})
            turns[-1]["assistant"].append((ts, m["text"]))

    path = _find_session_file(session_id)
    writing = bool(path) and (time.time() - path.stat().st_mtime) < LIVE_S

    out: list[dict] = []

    def emit(ts: str, kind: str, body: str) -> None:
        out.append({"ts": ts, "session_id": session_id, "agent_id": "",
                    "kind": kind, "title": "", "body": body[:140],
                    "pushed": False})

    for i, turn in enumerate(turns):
        lines = turn["assistant"]
        if not lines:
            continue
        start = _secs(turn["start"])
        relayed = 0.0
        for ts, text in lines[:-1]:
            t = _secs(ts)
            if t - start < notify_watch.PROGRESS_MIN_TURN:
                continue
            if t - relayed < notify_watch.PROGRESS_COOLDOWN:
                continue
            relayed = t
            emit(ts, "progress", text)
        final_ts, final_text = lines[-1]
        if writing and i == len(turns) - 1:
            emit(final_ts, "progress", final_text)   # happening now, not done
        elif _secs(final_ts) - start >= notify_watch.MIN_WORKING:
            emit(final_ts, "done", final_text)
        # else: a quick conversational turn — no event, same as the pushes

    _overlay(session_id, out)
    out.extend(_trigger_fires(session_id))
    out.sort(key=lambda e: e["ts"])
    return out


def _trigger_fires(session_id: str) -> list[dict]:
    """Every environment trigger that fired on this session, as a `trigger` entry.

    The fire log is where the session's environment acted — compact typed, text
    injected, a tool blocked — and none of that is in the transcript as itself. The
    app shows an entry's body, not its title, so the trigger's name leads the body
    and the outcome closes it: a fire still `pending` is one whose delivery never
    reported back.
    """
    from . import triggers

    out = []
    for f in triggers.fires(session_id):
        if not f.get("fired"):
            continue
        what = f.get("text") or f.get("keys") or f.get("action") or ""
        body = f"{f.get('trigger')} · {what}"
        if f.get("outcome"):
            body += f" → {f['outcome']}"
        ts = f.get("ts") or ""
        out.append({"ts": ts if "." in ts else f"{ts}.000", "session_id": session_id,
                    "agent_id": "", "kind": "trigger", "title": f.get("trigger") or "",
                    "body": body[:140], "pushed": False})
    return out


def _overlay(session_id: str, out: list[dict]) -> None:
    """Land the log's transcript-invisible facts on the derived timeline."""
    claimed: set[int] = set()
    for ev in since(""):
        if ev.get("session_id") != session_id:
            continue
        if ev.get("kind") == "waiting":
            out.append(ev)
            continue
        key = (ev.get("body") or "")[:100]
        hit = next((j for j, d in enumerate(out)
                    if j not in claimed and d["body"][:100] == key), None)
        if hit is None:
            continue  # the transcript already tells this fact its own way
        claimed.add(hit)
        out[hit]["pushed"] = bool(ev.get("pushed"))
        if ev.get("kind") == "error":
            out[hit]["kind"] = "error"
