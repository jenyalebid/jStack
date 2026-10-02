"""Structured message parsing for jRemote.

Turns a Claude session's JSONL into messages whose content is an ordered list
of typed segments the app can render richly and narrate selectively:

  {"type": "text", "text": "..."}              # prose (inline markdown + urls)
  {"type": "code", "lang": "swift", "text": ...}
  {"type": "tool", "name": "Bash"}             # a compact chip; result body dropped

Design choices that keep the phone transcript clean:
  - `tool_use` becomes a low-weight chip (name only); `tool_result` bodies are
    dropped — they are the noise behind the old `[tool result]` bubbles.
  - System-injection noise (SessionStart hooks, `<command-*>` wrappers, cron and
    review markers) is filtered at the message level.

Top-level `text` (flattened prose) is kept for back-compat with the old history
shape and for callers that don't understand segments.
"""

import json
import re
from pathlib import Path

_SID_RE = re.compile(r"^[0-9a-f\-]{32,40}$")
_FENCE = re.compile(r"```([^\n`]*)\n?(.*?)```", re.DOTALL)

# A whole message is dropped when its flattened prose begins with one of these —
# these are machine-injected, never something the user typed or an agent said
# to them.
_NOISE_PREFIXES = ("<", "Caveat:", "# AGENTS.md instructions for ")

# A machine-spawned session's first message wraps its task in a routing marker.
# The marker is noise; the body is the reason the session exists — strip one,
# surface the other, everywhere the session presents itself.
_SPAWN_MARKER = re.compile(r"^\[(?:cron:|POST-SESSION-REVIEW)[^\]]*\]\s*")


def spawn_task(text: str) -> str:
    """The injected task behind a spawn marker; '' when text isn't one."""
    m = _SPAWN_MARKER.match(text or "")
    return text[m.end():].strip() if m else ""

_CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

# Input fields worth surfacing as a tool's one-line summary, in priority order.
_SUMMARY_KEYS = ("query", "command", "pattern", "file_path", "path", "url",
                 "description", "prompt", "skill", "subagent_type")


def tool_summary(name: str | None, inp) -> str:
    """A short human label for a tool call, e.g. the search query or file path."""
    if not isinstance(inp, dict):
        return ""
    for k in _SUMMARY_KEYS:
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().replace("\n", " ")[:90]
    return ""


def _split_prose(text: str) -> list[dict]:
    """Split a prose blob into ordered text/code segments by fenced code blocks."""
    segs: list[dict] = []
    pos = 0
    for m in _FENCE.finditer(text):
        pre = text[pos:m.start()]
        if pre.strip():
            segs.append({"type": "text", "text": pre.strip()})
        segs.append({
            "type": "code",
            "lang": (m.group(1) or "").strip(),
            "text": m.group(2).rstrip("\n"),
        })
        pos = m.end()
    tail = text[pos:]
    if tail.strip():
        segs.append({"type": "text", "text": tail.strip()})
    return segs


def _blocks_to_segments(content) -> list[dict]:
    """Flatten a message's content blocks into ordered typed segments."""
    segs: list[dict] = []
    if isinstance(content, str):
        return _split_prose(content)
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                segs += _split_prose(block)
            elif isinstance(block, dict):
                bt = block.get("type")
                if bt == "text":
                    segs += _split_prose(block.get("text", ""))
                elif bt in ("tool_use", "server_tool_use"):
                    segs.append({
                        "type": "tool",
                        "name": block.get("name", "?"),
                        "summary": tool_summary(block.get("name"), block.get("input")),
                    })
                # tool_result: intentionally dropped — raw tool output is noise.
    return segs


def _flatten(segs: list[dict]) -> str:
    """Prose-only flattening, for back-compat `text` and the noise filter."""
    return "\n".join(s["text"] for s in segs if s["type"] == "text").strip()


def _is_noise(role: str, prose: str) -> bool:
    return role == "user" and prose.startswith(_NOISE_PREFIXES)


