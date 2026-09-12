"""The board: enumerate agents and their Claude Code sessions.

Reuses existing machinery — the agent roster (`hostenv`, which is `lib.agents`
on this Mac and the filesystem on a standalone host), the cached
session-summary parser (`transcripts.get_session_summary`), and the live
process scan (`procscan.get_claude_processes`). No duplicated parsing.

Scope: sessions whose cwd is an agent workspace under `~/Agents/{Name}/`.
That is "any of our agents." Non-agent project dirs are skipped.
"""

import json
import os
from datetime import datetime
from pathlib import Path

from .hostenv import active_agents, submode_dirs, project_dir_to_agent, workspace
from .transcripts import get_session_summary
from . import hostenv

_CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

# jRemote is a direct mirror of each agent's `chat/` sub-mode — never the root
# (pipeline) workspace. When an agent has a chat sub-mode, the app talks to
# `{base}-chat`: sessions list filters to chat, and new sessions spawn with the
# chat dir as cwd (so the dashboard classifies them as chat, not pipeline).
_CHAT_MODE = "chat"


def _chat_scoped_id(base: str) -> str:
    """The agent id jRemote should use: `{base}-chat` if that sub-mode exists,
    else the base id (agents with no chat sub-mode fall back to root)."""
    return f"{base}-{_CHAT_MODE}" if _CHAT_MODE in submode_dirs(base) else base


_LIVE_WINDOW_SECS = 90

# How long a dangling tool_result may still mean "claude is composing the next
# block". Generous on purpose — a slow API round-trip on a large context, or a
# retry after an overload, can outrun the live window — but finite, because the
# alternative is a green dot that never goes out. See `_turn_sampled`.
_TOOL_RESULT_GRACE = 180

# How long the harness's turn marker may keep speaking for a transcript that
# has stopped moving. Covers the longest honest sampled gap — a model thinking
# between narration lines — while keeping a marker leaked by a killed turn from
# holding the dot for the 12 hours its own sweep allows. See `_turn_state`.
_TURN_MARKER_GAP = 600

# How far a transcript's birth may precede its process's start before the pair
# is impossible: `uptime_minutes` is rounded to a tenth of a minute and clocks
# drift, but a session's own transcript can never predate the session.
_START_SLACK = 120

# What Claude Code writes into the transcript for a LOCAL slash command — one
# it runs itself (`/compact`, `/login`), with no prompt to the model. All four
# land as `user` lines, all after the command has already finished, and none of
# them asks the model for anything. See `_local_bookkeeping`.
_LOCAL_COMMAND_TAGS = ("<local-command-caveat>", "<local-command-stdout>",
                       "<command-name>", "<command-message>")


def _local_bookkeeping(entry: dict, content) -> bool:
    """Is this `user` line the harness talking to itself rather than a prompt?

    Two kinds, and both are the tail a session is left sitting on when a local
    command is the last thing that happened to it:

    * the four `_LOCAL_COMMAND_TAGS` — the caveat, the command echo and the
      command's own stdout;
    * a compaction's summary (`isCompactSummary`), which replaces the context
      rather than asking anything of it.

    Why this is load-bearing rather than cosmetic: the Stop hook
    (`compact_on_delivery.py`) types `/compact` at the end of a heavy turn, so
    the newest `user` line on the busiest sessions we run is `<local-command-
    stdout>`. Read as a prompt it means "claude owes a reply", and the dot sat
    green for the rest of the session's life — never orange, on exactly the
    deliveries the user most needs to see land. `/compact` submits no prompt, so no
    turn marker backs it either; the tail was the whole story.

    Deliberately a closed list of tags, not the `startswith('<')` that
    `notify_watch._local_command_reopen` can afford. `<task-notification>` and
    `<system-reminder>` arrive the same shape and DO re-invoke the model, and a
    typed absolute path is a bare string opening with `/`. Guessing wide here
    buys the opposite bug: a working session reading idle.

    A *custom* slash command (`/push`, `/distribute`) writes `<command-name>`
    too, and skipping it is right there as well — the harness follows it
    immediately with the expanded prompt as a list-content `user` line, which
    the walk reaches first because it is newer."""
    if entry.get("isCompactSummary"):
        return True
    return (isinstance(content, str)
            and content.lstrip().startswith(_LOCAL_COMMAND_TAGS))


def _birth(path) -> float:
    """When this transcript was created — the fact that ties it to the process
    that made it. Never mtime: post-session writers (the review engine, a
    later resume) touch old transcripts, so mtime says who wrote last, not
    who owns it."""
    st = path.stat()
    return getattr(st, "st_birthtime", st.st_mtime)


def _proc_start(pids) -> float:
    """When the oldest of these processes started — a session's existence
    stamp for as long as it has no transcript to be born from.

    Claude Code writes the JSONL on the **first message**, not at startup, so
    a session opened and left at an empty prompt has a running process and no
    transcript for as long as nobody types — minutes, on a session spawned
    from the phone and typed into later. For that whole window the process is
    the only thing that knows the session exists, and it is what the board
    dates the row by."""
    import psutil
    starts = []
    for pid in pids:
        try:
            starts.append(psutil.Process(int(pid)).create_time())
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError, TypeError):
            continue
    return min(starts) if starts else 0.0


def _headless_transcript(path) -> bool:
    """This transcript was written by a headless/SDK run, not a terminal.

    Claude Code stamps every user line with its `entrypoint`: `cli` for an
    interactive terminal, `sdk-cli` for the scheduler's `-p` runs. The first
    couple of lines are bookkeeping and carry none, so scan a few.

    Load-bearing for `correlate_raw`: a headless run states its sid in argv,
    so while it lives it is `owned` and never a candidate. A `sdk-cli`
    transcript in the candidate pool therefore means **that process is gone**
    — and pairing it with whatever else happens to be running in the same
    workspace is how a finished cron round got adopted by a live sibling and
    kept a card on the board for hours after it exited. Unreadable is not
    headless: a file we cannot look at stays eligible rather than being
    excluded on a guess."""
    try:
        with path.open() as f:
            for i, line in enumerate(f):
                if i > 8:
                    break
                try:
                    ep = json.loads(line).get("entrypoint")
                except (ValueError, AttributeError):
                    continue
                if ep:
                    return ep != "cli"
    except OSError:
        return False
    return False


def correlate_raw(procs, now) -> dict:
    """{sid: pid} for interactive `claude`s that carry no sid in argv.

    A raw `claude` never says which session it is, so identity is observed from
    the one thing the process and the transcript share: **a session's
    transcript is created by its own process, moments after that process
    starts.** Pairs are scored by |birth − start|, assigned 1:1 closest-first,
    and a transcript that predates its candidate is never claimed at all.

    The rule this replaces took the *newest* transcript born since the process
    started, which handed every DEAD session to a living sibling in the same
    workspace. Close a chat and the window still open beside it adopted the
    closed session's transcript: the closed session kept a working dot on the
    board, the live one vanished from it entirely, and the phone was wrong
    about both. Newest-wins asks "which transcript is most recent", which is a
    question about the workspace; the owner is a question about *this
    process*, and only the start time answers it."""
    owned = {p["session_id"] for p in procs if p.get("session_id")}
    pairs = []
    for p in procs:
        if p.get("session_id"):
            continue
        # Only a `claude` can own a Claude transcript. Codex carries no sid in
        # argv either, so it falls into this same sid-less pool — and with
        # nothing of its own to match, it was handed the nearest unowned JSONL
        # in the workspace: a finished cron round came back as a running,
        # nameless card, while the Codex that actually held the pane lost its
        # pane identity to the same bad pair and reported itself dead.
        if (p.get("engine") or "claude") != "claude":
            continue
        pd, _ = _resolve_dir(p.get("location") or "")
        if pd is None:
            continue
        started = now - float(p.get("uptime_minutes") or 0) * 60
        for f in pd.glob("*.jsonl"):
            if f.stem in owned:
                continue        # another process named it in argv — not ours
            st = None
            try:
                delta = _birth(f) - started
            except OSError:
                continue
            if delta < -_START_SLACK:
                continue        # born before this process existed
            if _headless_transcript(f):
                continue        # its writer named itself; absent = exited
            pairs.append((abs(delta), f.stem, p["pid"]))
    pairs.sort()
    out, took_sid, took_pid = {}, set(), set()
    for _, sid, pid in pairs:
        if sid in took_sid or pid in took_pid:
            continue
        took_sid.add(sid)
        took_pid.add(pid)
        out[sid] = pid
    return out


