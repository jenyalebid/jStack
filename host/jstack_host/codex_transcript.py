"""Codex rollout adapter for jRemote's engine-neutral transcript surfaces."""

import json
import math
import subprocess
from functools import lru_cache
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# Codex wraps an attachment in markup that it splits across content blocks: the
# open tag, the image block, the close tag, and only then what the user typed. Each
# half is machine-written and goes — but it goes BEFORE the noise test and after
# the blocks are joined, because a prompt that opens with a screenshot is still
# The user talking, and a lone '<' or '</image>' left at the front would read as
# injection and take their words down with the tag.
_ATTACHMENT = re.compile(r"<image\b[^>]*>|</image>")

# Machine-injected openings — a whole user message starting with one is dropped.
_NOISE_PREFIXES = ("<", "Caveat:", "# AGENTS.md instructions for ")


def strip_attachments(text: str) -> str:
    """A prompt's own words, with Codex's inlined attachment markup removed."""
    return _ATTACHMENT.sub("", text or "").strip()


def root() -> Path:
    return Path.home() / ".codex" / "sessions"


def session_id(path: Path) -> str:
    meta = metadata(path)
    return str(meta.get("session_id") or meta.get("id") or "")


def metadata(path: Path) -> dict:
    try:
        with path.open() as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") == "session_meta":
                    return entry.get("payload") or {}
    except OSError:
        pass
    return {}


def path_for_id(sid: str) -> Path | None:
    base = root()
    if not base.exists():
        return None
    matches = list(base.glob(f"**/*-{sid}.jsonl"))
    return matches[-1] if matches else None


def rollout_started_after(started_at: float, cwd: str = "") -> Path | None:
    """The rollout Codex created for a launch at ``started_at``.

    Codex does not hold its JSONL open: it opens, appends, and closes on each
    write, so lsof cannot bind a live process to the file.  The session_meta
    timestamp and cwd are facts Codex itself writes before the first turn.
    Pick the earliest matching rollout born after this launch; an older pane
    in the same cwd is therefore ineligible, and simultaneous launches each
    claim the file immediately following their own start.
    """
    candidates = []
    for path in root().glob("**/rollout-*.jsonl"):
        meta = metadata(path)
        if cwd and meta.get("cwd") != cwd:
            continue
        raw = str(meta.get("timestamp") or "")
        try:
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            continue
        if stamp >= started_at:
            candidates.append((stamp, path))
    return min(candidates, default=(0, None), key=lambda item: item[0])[1]


def bind_open_session(board_sid: str, started_at: float, cwd: str = "",
                      attempts: int = 80) -> None:
    """Record the rollout created by this managed Codex launch."""
    def run() -> None:
        from . import managed
        for _ in range(attempts):
            path = rollout_started_after(started_at, cwd)
            if path:
                managed.record_transcript(board_sid, str(path))
                return
            time.sleep(.25)
    threading.Thread(target=run, name=f"codex-rollout-{board_sid[:8]}",
                     daemon=True).start()


def message_entries(path: Path) -> list[dict]:
    """Normalize Codex response items to jRemote's message shape."""
    out = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return out
    for raw in lines:
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "response_item":
            continue
        payload = entry.get("payload") or {}
        ptype = payload.get("type")
        if ptype == "message" and payload.get("role") in ("user", "assistant"):
            segments = []
            for block in payload.get("content") or []:
                if not isinstance(block, dict):
                    continue
                text = block.get("text")
                if block.get("type") in ("input_text", "output_text") and text:
                    segments.append({"type": "text", "text": text})
            if payload["role"] == "user":
                typed = strip_attachments(
                    "\n".join(s["text"] for s in segments))
                segments = [{"type": "text", "text": typed}] if typed else []
            if segments:
                text = "\n".join(s["text"] for s in segments)
                if payload["role"] == "user" and text.startswith(_NOISE_PREFIXES):
                    continue
                out.append({"role": payload["role"], "segments": segments,
                            "text": text[:4000],
                            "timestamp": entry.get("timestamp", "")})
        elif ptype in ("custom_tool_call", "function_call"):
            name = payload.get("name") or "tool"
            inp = payload.get("input") or payload.get("arguments") or ""
            summary = str(inp).replace("\n", " ")[:90]
            out.append({"role": "assistant",
                        "segments": [{"type": "tool", "name": name,
                                      "summary": summary}],
                        "text": "", "timestamp": entry.get("timestamp", "")})
    return out


