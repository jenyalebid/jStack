"""Push the board the moment it changes, instead of answering polls.

The Mac knows a window closed the instant it closes — the process is gone, the
tmux client is gone. A phone on a poll learns it a poll later, and until then
shows a session "On Mac" that isn't on the Mac. The lag was never in observing
the fact; it was in nobody telling anyone.

So the fact gets watched here and pushed:

* **One watcher, not one per client.** The observation is the expensive part
  (~150ms of psutil per pass), so every connected app shares a single pass. Two
  phones cost what one costs.
* **It runs while anyone is listening — and notifications always are.**
  SSE subscribers (phones streaming the board) come and go; registered
  consumers (the notify engine) are permanent, so with any consumer the
  watcher never stops — it just slows to `TICK_IDLE` when no phone is
  actually watching. Notifications are somebody looking: the working→idle
  edge matters most precisely when no screen is on the board.
* **Only changes go on the wire.** Snapshots are compared whole, so an idle
  board is an idle connection — no traffic, no wakeups, no battery.
* **Actions don't wait for the tick.** Anything that changes the board (a
  close, an open, a takeover) calls `poke()`, and the recompute happens on the
  next pass of the loop rather than up to `TICK` later.

The unit pushed is the **whole board**, never a delta: a client that connects
mid-stream, reconnects after a network flap, or misses a frame is correct
immediately, with no replay to get wrong.
"""

import asyncio
import contextlib
import json

# How often the board is re-observed while someone is watching. Fast enough
# that a window closing reads as instant on the phone, slow enough that the
# scan is a rounding error on a machine running agents all day.
TICK = 1.0
# The pace with no phone on the board — consumers (notifications) only need
# an edge within a few seconds, not within one.
TICK_IDLE = 3.0

_subscribers: "set[asyncio.Queue]" = set()
_consumers: "list" = []
_watcher: "asyncio.Task | None" = None
_wake: "asyncio.Event | None" = None
_loop: "asyncio.AbstractEventLoop | None" = None
_latest: "list[dict] | None" = None
_digest: "str | None" = None


def _snapshot() -> list[dict]:
    from . import board
    return board.active_sessions()


def _tick() -> float:
    """Seconds to wait before observing the board again.

    `TICK` is only worth paying while the board is what somebody is looking at.
    Two cases where it isn't:

    * **Nobody subscribed** — the original rule. Consumers (notifications) need
      an edge within a few seconds, not within one.
    * **A device is inside a terminal.** The board is behind a full-screen
      thread, and the observation is the most expensive thing this process does
      — psutil and `ps` across every process on a machine that may be mid-Xcode
      build, in the same interpreter that owes that terminal its next PTY
      frame. Paying it every second for a covered screen is how a keystroke
      ends up queued behind a process scan (#30).

    Notifications keep working throughout: `TICK_IDLE` is already what they are
    dimensioned for. Nothing that *changes* the board waits on this at all —
    `poke()` recomputes on the next pass — so the slower pace costs a stale
    read of something nobody is watching, and never a late reaction to an
    action someone took."""
    from . import attach
    if not _subscribers:
        return TICK_IDLE
    return TICK_IDLE if attach.any_live() else TICK


def poke() -> None:
    """Recompute now — an endpoint just changed the board itself.

    Callable from anywhere, including a sync endpoint running in FastAPI's
    thread pool, which is why the wake is scheduled onto the watcher's loop
    rather than set directly. With nobody watching there is no watcher to
    wake and this is a no-op."""
    loop, wake = _loop, _wake
    if loop is None or wake is None:
        return
    with contextlib.suppress(RuntimeError):   # loop closed / shutting down
        loop.call_soon_threadsafe(wake.set)


def add_consumer(fn) -> None:
    """Register a permanent in-process consumer: called with every changed
    snapshot, for as long as the process lives. Consumers keep the watcher
    running with no SSE subscribers (at `TICK_IDLE`) — the notify engine
    needs edges exactly when no phone is streaming the board."""
    if fn not in _consumers:
        _consumers.append(fn)


async def ensure_running() -> None:
    """Start the watcher if anyone — subscriber or consumer — is listening.
    Called from dashboard startup once consumers are registered; subscription()
    routes through it too so there is one start path."""
    global _watcher, _wake, _loop, _digest
    if not (_subscribers or _consumers):
        return
    _loop = asyncio.get_running_loop()
    # Both hub entry points arm this watcher at startup, on the loop the
    # terminals ride — the one place the loop watch can start from either.
    from . import loop_watch
    loop_watch.start()
    if _wake is None:
        _wake = asyncio.Event()
    if _watcher is None or _watcher.done():
        _digest = None          # a fresh watcher broadcasts its first pass
        _watcher = asyncio.create_task(_run())