def _claude_procs() -> list[dict]:
    """The live claude process scan. A failure RAISES — it must never read as
    an empty Mac. Translating failure into [] here fabricated a board where
    nobody was working, and one such tick fired a done-push for every mid-turn
    session on the machine. Every consumer already handles the raise better
    than it handled the lie: board_watch skips the pass and keeps its last
    truth, reconcile guards itself and reaps nothing, routes surface the
    error to a caller that retries."""
    from .procscan import get_claude_processes
    return get_claude_processes().get("processes", [])


def _resolve_dir(loc: str) -> tuple[Path | None, str]:
    """cwd → (claude project dir or None, display label)."""
    home = str(Path.home())
    if loc.startswith("~"):
        loc = home + loc[1:]
    loc = loc.rstrip("/")
    if not loc:
        return None, ""
    pd = _CLAUDE_PROJECTS / loc.replace("/", "-").replace(".", "-")
    return (pd if pd.is_dir() else None), (Path(loc).name or loc)


def _window_truth() -> tuple[dict[str, str], set[str]]:
    """(pane tty → tmux session name, tmux sessions a client is displaying).

    Tolerates a tmux failure by reporting no panes — every session then falls
    back to its own tty, which is the raw-window rule.
    """
    from . import managed
    try:
        return managed.pane_ttys(), managed.attached_names()
    except Exception:
        return {}, set()


def _has_window(tty: str | None, panes: dict[str, str], attached: set[str]) -> bool:
    """Is a terminal window on the Mac displaying this process right now?

    Three observable cases, no argv guessing:
      no tty            → headless worker. Never a window.
      tty is a tmux pane → a window exists only while a client is attached to
                          that tmux session. The pane's tty outlives every
                          window close, so it alone proves nothing.
      any other tty      → a raw terminal window owns it. Closing that window
                          SIGHUPs the process, so its existence IS the window.
    """
    if not tty:
        return False
    name = panes.get(tty)
    if name is not None:
        return name in attached
    return True


def _pane_sids(panes: dict[str, str]) -> dict[str, str]:
    """tty → the managed session id whose pane owns that tty.

    A managed pane is named `jr-<sid[:8]>`, so the registry turns the pane
    name back into the full sid. This is a session's identity when its own
    process can't state one: Codex puts no sid in argv and writes no Claude
    transcript, so the pane is the only thing that knows which session that
    process is. Without it the process reads as anonymous and the board would
    carry two rows for one pane — the registry's, and a pid- row beside it."""
    from . import managed
    try:
        reg = managed.open_registry()
    except Exception:
        return {}
    by_prefix = {sid[:8]: sid for sid in reg}
    out = {}
    for tty, name in panes.items():
        if name.startswith("jr-"):
            sid = by_prefix.get(name[3:])
            if sid:
                out[tty] = sid
    return out


def _proc_scan(procs: list[dict] | None = None) -> tuple[dict[str, dict], list[dict]]:
    """One pass over the live claude processes — the session truth every
    consumer shares (board rows, session lists, agent live flags, PTY gates).

    Returns (sessions, orphans). `sessions` maps sid → {"window": a terminal
    window is displaying it right now, "tty": a terminal holds it at all,
    "engine": which CLI it runs, "name" / "model": the title and model the
    spawn pinned in its own argv, "pd": claude project dir or None, "label":
    folder display label}. `orphans` are running processes whose session can't
    be identified — {"pid", "pd", "label", "engine", "window", "tty"}.

    **Every agent-CLI process the scan sees becomes one of the two.** A
    process spending tokens is a session on this Mac, and identity is a
    convenience for showing it well, never the price of being shown at all.
    The old rule kept an unidentified process only `if window` — so a raw
    `claude` in a detached tmux, a headless worker whose argv carries no sid,
    and every Codex not spawned by jRemote (Codex never writes a Claude
    transcript to correlate against) ran on this machine while the board said
    the machine was quiet.

    `window` is observed from the controlling tty, not inferred from argv.
    The old rule (`source in ("cli","vscode")`) read "launched interactively",
    which inside tmux stays true forever: closing the iTerm window detaches the
    client but leaves the pane, its tty, and the claude running, so every window
    The user ever closed kept reporting itself open. See `_has_window`.

    Identity, in order: the sid in argv (resumed/headless runs); the managed
    pane the process sits in (`_pane_sids` — how a Codex session is named,
    since its argv says nothing and it has no Claude transcript); then
    `correlate_raw` for raw interactive `claude`s — the transcript born
    closest to that process's own start, assigned 1:1. Correlation never
    claims a JSONL another process already owns, and a process that resolves
    to no session surfaces as its own orphan rather than being merged into a
    session that isn't it."""
    import time as _time
    if procs is None:
        procs = _claude_procs()
    now = _time.time()
    panes, attached = _window_truth()
    pane_sids = _pane_sids(panes)
    sessions: dict[str, dict] = {}
    orphans: list[dict] = []

    def add(sid, window, headless, pd, label, pid=None, engine="", tty=None,
            name="", model=""):
        row = sessions.setdefault(
            sid, {"window": False, "headless": True, "pd": pd, "label": label,
                  "pids": [], "engine": "", "tty": False, "name": "",
                  "model": ""})
        row["window"] = row["window"] or window
        # Any process with a terminal makes the session non-headless.
        row["headless"] = row["headless"] and headless
        row["tty"] = row["tty"] or bool(tty)
        row["engine"] = row["engine"] or engine
        # `claude --name` — the spawn's own title, carried in its argv. First
        # non-empty wins: a session resumed by a second process states the
        # title once, and the resume that omits it must not blank it.
        #
        # This has to ride the *session* row and not just the orphan path,
        # because a headless spawn is identified (its sid is in its argv) and
        # so never becomes an orphan. Without this, argv was the one place a
        # title could live that `entry` never looked — and the open registry it
        # falls back to is gated on a live tmux, which a headless run has none
        # of. A named worker showed up anonymous.
        row["name"] = row["name"] or name
        # Same rule, same reason: `--model` is stated in argv and recorded
        # nowhere a later reader can reach.
        row["model"] = row["model"] or model
        if pid:
            row["pids"].append(pid)
        if row["pd"] is None:
            row["pd"], row["label"] = pd, label

    unnamed = []
    for p in procs:
        pd, label = _resolve_dir(p.get("location") or "")
        tty = p.get("tty")
        window = _has_window(tty, panes, attached)
        headless = not tty
        engine = p.get("engine") or ""
        if p.get("session_id"):
            add(p["session_id"], window, headless, pd, label, p.get("pid"),
                engine, tty, p.get("window_name") or "", p.get("model") or "")
        else:
            unnamed.append((p, pd, label, window, headless, engine, tty))

    by_pid = {pid: sid for sid, pid in correlate_raw(procs, now).items()}
    for p, pd, label, window, headless, engine, tty in unnamed:
        # A managed pane names its own process. This is the only identity a
        # Codex session has: no sid in argv, no Claude transcript to correlate
        # — without it every managed Codex would show up twice, once as its
        # registry row and once as an anonymous pid- row for the same pane.
        sid = by_pid.get(p.get("pid")) or pane_sids.get(tty or "")
        if sid:
            add(sid, window, headless, pd, label, p.get("pid"), engine, tty,
                p.get("window_name") or "", p.get("model") or "")
        else:
            orphans.append({"pid": p.get("pid"), "pd": pd, "label": label,
                            "name": p.get("window_name") or "",
                            "engine": engine, "window": window,
                            "tty": bool(tty)})
    return sessions, orphans


def _turn_state(path, pids) -> str:
    """'working', 'error', or 'idle' — the sampled tail verdict, with one
    override: an idle-reading tail while the harness's own turn clock says
    the turn is open is a sampled gap, not an end. Mid-turn the transcript
    trails its last text line for as long as the model thinks before its
    next tool call — a tick landing in that stretch read idle, flapped the
    board dot, and fired a done-push for every narration line of a long
    autonomous turn. UserPromptSubmit sets the marker, Stop clears it
    (attention_hook.py); error keeps outranking it — an API failure is a
    last word, however open the turn.

    The override is bounded by the gap it exists to bridge. Stop and
    SessionEnd are the only things that clear the marker, so a turn that DIED
    mid-flight — killed, crashed, interrupted before the harness could run a
    hook — leaves it set, and the only backstop behind it sweeps at 12 hours.
    Unbounded, that pins a finished session's dot green for the rest of the
    day: exactly the "still working" a done session must never show. A
    sampled gap is minutes of a model thinking, and a session really mid-turn
    keeps writing — hook and system lines included. So the marker may speak
    for a transcript that has moved recently, and not for one gone silent."""
    state = _turn_sampled(path, pids)
    if state == "idle" and _turn_marker_open(path) and _moved_recently(path):
        return "working"
    return state


