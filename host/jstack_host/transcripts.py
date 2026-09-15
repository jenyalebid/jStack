"""Reading a Claude transcript — the part of the host API that is pure parsing.

Nothing here knows about the machine. A session JSONL has the same shape on every
machine, a project dir encodes its cwd the same way, and what a session weighs
is arithmetic — so this is the largest body of jRemote code that a standalone
host needs verbatim, and it lived in the dashboard only because that is where
it was first written.

Two things it is NOT allowed to know, both of which used to sit inside it:

- **Which agent owns a project dir when the transcript cannot say.** A handful
  of pre-marker sessions are explained only by this Mac's own history. That
  table is a host fact and comes from `hostenv`.
- **Where to keep its cache.** The parsed-summary snapshot used to live under
  the dashboard's own tree. It is `~/.cache/jremote/` now, overridable with
  `JREMOTE_CACHE_DIR` — a cache the host owns, not one the dashboard lends it.

The cache is what makes a cold dashboard usable: without it every restart
re-parses ~2400 transcripts (~3.5s of pure `json.loads`) before the first
agent grid renders.
"""

import atexit
import json
import os
import re
import threading
import time as _time
from pathlib import Path

from . import compaction
from .hostenv import project_dir_agent_overrides


# ── Session cache ──
# Mtime-based: parse each JSONL once, reparse only when the file changes.
# Key: file path string. Value: (mtime_ns, size, parsed_dict).
# Persisted across restarts; per-entry validation stays lazy (mtime + size).

_session_cache: dict[str, tuple[int, int, dict]] = {}
_cache_lock = threading.Lock()

_CACHE_DIR = Path(os.environ.get("JREMOTE_CACHE_DIR")
                  or Path.home() / ".cache" / "jremote").expanduser()
_CACHE_FILE = _CACHE_DIR / "session_summaries.json"
_cache_dirty = False

#: Bump whenever `_parse_session_jsonl` changes the *shape* of what it returns.
#: The per-entry validation below is mtime/size only, which answers "has this
#: file changed", never "was this parsed by the code now asking". A new summary
#: field is therefore invisible for every unchanged transcript — permanently,
#: since the file never changes again — and callers indexing it get a KeyError
#: from a cache hit while a fresh parse works fine. Dropping the whole snapshot
#: on a version mismatch costs one re-parse (~3.5s, once) and removes the class.
#: Bumped for `machine_typed_prompt`: same shape, different values, and a
#: transcript that will never be written to again would otherwise serve its
#: cached `/compact` forever.
_CACHE_SCHEMA = 3


def machine_typed_prompt(text: str) -> bool:
    """Was this `last-prompt` typed by the machine rather than by a person?

    `last-prompt` records what was submitted at the prompt, whoever submitted
    it — and `/compact` is increasingly not a person. Claude Code auto-compacts
    on its own, and on this Mac a Stop hook (`compact_on_delivery.py`) types it
    outright at the end of a heavy turn, so the newest "prompt" on exactly the
    busiest sessions is a command the session ran on itself. Two costs, both
    live: a heavy card's last exchange reads `/compact` instead of the last
    thing the user actually said, and the field doubles as `notify_watch`'s
    interaction stamp — a stamp that moves means "the user typed something", which
    clears an unread dot nobody ever looked at.

    Skipping the line rather than blanking the field is the point: last-wins
    over the lines that remain leaves the field on the last prompt a person
    typed, which is what a card is for.

    Instructions included (`/compact keep the API details`): still the harness
    compacting, not a turn of conversation. A closed list of one, for the same
    reason `board._local_bookkeeping` keeps its list closed — a custom slash
    command (`/push`, `/report`) IS the user talking, and guessing wide empties the
    field on the sessions that need it most."""
    head = (text or "").strip().split(None, 1)
    return bool(head) and head[0] == "/compact"


def _load_session_cache_from_disk() -> None:
    """Populate _session_cache from the last persisted snapshot (best-effort).

    Prunes entries whose file no longer exists. Per-file mtime/size validation
    still happens lazily in get_session_summary, so a stale entry for a changed
    file is harmless — it just gets re-parsed on next access.

    A snapshot written by a different summary shape is discarded whole: see
    `_CACHE_SCHEMA`.
    """
    try:
        raw = json.loads(_CACHE_FILE.read_text())
    except Exception:
        return
    if not isinstance(raw, dict) or raw.get("schema") != _CACHE_SCHEMA:
        return  # older/foreign shape (or the pre-versioning flat file) — re-parse
    loaded = {}
    for key, val in (raw.get("entries") or {}).items():
        try:
            if not isinstance(val, list) or len(val) != 3:
                continue
            if not os.path.exists(key):
                continue  # drop summaries for deleted sessions
            loaded[key] = (int(val[0]), int(val[1]), val[2])
        except Exception:
            continue
    with _cache_lock:
        _session_cache.update(loaded)