async def _run() -> None:
    """Observe, compare, broadcast — while anyone is subscribed or consuming."""
    global _latest, _digest, _watcher
    try:
        while _subscribers or _consumers:
            try:
                rows = await asyncio.to_thread(_snapshot)
            except Exception as e:  # noqa: BLE001
                # A failed observation is not an empty board. Broadcasting one
                # would clear every app's screen on a transient psutil error,
                # so a bad pass is skipped and the last truth stands — loudly:
                # an invisible skip is how a lower layer's fabricated "empty"
                # board went unnoticed until it paged a phone.
                # With the traceback: the type alone ("KeyError: 'name'")
                # names the symptom and hides the line, which is the whole
                # question when the failure is rare and un-reproducible.
                import traceback
                print(f"board_watch: observation failed, tick skipped "
                      f"({type(e).__name__}: {e})\n"
                      f"{traceback.format_exc()}", flush=True)
                rows = None
            if rows is not None:
                if json.dumps(rows, sort_keys=True, default=str) != _digest:
                    # Consumers first, then the re-read, then the wire. The
                    # notify engine reads this very snapshot to decide a turn
                    # ended, and marks the session unread as it does — but it
                    # writes that to its own state, not to these rows, which
                    # were built a moment BEFORE the verdict existed. So the
                    # frame announcing "the reply landed" — new card text, a
                    # push already on its way — went out with `unread` still
                    # false, and the dot could not go orange until the next
                    # tick. A verdict belongs in the frame that caused it, so
                    # nothing goes on the wire until every writer of this
                    # frame has had its say.
                    for fn in list(_consumers):
                        # A consumer must never take the watcher down with it —
                        # the board stream is every phone's screen.
                        try:
                            fn(rows)
                        except Exception as e:  # noqa: BLE001
                            print(f"board_watch: consumer failed ({type(e).__name__}: {e})",
                                  flush=True)
                    _refresh_unread(rows)
                    _digest = json.dumps(rows, sort_keys=True, default=str)
                    _latest = rows
                    for q in list(_subscribers):
                        _offer(q, rows)
            try:
                await asyncio.wait_for(_wake.wait(), timeout=_tick())
            except asyncio.TimeoutError:
                pass
            else:
                _wake.clear()
    finally:
        # Cleared without an await between the loop test and here, so a
        # subscriber arriving at any point either finds a watcher that will
        # see it on its next pass, or none at all and starts one.
        _watcher = None


def _refresh_unread(rows: list[dict]) -> None:
    """Re-read the unread marks the consumers may have just moved.

    `unread` is server state, and the notify engine writes it from the same
    snapshot the phones are about to be handed: a done edge in this pass marks
    the session, a turn opening clears it. Re-reading costs one in-memory set
    and puts the orange dot in the frame that earned it instead of the next
    one — the difference between the push and the dot agreeing and the dot
    trailing it by a tick.

    Read exactly as the board reads it, so this can only ever agree with what
    the next pass will build. A read that fails changes nothing: the rows as
    built are still true, just one edge old."""
    from . import notify
    try:
        unread = notify.unread_sids()
    except Exception as e:  # noqa: BLE001 — a blind read must not rewrite rows
        print(f"board_watch: unread re-read failed ({type(e).__name__}: {e})",
              flush=True)
        return
    for row in rows:
        # Only rows the board gave an unread state to. Which rows have one is
        # the board's call, not this pass's — inventing the key here would
        # hand the app a field its builder deliberately left off.
        if "unread" in row:
            row["unread"] = row.get("session_id", "") in unread


def _offer(q: "asyncio.Queue", rows: list[dict]) -> None:
    """Hand a subscriber the newest board, replacing anything it hasn't read.

    A slow client must never make the watcher grow a backlog: stale boards have
    no value once a newer one exists, so the queue holds at most the latest."""
    while not q.empty():
        with contextlib.suppress(asyncio.QueueEmpty):
            q.get_nowait()
    with contextlib.suppress(asyncio.QueueFull):
        q.put_nowait(rows)


@contextlib.asynccontextmanager
async def subscription():
    """Yield a queue that receives a board snapshot on every change.

    The first frame arrives immediately — the last observed board if a watcher
    is already running (unchanged means still true), otherwise the watcher's
    own first pass — so a connecting app never waits for something to change
    before it can paint."""
    q: "asyncio.Queue" = asyncio.Queue(maxsize=1)
    _subscribers.add(q)
    already_running = _watcher is not None and not _watcher.done()
    await ensure_running()
    if already_running and _latest is not None:
        _offer(q, _latest)
    try:
        yield q
    finally:
        _subscribers.discard(q)
        if not _subscribers and _wake is not None:
            _wake.set()         # let the watcher notice it has no audience
