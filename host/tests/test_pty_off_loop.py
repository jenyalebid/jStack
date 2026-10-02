"""Nothing on the keystroke path waits on tmux from the loop thread.

Every attached terminal shares one event loop. A `tmux list-sessions` fork
asked per keystroke on it froze them all for up to 1.1s whenever the Mac was
busy — caught by loop_watch on 2026-10-02 at `pty.py pump_input ->
managed.open_registry`. The attach check, the engine lookup and the EOF
disposition all ask tmux; each must run in a worker thread, and the engine
lookup only once per connection.
"""

import asyncio
import os
import subprocess
import threading

from jstack_host import attach, devices, managed, notify
from jstack_host import pty as jpty

SID = "0f0e0d0c-0b0a-4908-8706-050403020100"


class _Socket:
    client = None
    headers = {}

    def __init__(self, frames):
        self._frames = list(frames)

    async def accept(self):
        pass

    async def receive(self):
        await asyncio.sleep(0)
        if self._frames:
            return {"type": "websocket.receive", "bytes": self._frames.pop(0)}
        return {"type": "websocket.disconnect"}

    async def send_bytes(self, data):
        pass

    async def send_text(self, data):
        pass

    async def close(self, code=1000, reason=""):
        pass


def test_keystrokes_never_ask_tmux_on_the_loop(monkeypatch):
    asked = []
    loop_thread = []

    def recorded(name, result):
        def call(*_args):
            asked.append((name, threading.get_ident()))
            return result
        return call

    child = subprocess.Popen(["/bin/sleep", "30"])
    master, slave = os.openpty()
    os.set_blocking(master, False)

    async def never(_device):
        await asyncio.Event().wait()

    monkeypatch.setattr(jpty, "_authorized", lambda ws: "device")
    monkeypatch.setattr(jpty, "_ensure_managed", recorded("ensure", None))
    monkeypatch.setattr(jpty, "_eof_disposition", recorded("eof", (1000, "detached")))
    monkeypatch.setattr(jpty, "_spawn_attach", lambda sid, cols, rows: (child.pid, master))
    monkeypatch.setattr(managed, "open_registry", recorded("registry", {SID: {"engine": "claude"}}))
    monkeypatch.setattr(devices, "wait_revoked", never)
    monkeypatch.setattr(notify, "mark_attached", lambda sid: None)
    monkeypatch.setattr(notify, "unmark_attached", lambda sid: None)

    async def run():
        loop_thread.append(threading.get_ident())
        await jpty.pty_ws(_Socket([b"a", b"b", b"c", b"\r"]), SID)

    try:
        asyncio.run(run())
    finally:
        os.close(slave)
        child.kill()
        child.wait()
        attach._live.clear()

    assert [name for name, _ in asked].count("registry") == 1
    assert "ensure" in [name for name, _ in asked]
    assert all(thread != loop_thread[0] for _, thread in asked), asked