# path -> the size last seen on a fresh-mtime pass. Idle Claude Code sessions
# touch their own transcript once an hour — same bytes, mtime bumped by exactly
# 3600s, observed live — so a fresh mtime alone is not "a turn is writing".
_moved_sizes: dict[str, int] = {}


def _moved_recently(path) -> bool:
    """Has this transcript been written to inside the sampled-gap window?

    Grown, not merely touched. The hourly zero-byte keep-alive lands inside the
    window like a real write would, and this is what lets a marker outlive its
    turn: a session interrupted mid-turn keeps its marker, and one touch an
    hour would re-pin its dot green for ten minutes at a time — the "still
    working" a dead turn must never show. A turn genuinely in flight writes
    tool calls and results continuously, so growth is what it looks like; a
    silent model think doesn't move the mtime either way and is already out."""
    import time as _time
    try:
        st = os.stat(path)
    except OSError:
        # Cannot look. Say so by declining to override: a marker outliving its
        # turn is the failure being bounded here, and an unreadable transcript
        # is no evidence that a turn is still running.
        return False
    if _time.time() - st.st_mtime >= _TURN_MARKER_GAP:
        _moved_sizes.pop(str(path), None)
        return False
    # First fresh pass has nothing to compare against — the transcript really
    # was just written, and the sceptical read is the one that would call a
    # live turn dead. Growth decides only once there is a previous size.
    seen = _moved_sizes.get(str(path))
    _moved_sizes[str(path)] = st.st_size
    return seen is None or st.st_size != seen


# Turn-clock markers — attention_hook.py's second directory: one file per
# session id, existing exactly while Claude Code owes a reply (set on
# UserPromptSubmit, cleared on Stop/SessionEnd).
_TURN_DIR = hostenv.state_dir() / "jremote_turn"
_ATTENTION_DIR = hostenv.state_dir() / "jremote_attention"


def _turn_marker_open(path) -> bool:
    """Does the harness say this session's turn is still open? Swept when
    older than any real turn — a crash between set and clear must not pin a
    later session of the same id to working. Also consulted for a managed row
    whose process the scan missed (a short scan must not read as idle); the
    bound on crash residue there is reconcile reaping the claude-less session,
    and the age sweep behind it."""
    import time as _time
    marker = _TURN_DIR / Path(path).stem
    try:
        if _time.time() - marker.stat().st_mtime > 12 * 3600:
            marker.unlink(missing_ok=True)
            return False
        return True
    except OSError:
        return False


def _turn_sampled(path, pids) -> str:
    """What an interactive session's transcript tail says it is doing —
    'working', 'error', or 'idle'. Long tool calls (an xcodebuild, a big
    test run) write nothing for minutes, and the working dot must not
    flicker off mid-build.

    Deliberately NOT judged here: waiting-on-a-dialog. The tail cannot carry
    it — the assistant line that opens a dialog is sometimes buffered until
    the dialog resolves, and a childless tool_use is just as often an MCP or
    in-process tool mid-run (neither forks a child). Dialogs are reported by
    Claude Code's own hook events instead (see `attention`).

    Read the transcript tail and judge the last *message* line:

      user, local bookkeeping  → not a message at all: a local command's echo
                                 and stdout, or a compaction summary. Skipped,
                                 and the walk continues past it
                                 (`_local_bookkeeping`).
      user, not an interrupt   → claude owes a reply: working. A typed prompt
                                 owes one for as long as it takes. A tool
                                 RESULT is also a user line, and it owes one
                                 within an API round-trip — so that half gets
                                 a clock (`_TOOL_RESULT_GRACE`). Without it a
                                 turn killed between the result and the next
                                 block pins the row working for the life of
                                 the session.
      assistant flagged
      isApiErrorMessage        → the session's last word is an API failure
                                 (limit hit, request error) — nothing will
                                 move until the user does: error.
      assistant ending in
      tool_use                 → a tool call with no result yet. Working ONLY
                                 if a child process younger than that line is
                                 running under the session's claude — a real
                                 tool executes as a child; a dialog waits
                                 with none. (MCP servers are children too,
                                 but they start with the session, so the age
                                 test excludes them.)
      assistant ending in
      thinking (or no blocks)  → mid-generation. The thinking block persists
                                 when the stream *starts*, and a completed turn
                                 always ends in text or tool_use — an alive
                                 session can only sit on this tail while it is
                                 still producing: working.
      assistant text           → the turn is complete: idle.

    Only ever called for a session whose process is alive, so a stale tail
    can't pin a dead session to working."""
    import json
    import time
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return "idle"
    if size > 65536:
        lines = lines[1:]  # first line is a partial
    for ln in reversed(lines):
        try:
            d = json.loads(ln)
        except (json.JSONDecodeError, ValueError):
            continue
        if d.get("type") not in ("user", "assistant"):
            continue
        content = (d.get("message") or {}).get("content")
        blocks = content if isinstance(content, list) else []
        if d["type"] == "user":
            if _local_bookkeeping(d, content):
                continue   # not a message — keep walking to the last real one
            if any(isinstance(b, dict) and b.get("type") == "tool_result"
                   for b in blocks):
                # Not a prompt — a tool handing its result back. claude owes
                # the next block within an API round-trip, so a result left
                # dangling is a turn that DIED here (killed mid-turn, a crash,
                # an interrupt the harness never got to record) and the row
                # must fall back to idle instead of claiming work forever.
                #
                # Counted against the line's OWN timestamp, not the file's
                # mtime: hook, attachment and system lines keep landing after
                # the last message and would keep a dead turn looking fresh.
                return ("working"
                        if time.time() - _line_epoch(d) < _TOOL_RESULT_GRACE
                        else "idle")
            text = " ".join(b.get("text", "") for b in blocks if isinstance(b, dict))
            if isinstance(content, str):
                text = content
            return "idle" if "[Request interrupted" in text else "working"
        if d.get("isApiErrorMessage"):
            return "error"
        last = blocks[-1] if blocks and isinstance(blocks[-1], dict) else {}
        if last.get("type") == "tool_use":
            return "working" if _child_younger_than(pids, _line_epoch(d)) else "idle"
        if last.get("type") == "thinking" or (
                isinstance(content, list) and not blocks):
            return "working"
        return "idle"
    return "idle"


def _turn_open(path, pids) -> bool:
    """Mid-turn right now — the working half of `_turn_state`'s verdict."""
    return _turn_state(path, pids) == "working"


# Dialog markers — written by Claude Code's own hook events (attention_hook.py
# wired in ~/.claude/settings.json), one file per session id. A marker exists
# exactly while Claude Code is showing a select dialog: set on the permission
# Notification and on PreToolUse of the dialog tools (AskUserQuestion,
# ExitPlanMode); cleared on any PostToolUse (the dialog resolved and the tool
# ran), UserPromptSubmit, Stop, SessionEnd. Nothing is inferred from screen
# content or transcript shape — the harness says a dialog is up, or it isn't.


def _dialog_sids() -> set[str]:
    """Session ids Claude Code reports as sitting on a dialog right now.
    Sweeps markers old enough that no live dialog can explain them — a crash
    between set and clear must not pin a future session of the same id red."""
    import time as _time
    try:
        entries = list(_ATTENTION_DIR.iterdir())
    except OSError:
        return set()
    out = set()
    now = _time.time()
    for f in entries:
        try:
            if now - f.stat().st_mtime > 6 * 3600:
                f.unlink(missing_ok=True)
                continue
        except OSError:
            continue
        out.add(f.name)
    return out


def awaiting_dialog() -> set[str]:
    """Session ids sitting on a dialog — the public read of the same markers the
    board's red dot uses.

    Public because it is load-bearing for anything outside the board that decides
    whether a quiet session is stuck. It cannot be inferred from the transcript: the
    assistant line that OPENS a dialog is sometimes buffered and appended only when the
    dialog is answered (a tool_use stamped 01:32:26 landed on disk at 01:34:37, at the
    answer), so a session waiting on a permission prompt is byte-for-byte
    indistinguishable from one that died, for the whole wait. A caller that guesses
    types into a prompt somebody is in the middle of answering.
    """
    return _dialog_sids()


