"""The standalone jRemote host — this package, serving itself, no dashboard.

    python3 -m jstack_host.server            # 127.0.0.1:9090
    python3 -m jstack_host.server --host 0.0.0.0 --port 9090

On this Mac the host API is mounted into the dashboard's FastAPI process and
that does not change. This module is the same API on a machine that has no
dashboard to mount it into — any Mac that is an instance and nothing more.

**It is not a second implementation.** It mounts the same two routers the
dashboard mounts and runs the same background work the dashboard's startup
hooks run, because a host that serves the board without reconciling it, or
without an indexer, is a host whose board is wrong within the hour. What it
deliberately does not carry is the dashboard's own machinery — pipeline
recovery, the cookie-auth middleware, every HTML page — none of which a phone
or a Mac app ever asks for.

The lifecycle, and why each piece is here:

- `managed.reconcile()` once at startup — reaps sessions whose pane no longer
  runs an engine, so a host that was killed mid-flight comes up honest.
- `store.start_indexer()` — the SQLite session index that every board read is
  served from. Without it the board is frozen at whatever the last process to
  index left behind.
- `board_watch` + `notify_watch` — done-processing push notifications.
- `feed.start_indexer()` — the day feed's commit/run index, so the feed screen
  serves a current day rather than a stale one.

Every one is guarded exactly the way the dashboard guards them: a failure in
any of them must not stop the host from coming up. A jRemote host that will not
start is a Mac the user cannot reach; a jRemote host with a cold indexer is a Mac
The user can reach and then fix.

Auth is unchanged — the same bearer token dependency on every route, reading
`hostenv.token_path()`. A host with no token file rejects everything, which is
the right default for a process whose whole job is driving agent sessions.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
from pathlib import Path

from fastapi import FastAPI

from . import hostenv

# The routers are imported inside `create_app`, not here. Importing them pulls
# in every module that binds a state path as a module constant, so a top-level
# import would freeze the state dir before `main()` has had the chance to honour
# `--state-dir`. hostenv itself is safe: it reads the environment when asked.

def _log(msg: str) -> None:
    print(f"jremote-host: {msg}", flush=True)


def acquire_lock():
    """One host per state dir, enforced before anything starts.

    Two jRemote hosts sharing a state dir is not a slow degradation, it is two
    `reconcile` passes tearing down each other's sessions and two indexers
    writing one SQLite file. On this Mac the dashboard is already such a host,
    and once the Mac app
    can install a host extension from Settings the second one is a click away —
    so the check has to live here, not in a README.

    An advisory `flock` on a file in the state dir, held for the life of the
    process. flock and not a pid file: the lock dies with the process whatever
    kills it, so a `kill -9` leaves nothing stale behind to clear by hand. The
    pid is written *inside* for the error message only, never read to decide.

    Returns the open file handle, which the caller must keep — letting it be
    garbage-collected closes it and drops the lock.
    """
    import fcntl
    path = hostenv.state_dir() / "host.lock"
    fh = open(path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.seek(0)
        holder = fh.read().strip() or "unknown pid"
        fh.close()
        raise SystemExit(
            f"jremote-host: another host is already serving {hostenv.state_dir()} "
            f"({holder}).\n"
            f"  On the dashboard Mac that is the dashboard itself — the host API is\n"
            f"  already mounted there at :9090 and this process is not needed.\n"
            f"  To run a second host anyway, give it its own state:\n"
            f"    --state-dir ~/.local/state/jremote-alt"
        )
    fh.seek(0)
    fh.truncate()
    fh.write(f"pid {os.getpid()}")
    fh.flush()
    return fh


def _provisioned() -> bool:
    """Whether anything can authenticate here — `devices.provisioned()` owns
    the predicate; this stays as the name `/api/health` and startup read."""
    from . import devices
    return devices.provisioned()


def _raise_fd_limit() -> str:
    """Lift the soft descriptor limit to what the kernel already allows.

    A launchd agent inherits launchd's default of 256 open files — a
    terminal's budget, not a server's. Every device connection, WebSocket,
    tmux pipe, transcript read and sqlite handle is one, and the board alone
    is polled several times a second. At the ceiling the listener's accept()
    fails with EMFILE and every client sees a host that is up and answers
    nothing. The soft limit is the process's own to raise, up to the hard
    limit: no privilege, no system setting. Returns what it ended at, for the
    startup log.
    """
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = 8192 if hard == resource.RLIM_INFINITY else min(8192, hard)
        if soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
        return str(resource.getrlimit(resource.RLIMIT_NOFILE)[0])
    except (ImportError, ValueError, OSError) as e:      # noqa: BLE001
        return f"unchanged ({type(e).__name__}: {e})"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """Everything the host needs running, none of it able to stop it starting.

    Lifespan rather than `@app.on_event`: the dashboard still uses the old
    decorators and takes a DeprecationWarning per hook for it. New code has no
    reason to inherit that.
    """
    hostenv.ensure_state_dir()
    _log(f"profile={hostenv.profile().name} state={hostenv.state_dir()}")
    _log(f"open-file limit {_raise_fd_limit()}")
    if not _provisioned():
        # Loud, and still serving. The API fails closed on every request, so
        # this is not a security hole — it is a host that will answer 401 to
        # everything until somebody writes the file or mints a device, and the
        # one place that can say so is right here.
        _log(f"NO TOKEN at {hostenv.token_path()} and no device rows — "
             "every request will 401")

    # The host's own credential, reconciled before anything can ask for it.
    #
    # `internal_token()` already repairs a plaintext file that has drifted from
    # its row — but it is only ever reached lazily, from showdoc and spawn. A
    # host whose file went stale therefore stayed stale until somebody happened
    # to open a document, and the menu bar, which reads that file directly and
    # has no way to trigger the repair, sat on "No Access" over a host that was
    # up and one call from fixing itself. Reinstalling did not help: install.sh
    # touches neither the row nor the file.
    #
    # Startup is the one moment that happens on every install and every reboot,
    # so it is where the two halves get put back in agreement. A revoked row is
    # left revoked — that is a person saying no, and honouring it is the whole
    # reason `internal_token()` returns "" instead of minting.
    try:
        from . import devices
        if devices.internal_token():
            _log("internal credential reconciled")
        else:
            _log("internal credential is revoked — not re-minting")
    except Exception as e:                                 # noqa: BLE001
        _log(f"internal credential skipped ({type(e).__name__}: {e})")

    try:
        from . import managed
        for sid in managed.reconcile():
            _log(f"reaped dead session {sid}")
    except Exception as e:                                 # noqa: BLE001
        _log(f"reconcile skipped ({type(e).__name__}: {e})")

    try:
        from . import store
        store.start_indexer()
        _log("store indexer running")
    except Exception as e:                                 # noqa: BLE001
        _log(f"store indexer skipped ({type(e).__name__}: {e})")

    # The day feed needs its index kept current — the same module the router
    # serves the screen from, so the indexer and the screen are never two
    # different feeds.
    try:
        from . import feed
        feed.start_indexer()
        _log("feed indexer running")
    except Exception as e:                                 # noqa: BLE001
        _log(f"feed indexer skipped ({type(e).__name__}: {e})")

    tasks: list[asyncio.Task] = []
    try:
        from . import board_watch, notify_watch
        board_watch.add_consumer(notify_watch.observe)
        await board_watch.ensure_running()
        _log("notify watch armed")
    except Exception as e:                                 # noqa: BLE001
        _log(f"notify watch skipped ({type(e).__name__}: {e})")

    _log("ready")
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t


def create_app() -> FastAPI:
    from .pty import ws_router
    from .router import grant_router, router, unauthenticated_router

    app = FastAPI(title="jRemote host", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(router)
    # Enrolment redemption, gated on the one-time code instead of a bearer —
    # and a standalone host needs it more than the dashboard does, because a
    # leaf is exactly the machine that can never be on the hub's LAN.
    app.include_router(unauthenticated_router)
    # Delegated minting, gated on a grant this machine issued to a named parent.
    # A managed host is the one that needs it; it mounts everywhere because a
    # host cannot be told at install time which role it will end up in.
    app.include_router(grant_router)
    app.include_router(ws_router)

    @app.get("/api/health")
    def health():
        """Unauthenticated, and deliberately so.

        This is what the app probes to decide whether a host is up, and — for
        local-first routing — whether the Mac it is running on *is* a host. A
        probe that needed the token could not answer "is anyone there" before
        The user has entered one, which is exactly when the app needs to know.

        It says what kind of host this is and nothing about what is on it: no
        agent names, no session ids, no paths. The board is behind the token.
        """
        from . import managed_access
        return {
            "ok": True,
            "service": "jremote-host",
            "standalone": True,
            "profile": hostenv.profile().name,
            "provisioned": _provisioned(),
            "managed": managed_access.is_leaf(),
        }

    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m jstack_host.server",
        description="Serve the jRemote host API on this machine.")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (default 127.0.0.1; use 0.0.0.0 to "
                         "accept from the LAN or a tunnel)")
    ap.add_argument("--port", type=int, default=9090,
                    help="bind port (default 9090 — the same port the app "
                         "already tries locally, so local-first routing is "
                         "one rule on every instance)")
    ap.add_argument("--state-dir", default=None,
                    help="override where this host keeps its state "
                         "(equivalent to JREMOTE_STATE_DIR)")
    args = ap.parse_args(argv)

    if args.state_dir:
        # Set before create_app, because the modules that name a file in the
        # state dir bind it as a module constant when they are imported.
        os.environ["JREMOTE_STATE_DIR"] = str(Path(args.state_dir).expanduser())

    try:
        import uvicorn
    except ImportError:
        print("uvicorn is required to serve: pip install uvicorn", file=sys.stderr)
        return 1

    # Before create_app, and well before the lifespan reconciles anything: the
    # point of the lock is to stop the second host *acting*, and by the time
    # uvicorn is up it already has. Held in a local for the life of the call —
    # the handle is the lock.
    hostenv.ensure_state_dir()
    _lock = acquire_lock()                                     # noqa: F841

    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
