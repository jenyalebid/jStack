"""Device registry + push decision for jRemote notifications.

Tracks registered device tokens, per-agent mutes, which thread the phone is
looking at, and the unread badge count. `notify()` fires a push only when
nothing says the user is already looking and nothing says they asked not to be told:

  - the phone's PTY client attached to that session (server-owned fact — the
    thread is literally on screen) suppresses it;
  - the app's foreground report on that session suppresses it;
  - a per-agent mute suppresses it.

The APNs send itself runs on a daemon thread: a slow or hung push must never
stall the board watcher's loop.
"""

import json
import threading
import time
from pathlib import Path

from . import apns
from . import hostenv

_STATE = hostenv.state_dir() / "jremote_devices.json"
_FG_TTL = 30  # a foreground heartbeat older than this = app backgrounded
_lock = threading.Lock()

# Sessions the phone is attached to over the PTY WebSocket right now.
# Connection-scoped truth, so it lives in memory: a dashboard restart drops
# every WebSocket with it. pty.py marks/unmarks around each connection.
# A count, not a set: on reconnect the new attach overlaps the old one's
# teardown, and the old unmark must not erase the new mark.
_attached: dict[str, int] = {}


def _load() -> dict:
    try:
        d = json.loads(_STATE.read_text())
    except (OSError, json.JSONDecodeError):
        d = {}
    d.setdefault("tokens", [])
    d.setdefault("muted_agents", [])
    d.setdefault("progress_muted", [])
    d.setdefault("fg_sid", None)
    d.setdefault("fg_at", 0.0)
    # Sessions that finished working and haven't been touched since — the
    # orange dot on the board, and the app badge is their count. Cleared by
    # any interaction, from either end: opening the thread on the phone,
    # typing into the session on the Mac (its turn re-opens), or closing it.
    d.setdefault("unread_sids", [])
    return d


def _save(d: dict) -> None:
    _STATE.parent.mkdir(parents=True, exist_ok=True)
    _STATE.write_text(json.dumps(d))


def register(token: str) -> None:
    with _lock:
        d = _load()
        if token and token not in d["tokens"]:
            d["tokens"].append(token)
            _save(d)


def set_foreground(session_id: str | None) -> None:
    """App reports what's on screen (a session id, or None for the board).
    Opening a thread is interaction — that session's unread mark clears."""
    with _lock:
        d = _load()
        d["fg_sid"] = session_id
        d["fg_at"] = time.time()
        if session_id and session_id in d["unread_sids"]:
            d["unread_sids"].remove(session_id)
        _save(d)


def unread_sids() -> set[str]:
    return set(_load()["unread_sids"])


def clear_unread(session_id: str) -> None:
    """The session was interacted with — Mac typing, a close, a phone open."""
    with _lock:
        d = _load()
        if session_id in d["unread_sids"]:
            d["unread_sids"].remove(session_id)
            _save(d)


def mark_attached(session_id: str) -> None:
    with _lock:
        _attached[session_id] = _attached.get(session_id, 0) + 1
    clear_unread(session_id)


def unmark_attached(session_id: str) -> None:
    with _lock:
        n = _attached.get(session_id, 0) - 1
        if n > 0:
            _attached[session_id] = n
        else:
            _attached.pop(session_id, None)


def _base_agent(agent_id: str) -> str:
    """Mutes key on the base agent — 'ops-chat' and 'ops-social-chat'
    are seats of the one agent the user mutes."""
    return (agent_id or "").split("-")[0].lower()


def muted_agents() -> set[str]:
    return {_base_agent(a) for a in _load()["muted_agents"]}


def set_muted(agent_id: str, muted: bool) -> None:
    base = _base_agent(agent_id)
    if not base:
        return
    with _lock:
        d = _load()
        current = {_base_agent(a) for a in d["muted_agents"]}
        if muted:
            current.add(base)
        else:
            current.discard(base)
        d["muted_agents"] = sorted(current)
        _save(d)


def progress_muted_agents() -> set[str]:
    return {_base_agent(a) for a in _load()["progress_muted"]}


def set_progress_muted(agent_id: str, muted: bool) -> None:
    base = _base_agent(agent_id)
    if not base:
        return
    with _lock:
        d = _load()
        current = {_base_agent(a) for a in d["progress_muted"]}
        if muted:
            current.add(base)
        else:
            current.discard(base)
        d["progress_muted"] = sorted(current)
        _save(d)


