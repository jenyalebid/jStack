"""The hub's own shell, reachable from any client — not a session.

No board row, no sid, no engine, no agent identity: a login shell in a PTY,
for the one thing that IS a Mac-side problem and not an agent problem (a
stuck helper, a daemon to inspect, a file to move) — reachable exactly when
the Mac itself is what's wedged.

One named tmux process, single per hub, started on demand. Any client may
join it; any client may close it, and closing ends it for everyone — that
is the intent, not a hazard to guard against.

Named outside `jr-` on purpose: `managed.reconcile()` reaps every
`jr-`-prefixed session whose pane isn't live in the agent scan (its own
docstring names "a bare shell" as precisely that debris). A login shell
under that prefix would be silently closed on every dashboard restart past
the 60s grace — staying off the prefix keeps this invisible to the reaper
by construction, not by a special case.

Protocol, the `pty.py` sibling: `/api/jremote/v1/hub/shell` upgrades to a
WebSocket, ensures the session, spawns a `tmux attach` client on the same
managed socket inside a real PTY, and pipes raw bytes both ways.

  client -> server  binary frame       raw keyboard/paste bytes -> PTY stdin
  client -> server  text frame (JSON)  {"type":"resize","cols":N,"rows":N}
  client -> server  text frame (JSON)  {"type":"close"} -- end it for everyone
  server -> client  binary frame       raw PTY output (ANSI stream)
  server -> client  text frame (JSON)  {"type":"end","code":N,"reason":s}
  close codes       4401 bad/missing token · 1000 detached (session lives) ·
                    4411 closed — this client or another asked to end it
"""

import asyncio
import fcntl
import json
import os
import pty as _pty
import signal
import struct
import subprocess
import termios

from fastapi import APIRouter, Depends
from starlette.websockets import WebSocket

from . import hostenv, managed
from .auth import authenticate_ws, require_token

NAME = "hub-shell"

http_router = APIRouter(prefix="/api/jremote/v1/hub/shell",
                        dependencies=[Depends(require_token)])
ws_router = APIRouter()

_READ_CHUNK = 65536


def is_open() -> bool:
    return subprocess.run(managed._t("has-session", "-t", NAME),
                          capture_output=True).returncode == 0


def ensure() -> None:
    """Idempotent: stands the shell up if it is not already running.

    It opens at the install root, never in the host process's own cwd — that
    is wherever launchd or a shell happened to start the dashboard, a place
    nobody chose to land in."""
    if is_open():
        return
    shell = os.environ.get("SHELL", "/bin/zsh")
    env = {**os.environ, "PATH": managed._PATH}
    subprocess.run(managed._t("new-session", "-d", "-s", NAME,
                              "-c", str(hostenv.stack_root()), shell, "-l"),
                  check=True, env=env)
    # Mouse and clipboard passthrough, same as a managed session's — this may
    # be the first session this tmux server ever creates, so nothing else can
    # be relied on to have set them.
    subprocess.run(managed._t("set-option", "-g", "mouse", "on"),
                  capture_output=True, env=env)
    subprocess.run(managed._t("set-option", "-g", "set-clipboard", "on"),
                  capture_output=True, env=env)
    subprocess.run(managed._t("set-option", "-g", "status", "off"),
                  capture_output=True, env=env)


def close() -> None:
    """Ends the shell for every client, not just this one."""
    subprocess.run(managed._t("kill-session", "-t", NAME), capture_output=True)


@http_router.get("/status")
def status():
    return {"open": is_open()}


@http_router.post("/close")
def close_route():
    close()
    return {"open": is_open()}


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    cols = max(20, min(500, int(cols)))
    rows = max(5, min(300, int(rows)))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _attach_argv() -> list[str]:
    # `-T sync` is the client half of the DEC 2026 synchronized-output
    # contract the CLI panes already rely on (see `pty.py`); a bare shell
    # benefits the same way from a terminal that never paints a half-applied
    # frame.
    return managed._t("-T", "sync", "attach", "-t", NAME)


def _spawn_attach(cols: int, rows: int) -> tuple[int, int]:
    env = {
        "PATH": managed._PATH,
        "TERM": "xterm-256color",
        "TERMINFO_DIRS": os.environ.get(
            "TERMINFO_DIRS", "/usr/share/terminfo:/opt/homebrew/share/terminfo"),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "HOME": os.environ.get("HOME", ""),
    }
    argv = _attach_argv()
    pid, master = _pty.fork()
    if pid == 0:  # child -- exec or die, never return into the server
        try:
            os.execve(argv[0], argv, env)
        finally:
            os._exit(127)
    _set_winsize(master, cols, rows)
    os.set_blocking(master, False)
    return pid, master


async def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            n = os.write(fd, view)
            view = view[n:]
        except BlockingIOError:
            await asyncio.sleep(0.005)


async def _reap(pid: int) -> None:
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


async def _end(ws: WebSocket, code: int, reason: str) -> None:
    try:
        await ws.send_text(json.dumps({"type": "end", "code": code,
                                       "reason": reason}))
    except Exception:
        pass
    try:
        await ws.close(code=code, reason=reason)
    except Exception:
        pass


@ws_router.websocket("/api/jremote/v1/hub/shell")
async def hub_shell_ws(ws: WebSocket, cols: int = 80, rows: int = 24):
    await ws.accept()
    device_id = await asyncio.to_thread(authenticate_ws, ws)
    if not device_id:
        await ws.close(code=4401, reason="invalid or missing bearer token")
        return

    ensure()
    pid, master = _spawn_attach(cols, rows)
    loop = asyncio.get_running_loop()
    out_q: asyncio.Queue = asyncio.Queue()
    closing_everyone = False

    def _on_readable():
        try:
            data = os.read(master, _READ_CHUNK)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if data:
            out_q.put_nowait(("data", data))
        else:  # EOF -- the attach client exited (detached, or the shell closed)
            loop.remove_reader(master)
            out_q.put_nowait(None)

    loop.add_reader(master, _on_readable)

    async def pump_output():
        while True:
            item = await out_q.get()
            if item is None:
                return
            await ws.send_bytes(item[1])

    async def pump_input():
        nonlocal closing_everyone
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            data = msg.get("bytes")
            if data:
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
            elif ctl.get("type") == "close":
                closing_everyone = True
                return

    out_task = asyncio.create_task(pump_output())
    in_task = asyncio.create_task(pump_input())
    try:
        done, _ = await asyncio.wait({out_task, in_task},
                                     return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            t.exception()  # a dead peer ends a pump by raising; that's normal
    finally:
        for t in (out_task, in_task):
            t.cancel()
        try:
            loop.remove_reader(master)
        except (OSError, ValueError):
            pass
        os.close(master)
        await _reap(pid)
        if closing_everyone:
            close()
        code, reason = (4411, "closed") if closing_everyone else (1000, "detached")
        await _end(ws, code, reason)