def _find_session_file(session_id: str) -> Path | None:
    if _CLAUDE_PROJECTS.exists():
        for pd in _CLAUDE_PROJECTS.iterdir():
            cand = pd / f"{session_id}.jsonl"
            if cand.exists():
                return cand
    # A managed Codex id is a board handle, so its rollout path rides the
    # registry — but the registry holds a session only while it is open.
    try:
        from .managed import open_registry
        bound = (open_registry().get(session_id) or {}).get("transcript")
        if bound and Path(bound).exists():
            return Path(bound)
    except Exception:
        pass
    # The index is what still knows once the session ends: it is the same row
    # History drew the card from, so a card that exists is a thread that opens.
    try:
        from .store import get_store
        indexed = get_store().transcript_path(session_id)
        if indexed and Path(indexed).exists():
            return Path(indexed)
    except Exception:
        pass
    # A native Codex id (history) resolves from the rollout name.
    from .codex_transcript import path_for_id
    return path_for_id(session_id)


def _is_booting(session_id: str) -> bool:
    """A managed session that exists but has not written its transcript yet.

    The board is registered-first by design — `record_open` lands the sid
    before the pane execs `claude` — and the CLI then takes ~10s to flush its
    first line to disk. Measured on this Mac: pane up at 15:13:08, JSONL born
    15:13:20. For that window a live session has no transcript, and saying
    "not found" about it is false: the session is right there, running.
    """
    try:
        from .managed import is_open
        return is_open(session_id)
    except Exception:
        return False


def parse_session(session_id: str) -> dict:
    """Return `{messages: [{role, segments, text, timestamp}]}` for a session.

    `pending` instead of `error` while a live session is still booting. The
    distinction is the client's: an error is rendered to the user and latched,
    an empty conversation just fills in as the live tail delivers. A booting
    session is the second thing, and calling it the first left a permanent
    red "Session file not found" under a terminal that was working fine.
    """
    if not _SID_RE.match(session_id):
        return {"messages": [], "error": "Invalid session ID"}
    path = _find_session_file(session_id)
    if not path:
        if _is_booting(session_id):
            return {"messages": [], "pending": True}
        return {"messages": [], "error": "Session file not found"}

    try:
        st = path.stat()
        key = (str(path), st.st_size, st.st_mtime_ns)
    except OSError:
        key = None
    hit = _parsed.get(session_id)
    if key is not None and hit is not None and hit[0] == key:
        return hit[1]
    result = _parse_file(path)
    if key is not None and "error" not in result:
        _parsed.pop(session_id, None)
        _parsed[session_id] = (key, result)
        while len(_parsed) > _PARSED_MAX:
            _parsed.pop(next(iter(_parsed)))
    return result


# Parsed transcripts by sid, keyed on the file's (path, size, mtime). The board
# tick, the events view and the session view all ask for the same Codex threads
# every second or so; a 12k-line rollout is ~370ms of GIL-held parsing, which
# stalled every terminal riding this process's loop. Callers only read.
_parsed: "dict[str, tuple[tuple, dict]]" = {}
_PARSED_MAX = 32


def _parse_file(path: Path) -> dict:
    if path.name.startswith("rollout-"):
        from .codex_transcript import message_entries
        return {"messages": message_entries(path)}

    messages = []
    try:
        with path.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") not in ("user", "assistant"):
                    continue
                msg = entry.get("message", {})
                role = msg.get("role", entry["type"])
                segs = _blocks_to_segments(msg.get("content", ""))
                if not segs:
                    continue  # e.g. a pure tool_result message
                if (role == "user" and segs[0]["type"] == "text"
                        and spawn_task(segs[0]["text"])):
                    # The wake/review injection IS the session's task — show
                    # the instructions, shed the routing bracket.
                    segs[0]["text"] = spawn_task(segs[0]["text"])
                prose = _flatten(segs)
                if _is_noise(role, prose):
                    continue
                # Drop only truly empty messages (e.g. a bare tool_result, which
                # we already skip). Keep anything with prose, code, OR a tool call
                # — a tool call usually lives in a message with no text.
                if not prose and not any(s["type"] in ("code", "tool") for s in segs):
                    continue
                messages.append({
                    "role": role,
                    "segments": segs,
                    "text": prose[:4000],
                    "timestamp": entry.get("timestamp", ""),
                })
    except Exception as e:  # noqa: BLE001 — surface parse failure to the client
        return {"messages": [], "error": str(e)}

    return {"messages": messages}
