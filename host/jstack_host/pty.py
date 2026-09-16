"""PTY WebSocket — the phone as a true terminal onto a managed session.

`/api/jremote/v1/sessions/{sid}/pty` upgrades to a WebSocket, spawns a
`tmux attach` client on the managed socket inside a real PTY, and pipes raw
bytes both ways. The phone renders them in a terminal emulator (SwiftTerm) —
a 1:1 view of the native CLI, byte-identical to iTerm attaching on the Mac.

Protocol:
  client → server  binary frame        raw keyboard/paste bytes → PTY stdin
  client → server  text frame (JSON)   {"type":"resize","cols":N,"rows":N}
  server → client  binary frame        raw PTY output (ANSI stream)
  server → client  text frame (JSON)   {"type":"end","code":N,"reason":s} —
                   the disposition, sent in-band just before the close (the
                   close frame itself races TCP teardown in URLSession and
                   can surface as ENOTCONN with the code lost) — and
                   {"type":"open","url":"jremote://session/…"} — a spawn this
                   instance drove (typed the handoff into this session); the
                   app opens it per its own window setting, or not at all
  close codes      4401 bad/missing token, or the device revoked mid-connection ·
                   4404 unknown session ·
                   4409 session held by a raw Mac terminal ·
                   4411 session ended (EOF, or `?reattach=1` on an idle
                   session) · 4412 dismissed from another device (the view
                   closes, the session lives) · 1000 detached

`?instance=<id>&platform=mac|pad|phone` names the app install behind the
connection (attach.py): who is showing each thread, and — stamped by every
binary input frame — who drove it last.

Disconnect kills only the tmux *client* (a detach) — the session keeps
running; its life is its claude process (the board invariant — see
`managed.py`). Every client is a viewer: attaching an idle session opens it
managed with no terminal window required, and detaching ends nothing.

**This socket never creates a session.** Attaching to one that is gone
closes 4411 so the thread dismisses. Resuming is a separate, deliberate act
with its own endpoint (`POST /sessions/{sid}/open`), and every client path
that means to resume already calls it first — the app's Open CLI, Take CLI
and new-chat spawn all stand the session up over HTTP and only then attach.

It used to revive on a plain attach, on the belief that "walking into a
thread IS the resume". That belief was already false when it was written:
the resume had its own endpoint the whole time, so the revive branch had no
caller that wanted it. What it did have was one that didn't — a thread
opened on a board row that went stale (the session was closed from another
device in the gap) attached plainly and silently respawned a `claude` the
user had just ended. That is the "sessions keep respawning" report of
2026-09-03: sid 7a2b3b3e was closed from the phone at 13:48:31 and stood
back up by the Mac app at 14:26, to be closed a second time at 14:27:42.
A viewer that can conjure what it is viewing has no way to tell the user
their session is gone — so it no longer can, and a stale row now heals
(4411 → the app drops its copy) instead of resurrecting.

`?reattach=1` is still sent by shipped builds and is accepted and ignored:
both intents are the same intent now.

The client is tagged `JREMOTE_PHONE_CLIENT=1` so `attached_names()` (the
"an iTerm window is showing this" display fact) can exclude it.

Auth is checked in-handler (accept → verify → close); the shared router
dependency raises HTTPException, which has no clean WS path.
"""

import asyncio
import fcntl
import json
import os
import pty as _pty
import signal
import struct
import termios
from pathlib import Path

from fastapi import APIRouter
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import attach, devices, managed, notify
from .auth import authenticate_ws
from .router import _SID_RE

ws_router = APIRouter()

_READ_CHUNK = 65536


def _authorized(ws: WebSocket) -> str:
    """The caller's device id, or "" — auth.authenticate_ws, same gate as the
    HTTP routes. The id is held for the life of the socket: revoking the
    device must end THIS connection, not just the next one."""
    return authenticate_ws(ws)


class SessionEnded(Exception):
    """Attach refused: there is no live session behind this sid."""


def _ensure_managed(sid: str) -> None:
    """Attach only. Raises unless sid's managed session is standing right now.

    Raises RuntimeError (held by a raw Mac terminal) or SessionEnded (nothing
    holds it). No terminal window is involved: the client attaching IS the
    viewer, and the session's board registration is its visibility.

    **A viewer never creates what it views.** Standing a session up is the
    deliberate act behind `POST /sessions/{sid}/open`, which every client path
    that means to resume already calls before it attaches. Reviving here too
    was a second, implicit resume with no caller that wanted it — and one that
    did not: an attach carrying a board row that went stale respawned the
    session the user had just closed from another device (see the module
    docstring). The question an attaching client is asking is "does the session
    I am showing still stand", and answering it by making that true is the one
    answer that cannot be wrong and cannot be useful.

    The answer never depends on what is on disk. A session closed before Claude
    Code wrote its JSONL — every share-sheet spawn, until the user types — is
    indistinguishable from a sid that never existed, and answering "unknown
    sid" (4404) there sent the app a code it parks a Reconnect banner on, so
    the window outlived its session: the one thing the close path exists to
    prevent. Gone is gone, and it closes 4411."""
    if managed.is_open(sid):
        return
    from .board import _live_session_ids

    if sid in _live_session_ids():
        raise RuntimeError("session is open in a terminal on the Mac")
    raise SessionEnded("session ended")