def _flush_session_cache_to_disk() -> None:
    """Write the cache to disk atomically if it changed since the last flush."""
    global _cache_dirty
    with _cache_lock:
        if not _cache_dirty:
            return
        snapshot = {"schema": _CACHE_SCHEMA,
                    "entries": {k: [v[0], v[1], v[2]]
                                for k, v in _session_cache.items()}}
        _cache_dirty = False
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snapshot))
        tmp.replace(_CACHE_FILE)
    except Exception:
        # On failure, re-mark dirty so the next flush retries.
        with _cache_lock:
            _cache_dirty = True


def _start_cache_flusher() -> None:
    """Background thread: persist the session cache every 30s when dirty."""
    def _loop():
        while True:
            _time.sleep(30)
            _flush_session_cache_to_disk()
    t = threading.Thread(target=_loop, name="session-cache-flusher", daemon=True)
    t.start()


_load_session_cache_from_disk()
atexit.register(_flush_session_cache_to_disk)
_start_cache_flusher()


def _parse_session_jsonl(filepath: Path) -> dict:
    """Parse a session JSONL file and extract summary data.

    For large files (>2MB), reads first 200 + last 200 lines instead of the full file
    to avoid blocking on 30MB+ sessions. Token totals will be approximate but
    page load won't take seconds.
    """
    first_msg = ""
    last_msg = ""
    in_tok = 0
    out_tok = 0
    last_in_tok = 0
    # The session's fixed overhead, kept for the compaction case: a boundary
    # reports only the conversation it kept, and the rest of a request has to
    # be added back from what this session's own opening turn cost.
    first_in_tok = 0
    calls = 0
    # One API turn is written as one JSONL line per content block, each
    # repeating the same message.id and the same usage block. Bill each turn
    # once or a tool-heavy session reads ~2x its real cost.
    seen_msg_ids: set[str] = set()
    has_queue_op = False
    has_telegram_meta = False
    agent_id_detected = None
    first_real_user_msg = ""
    entrypoint = ""
    slug = ""
    ai_title = ""
    custom_title = ""
    last_prompt = ""

    size = filepath.stat().st_size
    if size > 2_000_000:
        # Large file: read head + tail to avoid full parse
        with open(filepath, "r", errors="replace") as fh:
            head_lines = []
            for i, line in enumerate(fh):
                head_lines.append(line)
                if i >= 199:
                    break
            # Read tail by seeking near end
            tail_lines = []
            try:
                fh.seek(max(0, size - 500_000))
                fh.readline()  # skip partial line
                tail_lines = fh.readlines()[-200:]
            except Exception:
                pass
        lines = head_lines + tail_lines
    else:
        lines = filepath.read_text(errors="replace").splitlines()

    for line in lines:
        try:
            e = json.loads(line)
            etype = e.get("type", "")
            if etype == "queue-operation":
                has_queue_op = True
            if etype == "ai-title":
                ai_title = e.get("aiTitle", "")
            if etype == "custom-title":
                custom_title = e.get("customTitle", "")
            if etype == "last-prompt":
                prompt = e.get("lastPrompt", "")
                if not machine_typed_prompt(prompt):
                    last_prompt = prompt
            if not entrypoint and e.get("entrypoint"):
                entrypoint = e["entrypoint"]
            if e.get("slug"):
                slug = e["slug"]
            if etype == "user":
                content = e.get("message", {}).get("content", "")
                text = ""
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block["text"].strip()
                            break
                elif isinstance(content, str):
                    text = content.strip()
                if not has_telegram_meta and "Conversation info" in text and "sender_id" in text:
                    has_telegram_meta = True
                if not agent_id_detected:
                    agent_id_detected = _extract_agent_from_system(
                        text, project_dir=str(filepath.parent.name)
                    )
                if text and not text.startswith("<") and not first_msg:
                    first_msg = text
                if text and not first_real_user_msg:
                    if text.startswith("<system>") and "</system>" in text:
                        trigger = text.split("</system>", 1)[1].strip()
                        if trigger:
                            first_real_user_msg = trigger
                    elif not text.startswith("<system>"):
                        first_real_user_msg = text
            # A compaction moves the reading without writing a turn: the
            # newest API call on file describes the weight it just dropped.
            boundary = compaction.boundary_tokens(e)
            if boundary:
                pre, post = boundary
                last_in_tok = compaction.reading_after(post, first_in_tok, pre)
            if etype == "assistant":
                msg = e.get("message", {})
                u = msg.get("usage", {})
                it = compaction.request_tokens(u)
                # last_in_tok is a context-size reading, not a sum — safe to
                # take from any block of the turn.
                if it > compaction.MIN_READING:
                    last_in_tok = it
                    if not first_in_tok:
                        first_in_tok = it
                mid = msg.get("id")
                if not mid or mid not in seen_msg_ids:
                    if mid:
                        seen_msg_ids.add(mid)
                    in_tok += it
                    out_tok += u.get("output_tokens", 0)
                    calls += 1
                content = e.get("message", {}).get("content", "")
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text" and c["text"].strip():
                            last_msg = c["text"].strip()
                elif isinstance(content, str) and content.strip():
                    last_msg = content.strip()
        except Exception:
            pass

    return {
        "first_msg": first_msg[:400] if first_msg else "",
        "last_msg": last_msg[:400] if last_msg else "",
        "in_tokens": in_tok,
        "out_tokens": out_tok,
        "total_tokens": in_tok + out_tok,
        "last_context": last_in_tok,
        # The session's own overhead — what one request costs with nothing
        # accumulated. Already measured for the compaction estimate; exported
        # because it is also the floor a compact drops back to, and the two
        # must be the same number.
        "first_context": first_in_tok,
        "calls": calls,
        "has_queue_op": has_queue_op,
        "has_telegram_meta": has_telegram_meta,
        "agent_id": agent_id_detected,
        "first_real_user_msg": first_real_user_msg,
        "entrypoint": entrypoint,
        "slug": slug,
        "ai_title": ai_title,
        "custom_title": custom_title,
        "last_prompt": last_prompt,
    }


