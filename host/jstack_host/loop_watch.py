"""Measure the loop that carries every terminal, and name what stalls it.

Every keystroke and repaint rides one asyncio loop in this process (`pty.py`
reads the master fd with `add_reader`). Anything that holds it — a sync call in
a coroutine, a C call keeping the GIL, a fork from the loop thread — freezes
every attached terminal at once, and from the device it looks like the network.

A heartbeat on the loop stamps each tick; a watchdog thread off the loop sees
the stamp go stale past `STALL` and captures every thread's stack *during* the
stall (afterwards the culprit has returned and names nothing). Each stall is one
line in `<state>/logs/loop_stalls.jsonl`; `stats()` serves `/host/loop`.
"""

import asyncio
import json
import os
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path

# The heartbeat pace. Fine enough that a stall is measured to ~this, cheap
# enough to be nothing: one callback per tick.
BEAT = 0.05
# A loop held this long is a stall a person feels at the keyboard.
STALL = 0.25
# A stall's stacks are re-captured at this pace while it lasts, so a long one
# shows whether one call held it or several took turns.
RESAMPLE = 1.0
# The log is capped and rotated once; it is a diagnostic, not a history.
MAX_BYTES = 4 * 1024 * 1024
# The GIL handoff interval. At CPython's 5ms default, an I/O-bound loop thread
# waits up to 5ms per syscall whenever any worker computes (board scan,
# transcript parse) — measured p50 28ms / max 151ms echo latency under two busy
# threads; 0.5ms gave p50 3ms / max 10ms.
SWITCH = 0.0005
WINDOW = 600.0

_beat = 0.0
_loop_thread: "int | None" = None
_started = False
_gen = 0
_recent: "deque[tuple[float, float]]" = deque(maxlen=2000)
_lock = threading.Lock()
_path: "Path | None" = None


def _log_path() -> Path:
    from . import hostenv
    d = hostenv.state_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / "loop_stalls.jsonl"


def _stacks() -> dict:
    """Every thread's Python stack, the loop thread first and marked."""
    names = {t.ident: t.name for t in threading.enumerate()}
    out = {}
    frames = sys._current_frames()
    for ident in sorted(frames, key=lambda i: i != _loop_thread):
        if ident == threading.get_ident():
            continue
        label = ("LOOP " if ident == _loop_thread else "") + names.get(ident, str(ident))
        stack = traceback.extract_stack(frames[ident])
        # Idle pool workers all look alike; keep the ones doing something.
        if ident != _loop_thread and stack and stack[-1].name in ("wait", "_worker", "get", "select"):
            continue
        out[label] = [f"{f.filename.split('site-packages/')[-1]}:{f.lineno} {f.name}" for f in stack[-25:]]
    return out


def _write(record: dict) -> None:
    global _path
    try:
        if _path is None:
            _path = _log_path()
        if _path.exists() and _path.stat().st_size > MAX_BYTES:
            os.replace(_path, _path.with_suffix(".jsonl.1"))
        with open(_path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


async def _heartbeat() -> None:
    global _beat
    while True:
        _beat = time.monotonic()
        await asyncio.sleep(BEAT)


def _watch(gen: int) -> None:
    while gen == _gen:
        time.sleep(BEAT)
        held = time.monotonic() - _beat
        if held < STALL:
            continue
        began = _beat
        samples = [{"at_ms": round(held * 1000), "stacks": _stacks()}]
        next_sample = time.monotonic() + RESAMPLE
        while _beat == began and gen == _gen:
            time.sleep(0.01)
            if time.monotonic() >= next_sample:
                samples.append({"at_ms": round((time.monotonic() - began) * 1000),
                                "stacks": _stacks()})
                next_sample += RESAMPLE
        stall = _beat - began - BEAT
        if gen != _gen or stall < STALL:
            continue
        with _lock:
            _recent.append((time.time(), stall))
        _write({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "stall_ms": round(stall * 1000),
                "samples": samples})


def start() -> None:
    """Arm both halves on the running loop. Idempotent; call from startup."""
    global _started, _loop_thread, _beat, _gen
    if _started:
        return
    _started = True
    _gen += 1
    _loop_thread = threading.get_ident()
    sys.setswitchinterval(SWITCH)
    _beat = time.monotonic()
    asyncio.get_running_loop().create_task(_heartbeat())
    threading.Thread(target=_watch, args=(_gen,), name="loop-watch", daemon=True).start()


def stats() -> dict:
    """Stalls in the last `WINDOW` seconds: count, worst, total held, the list."""
    now = time.time()
    with _lock:
        recent = [(t, s) for t, s in _recent if now - t <= WINDOW]
    return {
        "armed": _started,
        "window_s": WINDOW,
        "stall_threshold_ms": round(STALL * 1000),
        "stalls": len(recent),
        "worst_ms": round(max((s for _, s in recent), default=0) * 1000),
        "held_ms": round(sum(s for _, s in recent) * 1000),
        "recent": [{"ts": time.strftime("%H:%M:%S", time.localtime(t)), "ms": round(s * 1000)}
                   for t, s in recent[-20:]],
    }