async def _end(ws: WebSocket, code: int, reason: str) -> None:
    """Close with the code delivered in-band first.

    URLSessionWebSocketTask races the close frame against TCP teardown: when
    the server closes right after sending it, the client's pending receive
    can fail with ENOTCONN ("Socket is not connected") before the close code
    is delivered — the app then sees a transport error where the protocol
    said "session ended", and parks a Reconnect banner on a dead pane. A
    text frame is an ordinary message, ordered ahead of the close on the
    stream, so the app always learns the real disposition."""
    try:
        await ws.send_text(json.dumps({"type": "end", "code": code,
                                       "reason": reason}))
    except Exception:
        pass  # peer already gone — the close below is a no-op too
    try:
        await ws.close(code=code, reason=reason)
    except Exception:
        pass


def _eof_disposition(sid: str) -> tuple[int, str]:
    """EOF on the attach client: the session either died under the terminal or
    merely detached. tmux owns that fact (`is_open` asks it directly — the
    registry can lag a self-exit): still alive = a detach the client can
    reconnect to; gone = 4411, the app closes the thread instead of parking
    on a dead "[exited]" pane offering a Reconnect that can only fail."""
    if managed.is_open(sid):
        return 1000, "detached"
    return 4411, "session ended"


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    cols = max(20, min(500, int(cols)))
    rows = max(5, min(300, int(rows)))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _attach_argv(sid: str) -> list[str]:
    """The tmux client command for sid's session.

    `-T sync` is the client's half of a contract the CLI already keeps: it
    brackets every repaint in DEC 2026 synchronized output, and the app's
    terminal honours 2026 — but tmux only wraps its own repaints in it for a
    client that advertises the feature, and no TERM name implies it. Without
    the bracket the app paints half-applied frames, and since the CLI hides
    and shows the cursor once per frame — ten times a second while it works —
    the caret strobes and gets sampled wherever a frame was cut, leaving it
    sitting over content instead of on the prompt. Being a client feature it
    is a tmux global flag: it goes before the command, not after it."""
    return [managed._TMUX, "-L", managed._SOCK, "-T", "sync",
            "attach", "-t", managed._name(sid)]


def _spawn_attach(sid: str, cols: int, rows: int) -> tuple[int, int]:
    """Fork a `tmux attach` client for sid's managed session inside a fresh
    PTY. Returns (pid, master_fd). pty.fork gives the child a controlling
    tty, so TIOCSWINSZ on the master delivers SIGWINCH — resize just works."""
    env = {
        "PATH": managed._PATH,
        "TERM": "xterm-256color",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "HOME": str(Path.home()),
        # Marks this tmux client as the phone's mirror. Window truth reads it
        # back off the live process, so this attach can never be mistaken for
        # a window on the Mac.
        managed.PHONE_CLIENT_ENV: "1",
    }
    argv = _attach_argv(sid)
    pid, master = _pty.fork()
    if pid == 0:  # child — exec or die, never return into the server
        try:
            os.execve(argv[0], argv, env)
        finally:
            os._exit(127)
    _set_winsize(master, cols, rows)
    os.set_blocking(master, False)
    return pid, master


async def _write_all(fd: int, data: bytes) -> None:
    """Nonblocking PTY write with backoff — a large paste can outrun the
    kernel buffer; keystrokes never do."""
    view = memoryview(data)
    while view:
        try:
            n = os.write(fd, view)
            view = view[n:]
        except BlockingIOError:
            await asyncio.sleep(0.005)