def get_session_summary(filepath: Path) -> dict:
    """Get parsed session summary, using cache when file hasn't changed."""
    key = str(filepath)
    try:
        st = filepath.stat()
        mtime_ns = st.st_mtime_ns
        size = st.st_size
    except Exception:
        return {}

    with _cache_lock:
        cached = _session_cache.get(key)
        if cached and cached[0] == mtime_ns and cached[1] == size:
            return cached[2]

    # Parse outside lock to avoid blocking other threads
    parsed = _parse_session_jsonl(filepath)

    global _cache_dirty
    with _cache_lock:
        _session_cache[key] = (mtime_ns, size, parsed)
        _cache_dirty = True

    return parsed


def _extract_agent_from_system(text: str, project_dir: str = "") -> str | None:
    """Which agent this transcript belongs to — its own marker, or the host's.

    The marker in the system prompt is the general answer and works anywhere.
    The override table is consulted first because it exists precisely for the
    sessions that have no marker; it is empty on a host with no history to
    explain, which leaves the marker as the only path.
    """
    overrides = project_dir_agent_overrides()
    if project_dir and project_dir in overrides:
        return overrides[project_dir]
    m = re.search(r"workspace[/-](\w+)", text)
    if m:
        return m.group(1)
    return None


def _project_dir_to_cwd(dirname: str) -> str | None:
    """Convert a Claude project dir name back to a filesystem path.

    Claude Code encodes: / → -, dot-prefixed dirs lose the dot (leaving --).
    e.g. '-Users-x' → '/Users/x'
         '-Users-x-Agents-Ada-pm' → '/Users/x/Agents/Ada/pm'

    Strategy: recursive descent — try all possible splits at each level and
    check the filesystem to find which interpretation is correct.
    """
    raw = dirname.lstrip("-")
    if not raw:
        return None

    # Handle double-dash (dot-prefix) by splitting on -- first, then resolving each segment
    # e.g. "Users-x--claude-rules" → ["Users-x", ".claude-rules"]
    double_segments = raw.split("--")
    resolved_segments = [double_segments[0]]
    for seg in double_segments[1:]:
        resolved_segments.append("." + seg)

    # Now resolve each segment (which may contain hyphens that are either / or literal -)
    def _resolve(prefix: str, remaining: str) -> str | None:
        if not remaining:
            return prefix if Path(prefix).exists() else None

        parts = remaining.split("-")
        # Try increasingly longer combinations of parts as a single directory name
        for split_at in range(1, len(parts) + 1):
            candidate_name = "-".join(parts[:split_at])
            candidate_path = prefix + "/" + candidate_name
            rest = "-".join(parts[split_at:])

            if Path(candidate_path).exists():
                if not rest:
                    return candidate_path
                result = _resolve(candidate_path, rest)
                if result:
                    return result

        return None

    # Chain resolution across double-dash segments
    path = ""
    for seg in resolved_segments:
        result = _resolve(path, seg)
        if result:
            path = result
        else:
            # Fallback: just append with simple replacement
            path = path + "/" + seg.replace("-", "/")

    return path if Path(path).exists() else None


def _find_session_cwd(session_id: str) -> str | None:
    """Find which project dir contains a session and return its cwd path."""
    from .messages import _find_session_file
    from .codex_transcript import metadata
    path = _find_session_file(session_id)
    if path:
        cwd = metadata(path).get("cwd")
        if cwd and Path(cwd).is_dir():
            return cwd
    claude_projects = Path.home() / ".claude" / "projects"
    if not claude_projects.exists():
        return None
    for project_dir in claude_projects.iterdir():
        if not project_dir.is_dir():
            continue
        # Check both .jsonl session files and subdirectories
        if (project_dir / f"{session_id}.jsonl").exists():
            cwd = _project_dir_to_cwd(project_dir.name)
            if cwd and Path(cwd).exists():
                return cwd
    return None