def recover_open_sessions(reg: dict) -> dict:
    """Recover links lost on reattach or when initial startup outlasted polling.

    A pane's creation time bounds its launch. Never claim another registered
    transcript, or a rollout born after the next pane in the same workspace.
    Ambiguous candidates stay unbound instead of showing another chat's text.
    """
    missing = {sid for sid, info in reg.items()
               if info.get("engine") == "codex" and not info.get("transcript")}
    if not missing:
        return reg
    from . import managed
    try:
        result = subprocess.run(managed._t(
            "list-panes", "-a", "-F",
            "#{session_name}\t#{session_created}\t#{pane_current_path}"),
            capture_output=True, text=True, timeout=3)
        if result.returncode:
            return reg
        panes = {}
        names = {managed._name(sid): sid for sid in reg}
        for line in result.stdout.splitlines():
            name, stamp, cwd = line.split("\t", 2)
            if name in names:
                panes[names[name]] = (float(stamp), cwd)
    except (OSError, ValueError, subprocess.SubprocessError):
        return reg
    claimed = {info.get("transcript") for info in reg.values()}
    candidates = []
    for path in root().glob("**/rollout-*.jsonl"):
        if str(path) in claimed:
            continue
        meta = metadata(path)
        try:
            stamp = datetime.fromisoformat(meta["timestamp"].replace("Z", "+00:00")).timestamp()
        except (KeyError, TypeError, ValueError):
            continue
        candidates.append((stamp, meta.get("cwd"), path))
    for sid in missing:
        if sid not in panes:
            continue
        start, cwd = panes[sid]
        if sum(t == start and loc == cwd for t, loc in panes.values()) > 1:
            continue
        end = min((t for other, (t, loc) in panes.items()
                   if other != sid and loc == cwd and t > start), default=float("inf"))
        matches = [path for stamp, loc, path in candidates
                   if loc == cwd and start <= stamp < end and str(path) not in claimed]
        if len(matches) != 1:
            continue
        path = str(matches[0])
        managed.record_transcript(sid, path)
        reg[sid] = dict(reg[sid], transcript=path)
        claimed.add(path)
    return reg


def summary(path: Path) -> dict:
    """Session-card facts from the provider's own events, cached per file write."""
    try:
        st = path.stat()
        return dict(_summary(str(path), st.st_mtime_ns, st.st_size))
    except OSError:
        return {}


@lru_cache(maxsize=64)
def _summary(path: str, mtime_ns: int, size: int) -> dict:
    out = {"context": 0, "tokens": 0, "model": "", "turn": "", "rate_sample": None}
    with open(path) as fh:
        for line in fh:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            payload = event.get("payload") or {}
            typ = event.get("type")
            if typ == "turn_context" and payload.get("model"):
                out["model"] = payload["model"]
            elif typ == "event_msg":
                kind = payload.get("type")
                if kind == "thread_settings_applied":
                    out["model"] = (payload.get("thread_settings") or {}).get("model") or out["model"]
                elif kind == "task_started":
                    out["turn"] = "working"
                elif kind in ("task_complete", "task_completed", "turn_aborted"):
                    out["turn"] = "idle"
                elif kind == "token_count":
                    info = payload.get("info") or {}
                    last = info.get("last_token_usage") or {}
                    total = info.get("total_token_usage") or {}
                    out["context"] = int(last.get("input_tokens") or out["context"])
                    out["tokens"] = int(total.get("total_tokens") or out["tokens"])
                    limits = payload.get("rate_limits")
                    if isinstance(limits, dict) and limits.get("limit_id") in (None, "codex"):
                        windows = []
                        for wid in ("primary", "secondary"):
                            w = limits.get(wid)
                            if not isinstance(w, dict) or w.get("used_percent") is None:
                                continue
                            try:
                                pct = float(w["used_percent"])
                                mins = int(w.get("window_minutes") or 0)
                                reset = w.get("resets_at")
                                reset = datetime.fromtimestamp(float(reset), tz=timezone.utc).isoformat() if reset else None
                            except (ValueError, TypeError, OverflowError, OSError):
                                continue
                            if not math.isfinite(pct) or not 0 <= pct <= 100:
                                continue
                            label = "Week" if mins == 10080 else "Session (5h)" if mins == 300 else f"{mins // 60}h" if mins and mins % 60 == 0 else f"{mins}m" if mins else wid.capitalize()
                            windows.append({"id": wid, "label": label, "pct": pct, "resets_at": reset})
                        if windows:
                            try:
                                stamp = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).timestamp()
                            except (KeyError, TypeError, ValueError):
                                continue
                            sample = {"label": "Codex", "source": "rollout", "sampled_at": stamp,
                                      "windows": windows, "refusal": None}
                            if out["rate_sample"] is None or stamp >= out["rate_sample"]["sampled_at"]:
                                out["rate_sample"] = sample
    return out