async def _reap(pid: int) -> None:
    """Detach = kill the tmux client (never the session). SIGTERM, then
    SIGKILL if it lingers; always waitpid so no zombie accumulates."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
        for _ in range(20):
            try:
                done, _status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return
            if done:
                return
            await asyncio.sleep(0.05)


async def _drain(ws, out_q) -> "tuple[int, str] | None":
    """Flush the outbound queue to the socket until it ends.

    Items: ("data", bytes) → binary frame · ("text", json_str) → control
    frame (the open-thread command to a driving device) · ("close", code,
    reason) → an ordered disposition, returned so the handler closes with
    *that* code instead of guessing from EOF · None → PTY EOF, returns None
    and the EOF disposition path decides."""
    while True:
        item = await out_q.get()
        if item is None:
            return None
        if item[0] == "data":
            await ws.send_bytes(item[1])
        elif item[0] == "text":
            await ws.send_text(item[1])
        elif item[0] == "close":
            return item[1], item[2]


@ws_router.websocket("/api/jremote/v1/sessions/{sid}/pty")
async def pty_ws(ws: WebSocket, sid: str, cols: int = 80, rows: int = 24,
                 instance: str = "", platform: str = ""):
    # `reattach` is still in shipped builds' query string and is deliberately
    # not a parameter here: undeclared params are ignored, and there is no
    # longer a second behaviour for it to select.
    await ws.accept()
    device_id = await asyncio.to_thread(_authorized, ws)
    if not device_id:
        await ws.close(code=4401, reason="invalid or missing bearer token")
        return
    if not _SID_RE.match(sid):
        await _end(ws, 4404, "invalid session id")
        return
    try:
        _ensure_managed(sid)
    except SessionEnded:
        await _end(ws, 4411, "session ended")
        return
    except RuntimeError as e:
        await _end(ws, 4409, str(e))
        return

    pid, master = _spawn_attach(sid, cols, rows)
    # The user is in this thread — the strongest "already looking" fact there is,
    # and it lives server-side with the connection. Suppresses done-pushes.
    notify.mark_attached(sid)
    loop = asyncio.get_running_loop()
    out_q: asyncio.Queue = asyncio.Queue()

    # The connection on the registry: who is showing this thread (close on
    # other instances) and — once bytes flow — who is driving it (a handoff
    # opens where it was typed). Hooks feed the outbound pump; both are
    # called from async routes on this same loop.
    att = attach.Attachment(
        sid=sid, instance=instance, platform=platform,
        send_text=lambda payload: out_q.put_nowait(
            ("text", json.dumps(payload))),
        order_close=lambda code, reason: out_q.put_nowait(
            ("close", code, reason)),
    )
    attach.register(att)

    def _on_readable():
        try:
            data = os.read(master, _READ_CHUNK)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if data:
            out_q.put_nowait(("data", data))
        else:  # EOF — tmux client exited (detached elsewhere or session died)
            loop.remove_reader(master)
            out_q.put_nowait(None)

    loop.add_reader(master, _on_readable)

    async def pump_input():
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            data = msg.get("bytes")
            if data:
                # Keystrokes are the driving fact — a handoff typed here
                # must open its window on this instance.
                attach.note_input(att)
                if (managed.open_registry().get(sid) or {}).get("engine") == "codex":
                    from .codex_commands import pending_command, translate_paste, workspace
                    if data == b"\r":
                        command = await asyncio.to_thread(pending_command, sid)
                        if command is not None:
                            await _write_all(master, command)
                            await asyncio.sleep(0.3)
                    cwd = await asyncio.to_thread(workspace, sid) if data.startswith(b"\x1b[200~/") else ""
                    data = translate_paste(data, cwd)
                await _write_all(master, data)
                continue
            text = msg.get("text")
            if not text:
                continue
            try:
                ctl = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                continue
            if ctl.get("type") == "resize":
                try:
                    _set_winsize(master, ctl["cols"], ctl["rows"])
                except (KeyError, TypeError, ValueError, OSError):
                    pass

    out_task = asyncio.create_task(_drain(ws, out_q))
    in_task = asyncio.create_task(pump_input())
    # Revocation is a live fact on this socket: the watcher resolves the
    # moment the device is revoked, and FIRST_COMPLETED makes that a close
    # mid-keystroke — a revoked phone must not keep typing into a terminal.
    rev_task = asyncio.create_task(devices.wait_revoked(device_id))
    code, reason = 1000, "detached"
    try:
        done, _ = await asyncio.wait({out_task, in_task, rev_task},
                                     return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            t.exception()  # a dead peer ends a pump by raising; that's normal
        if rev_task in done:
            code, reason = 4401, "device revoked"
        elif out_task in done:
            try:
                ordered = out_task.result()
            except Exception:  # noqa: BLE001 — dead peer mid-send
                ordered = None
            # An ordered close (4412) carries its own disposition; EOF asks
            # tmux whether this was a detach or the session ending.
            code, reason = ordered if ordered else _eof_disposition(sid)
    finally:
        attach.unregister(att)
        notify.unmark_attached(sid)
        for t in (out_task, in_task, rev_task):
            t.cancel()
        try:
            loop.remove_reader(master)
        except (OSError, ValueError):
            pass
        os.close(master)
        await _reap(pid)
        await _end(ws, code, reason)