def _foreground_on(d: dict, session_id: str) -> bool:
    return d["fg_sid"] == session_id and (time.time() - d["fg_at"]) < _FG_TTL


def _send_all(tokens: list[str], *, title: str, body: str,
              badge: int | None, session_id: str,
              collapse_id: str = "", extra: dict | None = None) -> None:
    dead = []
    # Only a payload that carries more than a session names `extra`, so a
    # session push calls apns.send exactly as it always has.
    more = {"extra": extra} if extra else {}
    for token in tokens:
        ok, detail = apns.send(token, title=title, body=body,
                               badge=badge, session_id=session_id,
                               collapse_id=collapse_id, **more)
        print(f"jremote notify: {detail} [{session_id[:8]}] "
              f"…{token[-8:]} {title}", flush=True)
        # A token APNs will never take again leaves the registry:
        # BadDeviceToken = wrong environment after both were tried,
        # Unregistered = the install is gone, BadEnvironmentKeyInToken = a
        # simulator token that reached real APNs. Left in place they draw
        # an error on every send, forever.
        if not ok and any(r in detail for r in
                          ("BadDeviceToken", "Unregistered",
                           "BadEnvironmentKeyInToken")):
            dead.append(token)
    if dead:
        with _lock:
            d = _load()
            d["tokens"] = [t for t in d["tokens"] if t not in dead]
            _save(d)


def notify(session_id: str, agent_id: str, *, title: str, body: str) -> bool:
    """A session finished working. Marks it unread and pushes — unless the user is
    already in that thread (then it isn't unread and needs no push), or the
    agent is muted (unread dot yes, buzz no).
    Returns whether a push was dispatched (for tests; delivery is async)."""
    if session_id in _attached:
        return False
    with _lock:
        d = _load()
        if _foreground_on(d, session_id):
            return False
        if session_id not in d["unread_sids"]:
            d["unread_sids"].append(session_id)
            _save(d)
        if _base_agent(agent_id) in {_base_agent(a) for a in d["muted_agents"]}:
            return False
        if not d["tokens"] or not apns.is_configured():
            return False
        tokens, badge = list(d["tokens"]), len(d["unread_sids"])
    threading.Thread(
        target=_send_all, kwargs=dict(tokens=tokens, title=title, body=body,
                                      badge=badge, session_id=session_id),
        daemon=True,
    ).start()
    return True


def broadcast(*, title: str, body: str, extra: dict | None = None,
              collapse_id: str = "") -> bool:
    """A push about no session — a run shortcut ending. Every registered
    device gets it; there is no thread to be foreground on or agent to mute.
    Returns whether a push was dispatched."""
    with _lock:
        d = _load()
        if not d["tokens"] or not apns.is_configured():
            return False
        tokens = list(d["tokens"])
    threading.Thread(
        target=_send_all, kwargs=dict(tokens=tokens, title=title, body=body, badge=None,
                                      session_id="", collapse_id=collapse_id,
                                      extra=extra),
        daemon=True,
    ).start()
    return True


def progress(session_id: str, agent_id: str, *, title: str, body: str) -> bool:
    """A mid-turn narration line from a still-working session — an FYI, not a
    state change. No unread mark, no badge; suppressed by everything that
    suppresses a done-push, plus its own per-agent toggle. Pushes for one
    session share a collapse id, so the stream reads as one updating banner.
    Returns whether a push was dispatched (delivery is async)."""
    if session_id in _attached:
        return False
    with _lock:
        d = _load()
        if _foreground_on(d, session_id):
            return False
        muted = {_base_agent(a) for a in d["muted_agents"]}
        muted |= {_base_agent(a) for a in d["progress_muted"]}
        if _base_agent(agent_id) in muted:
            return False
        if not d["tokens"] or not apns.is_configured():
            return False
        tokens = list(d["tokens"])
    threading.Thread(
        target=_send_all, kwargs=dict(tokens=tokens, title=title, body=body,
                                      badge=None, session_id=session_id,
                                      collapse_id=f"prog-{session_id}"),
        daemon=True,
    ).start()
    return True