def _line_epoch(d: dict) -> float:
    try:
        return datetime.fromisoformat(
            d.get("timestamp", "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _child_younger_than(pids, born_after: float) -> bool:
    """Any live descendant of `pids` created at/after `born_after` (2s slack —
    the tool child forks a beat before its tool_use line lands)."""
    import psutil
    for pid in pids:
        try:
            kids = psutil.Process(pid).children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        for k in kids:
            try:
                if k.create_time() >= born_after - 2:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    return False


def _is_live(sid: str, headless: bool, mtime: float, now: float, in_flight,
             path: str = "", pids=()) -> bool:
    """Producing output right now. A terminal session is live while the
    transcript is actually being written (or a jRemote turn is in flight), and
    stays live through a long quiet tool call (`_turn_open`); a headless
    worker has no idle state — it exists only while running.

    Keyed on headless (no controlling tty), NOT on whether a window displays
    it: a detached tmux session is an idle terminal session, not a worker, and
    keying this on `window` would pin every closed-window session to live."""
    if headless or bool(mtime and now - mtime < _LIVE_WINDOW_SECS) or sid in in_flight:
        return True
    return bool(path and pids) and _turn_open(path, pids)


def _live_session_ids() -> set[str]:
    """Session ids with a running Claude process (interactive or headless)."""
    return set(_proc_scan()[0])


def pids_holding(sid: str) -> list[int]:
    """PID(s) genuinely holding this session — never more, never fewer.

    Named procs (sid in argv: resumed/headless runs) match directly. A raw
    interactive claude carries no sid, so it's correlated by cwd — using the
    SAME 1:1 birth-time assignment as `_proc_scan` (and therefore as
    `_live_session_ids` and the PTY 4409 gate), so the close/kill path agrees
    with what the board shows live.

    The old rule correlated by mtime and only the single newest transcript,
    which disagreed with the scan two ways: it OVER-matched (every raw window
    in a shared workspace resolved to the same sid, so a Take-CLI close could
    SIGKILL sibling sessions) and UNDER-matched (a live raw session that wasn't
    the newest-modified transcript resolved to no pid at all — close cleared
    nothing while the scan still saw it live, so the PTY attach 4409'd and the
    session could never be taken)."""
    import time as _time
    from .procscan import get_claude_processes
    try:
        procs = get_claude_processes().get("processes", [])
    except Exception:
        return []
    # Direct holders: the sid is in argv. A session can be resumed by more than
    # one process, so collect all of them.
    direct = [p["pid"] for p in procs if p.get("session_id") == sid]
    if direct:
        return direct
    # Raw correlation: the same 1:1 assignment `_proc_scan` uses, from the same
    # function — the close path and the board must never disagree about who
    # owns a transcript, or a kill lands on a session the board wasn't showing.
    pid = correlate_raw(procs, _time.time()).get(sid)
    if pid:
        return [pid]
    # Pane identity — the same rung `_proc_scan` uses, and the only one a
    # Codex session has: nothing in its argv says the sid and it writes no
    # transcript to correlate. Without this the board could show a Codex
    # session working while every path that asks who holds it answered
    # "nobody", so a kill signalled nothing and reported success.
    panes, _ = _window_truth()
    ttys = {tty for tty, s in _pane_sids(panes).items() if s == sid}
    return [p["pid"] for p in procs if p.get("tty") in ttys and p.get("pid")]


def transcript_pristine(sid: str) -> bool:
    """Has anything ever been said in this session? True = provably nothing.

    The guard behind the app's open-then-back-out reap: a fresh phone-spawned
    session writes no transcript until its first message, so "no JSONL
    anywhere" is pristine. A transcript that does exist is pristine only if it
    holds no real conversation line — harness metadata (`last-prompt`, `mode`,
    …) and hook-injected context (`isMeta` user lines) are the machine talking
    to itself, not the user speaking, so they don't count. Any non-meta user or
    assistant line means the session carries work.

    Unprovable reads as NOT pristine: an unreadable transcript keeps its
    session. The only destructive direction here is killing something the user
    said something into, and this call is the one thing standing in the way —
    the phone's own "nothing was typed" judgment is stale the moment someone
    types into the session's Mac window."""
    import json
    # An injected briefing is content. A handoff/audit spawn carries its work
    # in `--append-system-prompt`, which never reaches the transcript — the
    # session reads blank from the JSONL while holding a full briefing. The
    # process argv is the only place that fact lives, so ask it: a live
    # holder spawned with a briefing keeps its session, typed-into or not.
    # Direct argv holders only, never birth-time correlation — a correlated
    # pairing can hand an unrelated raw process's briefing to this sid (and a
    # briefed RAW window is a `pid-` row, which the close path always keeps).
    if any(p.get("briefed") for p in _claude_procs()
           if p.get("session_id") == sid):
        return False
    if not _CLAUDE_PROJECTS.exists():
        return True
    for pd in _CLAUDE_PROJECTS.iterdir():
        path = pd / f"{sid}.jsonl"
        if not pd.is_dir() or not path.exists():
            continue
        try:
            with path.open() as f:
                for ln in f:
                    try:
                        obj = json.loads(ln)
                    except json.JSONDecodeError:
                        return False   # unparseable = unprovable = keep
                    if (obj.get("type") in ("user", "assistant")
                            and not obj.get("isMeta")):
                        return False
        except OSError:
            return False
        return True
    return True


def window_ttys(pids) -> list[str]:
    """The controlling ttys of these processes — the windows they live in.

    Read **before** they are signalled: a dead process has no tty to ask for,
    and the window it left behind would be unidentifiable. Feeds
    `managed.close_windows` so ending a session closes its window, the same
    event closing the window already performs in the other direction.

    Resolved through the same fresh `ps` map the board uses, never
    `psutil.terminal()`: that answer comes from a /dev map memoized for the
    life of the process, so in a long-running dashboard every window opened
    since startup resolves to nothing — and a close would quietly leave the
    window it was supposed to take with it."""
    from .procscan import _tty_map
    ttys = _tty_map()
    out = []
    for pid in pids:
        try:
            tty = ttys.get(int(pid))
        except (ValueError, TypeError):
            continue
        if tty:
            out.append(tty)
    return out


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def end_raw_holders(sid: str, sig, timeout: float = 10.0) -> bool:
    """Signal every process holding this session in a raw Mac window and wait
    for **those processes** to exit. True once none of them is left.

    The single "end the claude on the Mac" path, shared by the close endpoint
    and the takeover that moves a session onto the phone — the two must agree
    on which pids to signal and on when the transcript is free, because a
    takeover that starts `claude --resume` while the old one is still writing
    puts two writers on one file.

    Freedom is judged from the pids, never from `_live_session_ids()`. The scan
    answers a different question: it hands each *unowned* transcript to a live
    process in the same workspace, so the instant the real holder dies its
    transcript is re-assigned to a sibling `claude` in that directory and the
    session reads live forever. Waiting on that is a wait that never ends —
    the session gets killed, the caller is told the kill failed, and the work
    is gone with nothing to show for it. The processes we signalled own the
    fact; ask them."""
    import time as _time
    pids = pids_holding(sid)
    if not pids:
        return True  # nothing is holding the transcript
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        if not any(_alive(pid) for pid in pids):
            return True
        _time.sleep(0.2)
    return False


def mid_turn(sid: str) -> bool:
    """Is this session inside an unfinished turn — a prompt claude owes a reply
    to, or a tool call still running? The same judgment as the board's working
    dot (`_turn_open`), because that dot is what the caller was looking at when
    they decided to act on the session.

    The takeover reads this **before** signalling the holders: the open-tool_use
    branch asks the holding pids for a live tool child, and a dead process has
    none — judged after the kill, every mid-tool session would read idle."""
    pids = pids_holding(sid)
    if not pids or not _CLAUDE_PROJECTS.exists():
        return False
    for pd in _CLAUDE_PROJECTS.iterdir():
        f = pd / f"{sid}.jsonl"
        if pd.is_dir() and f.exists():
            return _turn_open(f, pids)
    return False


def _display_sub_mode(mode: str) -> str:
    """Match the conversations-tab chip form: default→root, nested→slashes."""
    if mode == "default":
        return "root"
    if mode.startswith("missions-"):
        return "missions/" + mode[len("missions-"):]
    if mode.startswith("chat-"):
        return "chat/" + mode[len("chat-"):]
    return mode


class _PDStat:
    """One project dir's reading: how many transcripts it holds, whether one is
    producing output right now, and when the newest was last written. The unit
    both consumers below are built from, so the projects tree and the process
    scan are each walked exactly once per call."""

    __slots__ = ("count", "live", "last")

    def __init__(self, count: int = 0, live: bool = False, last: float = 0.0):
        self.count, self.live, self.last = count, live, last


def _pd_stat(pd: Path, procs, now, in_flight) -> _PDStat:
    files = list(pd.glob("*.jsonl"))
    st = _PDStat(count=len(files))
    for f in files:
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        st.last = max(st.last, mtime)
        if not st.live:
            info = procs.get(f.stem)
            if info:
                st.live = _is_live(f.stem, info["headless"], mtime, now,
                                   in_flight, str(f), info.get("pids") or ())
    return st


class _SeatStats:
    """Per-agent counts, live-flags, and last-activity stamps for the agent's
    own chat seat. Counts come from cheap filename globs; live carries the
    same truth as the board rows — a session actually producing output right
    now.

    Every other seat is a card, and a card's numbers come from `carded_seats`
    reading `by_pd` directly. There is no second seat family here on purpose:
    a hardcoded one is exactly what the shortcuts table replaced."""

    def __init__(self):
        self.counts: dict[str, int] = {}
        self.has_live: dict[str, bool] = {}
        self.last_active: dict[str, float] = {}

    def add(self, base: str, st: _PDStat, counted: bool):
        if counted:
            self.counts[base] = self.counts.get(base, 0) + st.count
        self.last_active[base] = max(self.last_active.get(base, 0.0), st.last)
        if st.live:
            self.has_live[base] = True

    def stamp(self, base: str) -> str:
        t = self.last_active.get(base, 0.0)
        return datetime.fromtimestamp(t).isoformat() if t else ""


def _seat_stats() -> tuple[_SeatStats, dict[str, _PDStat]]:
    """One pass over the project dirs → (chat, by project dir).

    Chat covers the `chat` seat only — the agent's own conversations, which is
    the one seat the roster draws without a card.

    The second return is every agent project dir keyed by its own name, which
    is what an arbitrary seat is looked up by (see `carded_seats`). Keyed by
    the directory name and never by a re-parse of it: the encoding flattens `/`
    and `.` to the same `-`, so reading a seat back out of one is a guess."""
    import time as _time
    from .turns import _in_flight
    chat = _SeatStats()
    by_pd: dict[str, _PDStat] = {}
    if not _CLAUDE_PROJECTS.exists():
        return chat, by_pd
    procs, _ = _proc_scan()
    now = _time.time()
    for pd in _CLAUDE_PROJECTS.iterdir():
        if not pd.is_dir() or pd.name.startswith("."):
            continue
        parsed = project_dir_to_agent(pd.name)
        if not parsed:
            continue
        base, mode = parsed
        st = _pd_stat(pd, procs, now, _in_flight)
        by_pd[pd.name] = st
        if _display_sub_mode(mode) == _CHAT_MODE:
            chat.add(base, st, True)
    return chat, by_pd


def carded_seats(by_pd: dict[str, _PDStat] | None = None) -> list[dict]:
    """The seats that actually have a card, with the counts and pulse it draws.

    **Only the carded ones, deliberately.** The first cut of this shipped every
    seat on the host — 51 rows, 7.2 KB, 71% of `/agents` — on a fifteen-second
    poll, to describe a tree that changes when the user makes a directory. That is
    the whole seat list re-sent four times a minute per device forever, and the
    30s cache on the walk hid none of it: the walk was never the expensive part,
    the wire was. The host holds the `shortcuts` table, so it knows which seats
    are on someone's screen — typically a handful — and a handful is what ships.

    Structure isn't polled at all. The label and emoji are the device's own
    (authored in the picker, synced as user meta), and the picker browses live
    through `seats.browse()`, so nothing here needs a catalogue of what exists.
    What the host alone knows is the changing part — session count and pulse —
    and that is exactly what this returns.

    A card whose directory is gone comes back `missing`, counts zeroed: the
    delete semantic runs the other way (a card can be deleted without touching
    its folder), so the inverse has to render honestly rather than as a live
    card pointing at nothing.

    Stats are looked up by encoding each seat's path into its project dir name,
    and seat ids resolve through `seats.resolve()`. Only ever that direction:
    the encoding is lossy in reverse.
    """
    from . import seats as seatlib
    from .store import get_store

    wanted = [r["seat_id"] for r in get_store().shortcuts()]
    if not wanted:
        return []
    if by_pd is None:
        _, by_pd = _seat_stats()
    found = seatlib.resolve(wanted, sorted(active_agents()))

    out = []
    for seat_id in dict.fromkeys(wanted):        # de-duped, order kept
        here = found.get(seat_id)
        if here is None:
            out.append({"seat_id": seat_id, "base": "", "path": "", "label": "",
                        "slug": "", "missing": True, "session_count": 0,
                        "live": False, "last_activity": ""})
            continue
        base, rel = here
        root = workspace(base)
        st = by_pd.get(seatlib.project_dir_name(root / rel if rel else root))
        out.append({
            "seat_id": seat_id,
            "base": base,
            "path": rel,
            "label": rel or "root",
            # Where the seat stands, agent root down — what a card says about
            # itself on a surface with no agent header above it (Home's pins).
            "slug": seatlib.slug(base, rel),
            "missing": False,
            "session_count": st.count if st else 0,
            "live": bool(st and st.live),
            "last_activity": (datetime.fromtimestamp(st.last).isoformat()
                              if st and st.last else ""),
        })
    return out


def list_agents(stats=None) -> list[dict]:
    """Active agent roster (chat-scoped) with counts, live flag, role and last
    chat activity — everything the agent cards in the app render. `stats` is a
    `_seat_stats()` pair when the caller already has one, so `/agents` scans
    processes once, not twice.

    `base` is the bare agent name, sent rather than left to be derived: every
    seat id under an agent starts with it, so it is what the app groups carded
    seats by. Stripping it back off `agent_id` would be a decode, and the app
    has exactly one rule about seat ids — encode, never decode.

    `slug` is the same seat spelled readably — `ops/chat`, or the bare agent
    name when there is no chat dir to scope into. Read off the id this loop just
    built rather than parsed back out of it: `_chat_scoped_id` owns that fork,
    so this asks its answer instead of taking the fork a second time."""
    from . import notify
    from . import seats as seatlib
    chat, _ = stats or _seat_stats()
    muted = notify.muted_agents()
    out = []
    for name, cfg in sorted(active_agents().items()):
        roles = cfg.get("roles") or []
        agent_id = _chat_scoped_id(name)
        out.append({
            "notify_muted": name in muted,
            "agent_id": agent_id,
            "base": name,
            "slug": seatlib.slug(name, _CHAT_MODE if agent_id != name else ""),
            "name": cfg.get("name") or name.capitalize(),
            "description": cfg.get("description", ""),
            "emoji": cfg.get("emoji", ""),
            "sub_modes": submode_dirs(name),
            "session_count": chat.counts.get(name, 0),
            "live": chat.has_live.get(name, False),
            "role": roles[0] if roles else "",
            "last_activity": chat.stamp(name),
        })
    return out


def roster() -> dict:
    """What `/agents` answers: the agent cards and the carded seats, off one
    pass. Two calls would mean two process scans for one screen."""
    stats = _seat_stats()
    return {"agents": list_agents(stats), "seats": carded_seats(stats[1])}


_NOISE_PREFIXES = ("<",)


def _convo_lines(summary: dict) -> tuple[str, str]:
    """Last exchange for a chat card: (your last prompt, agent's last reply),
    one truncated line each; system-shaped noise suppressed. A spawn-marked
    prompt is the session's injected task — surfaced, marker shed."""
    from .messages import spawn_task

    def clean(text: str) -> str:
        text = " ".join((text or "").split())
        task = spawn_task(text)
        if task:
            return task[:120]
        if not text or text.startswith(_NOISE_PREFIXES):
            return ""
        return text[:120]
    return clean(summary.get("last_prompt") or ""), clean(summary.get("last_msg") or "")


def _preview(summary: dict) -> str:
    """One-line preview for a session box. A machine-spawned session previews
    as the task it was injected with — never an anonymous 'New session'."""
    from .messages import spawn_task
    for key in ("custom_title", "ai_title", "first_real_user_msg", "first_msg"):
        val = (summary.get(key) or "").strip()
        if not val:
            continue
        task = spawn_task(val)
        if task:
            return task[:80]
        if not val.startswith("<"):
            return val[:80]
    slug = (summary.get("slug") or "").replace("-", " ").title()
    return slug[:80]


def _session_colors() -> dict[str, str]:
    """{sid: color} from the session index — one lookup per board build.

    The index is the only reader of `agent-color` (see
    `store.session_colors`); the board asks it rather than parsing, so the
    live payload and the history payload can never disagree about a color.
    A store that can't answer costs the tint, never the board."""
    try:
        from .store import get_store
        return get_store().session_colors()
    except Exception:
        return {}


def _session_launches() -> dict[str, str]:
    """{sid: shortcut_id} — which saved shortcut opened each session.

    One read per board build, same shape and same contract as `_session_tags`:
    a store that cannot answer costs the stamp and never the board. Most
    sessions have no entry at all — a desk-side claude, a takeover and a share
    all open without a shortcut, and that is the resting state."""
    try:
        from .store import get_store
        return get_store().launch_shortcuts()
    except Exception:
        return {}


def _session_tags() -> dict[str, list[str]]:
    """{sid: [tag, ...]} from the timeline store — one lookup per board build.

    Tags are the timeline's third axis: `tail` answers which seat, `recall`
    answers when, a tag answers what a stretch of work was *about*. They are a
    relation on the session rather than on the entry, which is what makes them
    cheap here — one query answers every card on the board instead of one per
    row.

    Read directly, in read-only mode. The store reserves *writes* for
    `bin/log_event` so there is exactly one writer; reads are explicitly free,
    and shelling out per card would cost a fork apiece on a payload that a
    phone polls.

    A store that cannot answer costs the tags, never the board — same contract
    as `_session_colors`. An untagged session is the normal case, not an error:
    most tags are assigned when a session ends."""
    import os
    import sqlite3
    root = os.environ.get("JSTACK_TIMELINE_DIR") or (Path.home() / "Logs" / "Timeline")
    db = Path(root) / "timeline.db"
    if not db.exists():
        return {}
    out: dict[str, list[str]] = {}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
        try:
            rows = con.execute(
                "SELECT st.session_id, t.name FROM session_tags st "
                "JOIN tags t ON t.id = st.tag_id ORDER BY t.name").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    for sid, name in rows:
        out.setdefault(sid, []).append(name)
    return out


def open_sessions() -> list[dict]:
    """All currently-open managed sessions (any agent) — for the board's 'Open'
    section. Registry-backed so a just-opened session shows before its JSONL
    exists; preview + activity come from the JSONL once it's written."""
    from . import managed
    reg = managed.open_registry()
    if not reg:
        return []
    agents = active_agents()
    tags = _session_tags()
    out = []
    for sid, info in reg.items():
        base = info.get("agent", "")
        cfg = agents.get(base, {})
        preview, mtime = "", 0.0
        if _CLAUDE_PROJECTS.exists():
            for pd in _CLAUDE_PROJECTS.iterdir():
                f = pd / f"{sid}.jsonl"
                if f.exists():
                    summary = get_session_summary(f)
                    preview = _preview(summary) if summary else ""
                    try:
                        mtime = f.stat().st_mtime
                    except OSError:
                        mtime = 0.0
                    break
        out.append({
            "session_id": sid,
            "agent_id": base,
            "agent_name": cfg.get("name") or base.capitalize(),
            "emoji": cfg.get("emoji", ""),
            "preview": preview,
            "last_activity": datetime.fromtimestamp(mtime).isoformat() if mtime else "",
            # Same two fields every other board builder carries. They were
            # missing here, so the board's Open section — the one place a
            # just-spawned session shows before it has written anything — was
            # the one place the app could not tell a Codex row from a Claude
            # one. A row that cannot say which CLI it runs is a row the user has
            # to open to identify.
            "engine": info.get("engine", "claude"),
            "model": info.get("model", ""),
            "tags": tags.get(sid, []),
        })
    out.sort(key=lambda s: s["last_activity"], reverse=True)
    return out


def active_sessions() -> list[dict]:
    """Every claude window/worker on the Mac, for the app's main page.

    Flags per row:
      `live`    — actually producing output right now (JSONL written in the
                  last 90s, or a jRemote turn in flight)
      `open`/`on_mac` — a terminal window on the Mac is displaying it right
                  now, idle included. Same fact, and both are observed (tmux
                  client / controlling tty), never inferred: a session whose
                  window the user closed reports False from the next poll on, even
                  though its tmux pane and claude keep running.
      `managed` — a phone-drivable tmux exists. Independent of `open`: a
                  phone-opened session is managed with no window, and a
                  closed-window session stays managed too.
    Agent workspaces carry agent identity; any other folder is labeled by
    its name. A window whose session can't be identified yet (fresh, no
    transcript writes) shows as a synthetic `pid-<n>` row until its first
    write, then becomes the real session."""
    import time as _time
    from . import managed, notify
    from .turns import _in_flight

    procs, orphans = _proc_scan()
    reg = managed.open_registry()
    attached = managed.attached_names()
    agents = active_agents()
    unread = notify.unread_sids()
    dialogs = _dialog_sids()
    colors = _session_colors()
    tags = _session_tags()
    launches = _session_launches()
    now = _time.time()

    def identity(pd, label: str):
        """(agent_id, name, emoji, sub_mode) — agent identity when the dir is
        an agent workspace, folder label otherwise."""
        parsed = project_dir_to_agent(pd.name) if pd is not None else None
        if parsed:
            base, mode = parsed
            cfg = agents.get(base, {})
            return (base, cfg.get("name") or base.capitalize(),
                    cfg.get("emoji", ""), _display_sub_mode(mode))
        return "", label, "🖥️", ""

    def entry(sid, pd, label, window, headless=False, pids=(), window_name="",
              engine="", tty=False, running=True, model=""):
        agent_id, name, emoji, sub_mode = identity(pd, label)
        # A spawn's own title — argv `--name` wherever the process states one,
        # the open registry for a managed session (which states its title to
        # the registry instead). What the app shows for a session that has no
        # transcript yet, instead of an anonymous "New window".
        #
        # argv first because it is observed rather than recorded: it is exactly
        # as alive as the process, so it cannot outlive it or be reaped out
        # from under it. The registry can only answer for a live tmux, so it is
        # the fallback and never the other way round.
        window_name = window_name or (reg.get(sid) or {}).get("name", "")
        state = "idle"
        preview, mtime, tokens, last_prompt, last_reply = "", 0.0, 0, "", ""
        path, last_context, size, turns = "", 0, 0, 0
        spawned = 0.0
        if sid and not sid.startswith("pid-") and pd is not None:
            f = pd / f"{sid}.jsonl"
            if f.exists():
                path = str(f)
                summary = get_session_summary(f)
                preview = _preview(summary) if summary else ""
                tokens = (summary or {}).get("total_tokens", 0)
                last_context = (summary or {}).get("last_context", 0)
                turns = (summary or {}).get("calls", 0)
                if summary:
                    last_prompt, last_reply = _convo_lines(summary)
                try:
                    st = f.stat()
                    mtime = st.st_mtime
                    size = st.st_size
                    spawned = _birth(f)
                except OSError:
                    mtime = 0.0
        if not spawned:
            # No transcript to be born from — the process's own start is the
            # spawn. This is not only the fresh-window case: a session opened
            # and left at an empty prompt (every session spawned from the
            # phone, until the user types) runs for minutes before Claude Code
            # writes its JSONL, and a row with no `spawned` has no place in a
            # board ordered by it. create_time, not now-minus-uptime: a value
            # that drifts between polls would make every snapshot read as a
            # board change.
            if sid and sid.startswith("pid-"):
                pids = (sid[4:],)
            spawned = _proc_start(pids)
        # One tail read serves the working dot and the error half of the red
        # dot — managed rows only (the read costs). The waiting half comes
        # from the hook markers and applies to ANY interactive session: a raw
        # desk window sitting on a permission prompt is exactly when the tap
        # on the shoulder is wanted.
        #
        # Deliberately NOT gated on `headless`. A managed session always owns
        # a tmux pane, so a blank tty is a failed `ps` — never a fact about
        # the session. Gating here let one blind read skip the turn clock
        # altogether and leave `state` at its "idle" default: the row asserted
        # turn_open=False having observed nothing, and the notify watcher
        # pushed that as a done into a turn still running.
        turn_read = bool((sid in reg) and path)
        if turn_read:
            if sid in _in_flight:
                state = "working"
            elif pids:
                state = _turn_state(path, pids)
            elif _turn_marker_open(path):
                # The scan missed this session's process this pass. Absence
                # of evidence is not idleness: while the harness's turn clock
                # says the turn is open, the row keeps its working verdict —
                # one short scan otherwise read every mid-turn session as
                # freshly idle and fired a done-push for each of them.
                state = "working"
        if (sid in reg) and path and state != "working":
            # The turn just read closed, and that read is NEWER than the
            # summary that produced `last_reply` above — a turn whose final
            # text lands between the two hands the watcher a fresh idle
            # married to the previous turn's answer, and the done-push says
            # something the user was already told. Re-derive against the tail the
            # verdict was actually taken from. Costs a stat: unchanged since
            # the first read, the summary cache returns the same object.
            summary = get_session_summary(Path(path))
            if summary:
                last_prompt, last_reply = _convo_lines(summary)
        if not headless and state != "error" and sid in dialogs:
            state = "waiting"
        return {
            "session_id": sid,
            "agent_id": agent_id,
            "agent_name": name,
            "emoji": emoji,
            "sub_mode": sub_mode,
            "window_name": window_name,
            "preview": preview,
            "last_activity": datetime.fromtimestamp(mtime).isoformat() if mtime else "",
            "spawned": datetime.fromtimestamp(spawned).isoformat() if spawned else "",
            "tokens": tokens,
            "last_context": last_context,
            "turns": turns,
            # The session's own color, as Claude Code's `/color` recorded it.
            # Read from the session index, never parsed here: see
            # `store.session_colors`. "" is every other case — no color set,
            # not indexed yet, or an engine that has no such thing.
            "agent_color": colors.get(sid, ""),
            # What the session is about, from the timeline's tag relation.
            # Empty is the normal resting state for a chat — tags are assigned
            # by the session-end self-write, so a live one is usually untagged.
            # An auto-spawned issue-work session is the exception: it is tagged
            # at birth, because its subject was settled by the assignment.
            #
            # Not the same field as `tag` below, and the difference is the
            # whole point of the pin: `tags` is the FILING — every subject this
            # session's work belongs under, assignable by any hand at any time.
            # `tag` is the COCKPIT — the one subject whose history it was
            # opened reading. A pinned session ends up in both (the injector
            # files it under its pin at birth); a hand-tagged one is only ever
            # in `tags`, and calling that a pin would claim a window it never
            # had.
            "tags": tags.get(sid, []),
            "path": path,
            "size": size,
            "last_prompt": last_prompt,
            "last_reply": last_reply,
            "live": _is_live(sid, headless, mtime, now, _in_flight, path, pids),
            "open": window,
            "on_mac": window,
            "managed": sid in reg,
            # A process for this session is running RIGHT NOW — the fact the
            # board exists to report. Not liveness (`live` lingers ~90s past
            # the last write and a quiet session is still spending its
            # context), not a window, not an identity: if it holds a process,
            # it holds a row. Every surface downstream keys its "show it" on
            # this and nothing else.
            "running": running,
            # What holds this session, which is what it IS — never what it is
            # doing or who is watching. `window` (a client is displaying it)
            # stays a display fact on the row; a detached tmux pane is still a
            # terminal on this Mac and must not read as a headless worker.
            "hold": ("managed" if sid in reg else
                     "window" if window else
                     "terminal" if tty else "headless"),
            # Which CLI this session runs — the spawn's own registry entry
            # first, then what the process scan actually saw. Never a bare
            # default: an unmanaged Codex has no registry row, and calling it
            # "claude" is the board asserting an engine nobody observed.
            "engine": ((reg.get(sid) or {}).get("engine")
                       or engine or "claude"),
            # The spawn's registry entry first, then the model pinned in the
            # process's own argv. Registry first only because it is the older
            # record and the two never disagree — both are written by the same
            # spawn. argv is what answers for a run with no registry row at
            # all, which is every headless worker: the registry is gated on a
            # live tmux, so it has nothing to say about one.
            "model": ((reg.get(sid) or {}).get("model", "") or model),
            # The subject this session was opened ON — the pin, singular. See
            # `tags` above for why this is a second field and not a slice of
            # that one. Registry-sourced and never inferred: the spawn that
            # exported the pin is the same call that wrote this, so the board's
            # claim and the session's actual injected window cannot disagree.
            #
            # This is also the only thing that tells two rows apart. Pin two
            # subjects onto the same seat and the board has two ops/chat
            # cards with the same name, color and cwd, reading different
            # histories — without this, the user picks one at random.
            "tag": (reg.get(sid) or {}).get("tag", ""),
            # Which saved shortcut opened this session, "" for every session
            # started any other way. Store-sourced, not registry: it has to
            # outlive the close, because a shortcut's history is mostly
            # sittings that are over.
            "shortcut_id": launches.get(sid, ""),
            # What the turn is DOING, observed: "working" (claude owes a reply
            # RIGHT NOW — transcript tail / running tool child / turn in
            # flight), "idle" (it doesn't), "" (nobody read the turn clock for
            # this row — it isn't managed, or it has no transcript yet).
            #
            # Three values, not a bool, because the bool it replaced could not
            # tell "observed idle" from "never looked", and every reader of a
            # false had to guess which one it had. `live` lingers ~90s past
            # the last write by design; this flips within a tick of the reply
            # landing, which is what the done-processing notification keys on
            # — and, since a working dot that stays green a minute and a half
            # after the answer arrived is the board lying to the user, what the
            # app's status dot keys on too.
            "turn": ("working" if state == "working" else "idle") if turn_read else "",
            # Blocked on the user — the red dot: 'waiting' (permission prompt or
            # AskUserQuestion sitting unanswered) or 'error' (an API failure
            # is the session's last word). Clears by itself the moment the
            # transcript moves past the blocking line.
            "attention": state if state in ("waiting", "error") else "",
            # Finished working, not yet touched — the orange dot. Server-owned
            # because Mac interaction clears it (see notify/notify_watch).
            "unread": sid in unread,
        }

    def codex_row(sid, info, shown, running=False, tty=False):
        """A board row for a managed session that has no Claude transcript to
        be built from — a Codex pane.

        Its facts come from the spawn registry and the rollout file the Codex
        CLI writes, because a Codex session writes nothing under
        `~/.claude/projects`. Shared by both row loops on purpose: whether the
        process scan happened to name this pane's process must not change what
        the row says about it."""
        base = info.get("agent", "")
        cfg = agents.get(base, {})
        codex_messages = []
        codex_path = info.get("transcript", "")
        if codex_path:
            try:
                from .messages import parse_session
                codex_messages = parse_session(sid).get("messages", [])
            except Exception:
                codex_messages = []
        first_user = next((m.get("text", "") for m in codex_messages
                           if m.get("role") == "user" and m.get("text")), "")
        last_user = next((m.get("text", "") for m in reversed(codex_messages)
                          if m.get("role") == "user" and m.get("text")), "")
        last_reply = next((m.get("text", "") for m in reversed(codex_messages)
                           if m.get("role") == "assistant" and m.get("text")), "")
        st = None
        try:
            st = Path(codex_path).stat() if codex_path else None
            codex_activity = datetime.fromtimestamp(st.st_mtime).isoformat() if st else ""
            codex_spawned = datetime.fromtimestamp(
                getattr(st, "st_birthtime", st.st_mtime)).isoformat() if st else ""
            codex_size = st.st_size if st else 0
        except OSError:
            codex_activity = codex_spawned = ""
            codex_size = 0
        try:
            from .load import reading
            codex_load = reading(sid) or {}
        except Exception:
            codex_load = {}
        return {
            "session_id": sid, "agent_id": base,
            "agent_name": cfg.get("name") or base.capitalize(),
            "emoji": cfg.get("emoji", ""), "sub_mode": _CHAT_MODE,
            "window_name": info.get("name", ""),
            "preview": first_user[:80], "last_activity": codex_activity,
            "spawned": codex_spawned,
            "last_prompt": last_user, "last_reply": last_reply,
            "tokens": 0, "last_context": codex_load.get("context", 0),
            "turns": codex_load.get("turns", 0), "path": codex_path,
            # Codex has no `/color` and its rollouts record none — a Codex
            # thread is untinted by construction, not by omission.
            "agent_color": "",
            "live": bool(codex_activity and
                         now - (st.st_mtime if st else 0) < _LIVE_WINDOW_SECS),
            "size": codex_size,
            "open": shown, "on_mac": shown,
            # Observed, never asserted — the same rule every other row obeys.
            # Hardcoding these was what let one bad identity land in Active as
            # a phone-drivable session: no registry row, no pane, no window,
            # and the card still claimed a managed hold.
            "managed": sid in reg,
            "running": running,
            "hold": ("managed" if sid in reg else
                     "window" if shown else
                     "terminal" if tty else "headless"),
            # Which CLI this session runs. A Codex session writes no Claude
            # transcript, so this registry row IS its board presence —
            # without the engine here the app has nothing to label it with.
            "engine": info.get("engine", "claude"),
            "model": info.get("model", ""),
            # The pin, same as the main builder — and it matters MORE here.
            # This is the Open section, the one place a session shows before it
            # has written anything, which is exactly the window in which a
            # freshly-pinned row has no preview to tell it apart by.
            "tag": info.get("tag", ""),
            # Never read: a Codex pane writes no Claude transcript, so there
            # is no turn clock here to read. "" says exactly that — asserting
            # "idle" would hand the app a resting session that is mid-turn.
            "turn": "",
            # No transcript yet, but a dialog can already be up — the
            # hook marker doesn't need a JSONL to exist.
            "attention": "waiting" if sid in dialogs else "",
            "unread": sid in unread,
        }

    # Every running agent CLI gets a row — the scan only ever sees live
    # processes, so nothing here is stale. What varies is how it's flagged:
    # `hold` says what holds it, `managed` says the phone can drive it. A
    # session with neither window nor tmux (a headless worker, someone else's
    # detached run) is still spending tokens and still belongs on the board.
    # Dropping rows on a flag was how a running session could silently vanish
    # from the app.
    rows: dict[str, dict] = {}
    for sid, info in procs.items():
        reg_info = reg.get(sid) or {}
        # A named Codex process is a pane the scan resolved through
        # `_pane_sids`. Its row is still the registry/rollout one — `entry`
        # would look for a Claude transcript that a Codex session never
        # writes and produce a blank row where a working session was.
        if (reg_info.get("engine") or info.get("engine", "")) == "codex":
            rows[sid] = codex_row(sid, reg_info, info["window"], running=True,
                                  tty=info.get("tty", False))
        else:
            rows[sid] = entry(sid, info["pd"], info["label"],
                              info["window"], info["headless"],
                              info.get("pids") or (),
                              window_name=info.get("name", ""),
                              engine=info.get("engine", ""),
                              tty=info.get("tty", False),
                              model=info.get("model", ""))
    # Running processes with no session identity — pid-<n> rows. A fresh
    # window that hasn't written its transcript, and every Codex nobody
    # spawned through jRemote. `close` kills these by their pid, so a row
    # here is a session the user can both see and end.
    for o in orphans:
        sid = f"pid-{o['pid']}"
        rows[sid] = entry(sid, o["pd"], o["label"], o.get("window", True),
                          headless=not o.get("tty", False),
                          window_name=o.get("name", ""),
                          engine=o.get("engine", ""),
                          tty=o.get("tty", False))

    # Managed tmux sessions whose process the scan missed (or fresh, no JSONL).
    # Their window-ness is the attached-client check, same observation as every
    # other row — never an assumed True just because the registry lists them.
    for sid, info in reg.items():
        if sid in rows:
            continue
        shown = ("jr-" + sid[:8]) in attached
        row = None
        if _CLAUDE_PROJECTS.exists():
            for pd in _CLAUDE_PROJECTS.iterdir():
                if pd.is_dir() and (pd / f"{sid}.jsonl").exists():
                    # The scan found no process for this pane — the claude is
                    # gone and the pane outlived it, or the scan blinked. The
                    # row says so rather than claiming a process it never saw.
                    row = entry(sid, pd, pd.name, shown, running=False)
                    break
        if row is None:
            row = codex_row(sid, info, shown, running=False)
        rows[sid] = row

    out = list(rows.values())
    # Spawn order, not activity: activity reshuffles the board under the user's
    # thumb on every write, and the dot already says who's working.
    out.sort(key=lambda s: (s["spawned"], s["session_id"]), reverse=True)
    return out


def list_sessions(agent: str | None = None,
                  shortcut: str | None = None) -> list[dict]:
    """All agent sessions, newest-first. `agent` filters by base id ('ops')
    or base+mode id ('ops-chat'); `shortcut` narrows to the sittings one
    saved shortcut opened. The two compose — a shortcut names a seat anyway,
    so passing both costs nothing and asking for either alone is the normal
    call.

    Flags carry the same truth as /sessions/active — `live` = producing
    output right now, `open`/`on_mac` = a terminal window is displaying it on
    the Mac right now, `managed` = phone-drivable tmux — so a sync from either
    endpoint agrees. Window-ness comes from the shared `_proc_scan`, so the two
    endpoints cannot drift apart on it."""
    import time as _time
    from . import managed, notify
    from .turns import _in_flight
    if not _CLAUDE_PROJECTS.exists():
        return []
    procs, _ = _proc_scan()
    open_names = managed.open_names()
    unread = notify.unread_sids()
    dialogs = _dialog_sids()
    colors = _session_colors()
    tags = _session_tags()
    launches = _session_launches()
    now = _time.time()
    want_base, want_mode = None, None
    if agent:
        parsed = agent.split("-", 1)
        want_base = parsed[0].lower()
        want_mode = parsed[1] if len(parsed) > 1 else None
    # A shortcut's OWN history. Narrower than the seat's and deliberately so:
    # two shortcuts onto one seat are two threads of work, and the whole point
    # of a card that carries its own config is that its sittings are its own.
    # An id nothing has launched yet answers with an empty list, which is the
    # honest answer for a shortcut made a minute ago.
    from .store import canonical_id
    want_shortcut = canonical_id(shortcut) if shortcut else ""

    sessions = []
    for project_dir in _CLAUDE_PROJECTS.iterdir():
        if not project_dir.is_dir() or project_dir.name.startswith("."):
            continue
        parsed = project_dir_to_agent(project_dir.name)
        if not parsed:
            continue  # not an agent workspace — skip
        base, mode = parsed
        if want_base and base != want_base:
            continue
        if want_mode and _display_sub_mode(mode) != _display_sub_mode(want_mode) and mode != want_mode:
            continue
        for f in project_dir.glob("*.jsonl"):
            sid = f.stem
            if want_shortcut and launches.get(sid, "") != want_shortcut:
                continue
            try:
                st = f.stat()
                mtime = st.st_mtime
                size = st.st_size
                born = _birth(f)
            except OSError:
                continue
            summary = get_session_summary(f)
            if not summary:
                continue
            last_prompt, last_reply = _convo_lines(summary)
            info = procs.get(sid)
            jr_name = "jr-" + sid[:8]
            is_managed = jr_name in open_names
            window = bool(info and info["window"])
            # Same working- and red-dot truths as /sessions/active, computed
            # the same way, so a history sync can never wipe what a board sync
            # just set. `turn` stays "" wherever the clock wasn't read — a
            # blank leaves the board's answer standing; an invented "idle"
            # would overwrite it with a resting session that is mid-turn.
            attention, turn = "", ""
            if info and sid in _in_flight:
                turn = "working" if is_managed else ""
            elif info and not info["headless"]:
                pids = info.get("pids") or ()
                turn_st = (_turn_state(str(f), pids)
                           if is_managed and pids else "")
                if turn_st:
                    turn = "working" if turn_st == "working" else "idle"
                if turn_st == "error":
                    attention = "error"
                elif sid in dialogs:
                    attention = "waiting"
            sessions.append({
                "session_id": sid,
                "agent_id": base,
                "sub_mode": _display_sub_mode(mode),
                "preview": _preview(summary),
                "last_prompt": last_prompt,
                "last_reply": last_reply,
                "last_activity": datetime.fromtimestamp(mtime).isoformat(),
                # Same spawn truth as /sessions/active — the app's history
                # list sorts by it, so it must never be board-only.
                "spawned": datetime.fromtimestamp(born).isoformat(),
                "tokens": summary.get("total_tokens", 0),
                "last_context": summary.get("last_context", 0),
                "turns": summary.get("calls", 0),
                "agent_color": colors.get(sid, ""),
                # Same tag relation the board carries, so a history sync and a
                # board sync can never disagree about what a session was about.
                "tags": tags.get(sid, []),
                "shortcut_id": launches.get(sid, ""),
                "path": str(f),
                "size": size,
                "live": bool(info) and _is_live(sid, info["headless"], mtime, now,
                                                _in_flight, str(f), info.get("pids") or ()),
                "open": window,
                "on_mac": window,
                "managed": is_managed,
                "turn": turn,
                "attention": attention,
                "unread": is_managed and sid in unread,
            })
    sessions.sort(key=lambda s: s["last_activity"], reverse=True)
    return sessions
