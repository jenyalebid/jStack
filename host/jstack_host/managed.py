"""Managed (drivable) terminal sessions via tmux.

A session becomes drivable when its `claude` runs inside a tmux session keyed by
its sid. Client input → the PTY WebSocket / `tmux send-keys` → the one native
`claude` reads it as stdin. Single process (no two-writer), local socket (no
Apple Events / TCC).

## The board invariant

**A managed session exists exactly as long as its `claude` runs, and it is on
the board the whole time.** Every client — the phone, the Mac app, an iTerm
window — is a mirror that attaches and detaches freely; none of them owns the
session's life. Visibility is the board: creation is registered-first
(`record_open` lands the sid before `claude` starts, so a fresh session is a
named board row from its first instant), and the board is pushed to every
device. That — not a window per session — is what lets the user see anything a
remote caller starts here.

Life is bound to the process, tmux-native: the pane `exec`s `claude`, so when
`claude` exits — `/exit`, a crash, a kill — the pane dies and the tmux server
destroys the session with it. There is no windowless zombie to sweep and no
client-count bookkeeping: zero attached clients is a perfectly healthy idle
chat.

A session ends exactly two ways: `close_managed()` (the app's Close/Kill, or
any host caller), or `claude` exiting on its own. Detaching — closing an iTerm
window included — ends nothing.

## iTerm is a viewer, not life support

`launch_terminal()` attaches an iTerm window on demand (the app's Open in
iTerm, the desk-side spawn's window). The phone/app PTY client is tagged
(`JREMOTE_PHONE_CLIENT`) and excluded from `attached_names()`, so "an iTerm
window is showing this" stays an honest, observable display fact — it just no
longer decides whether the session may exist. Desk-side spawns (handoff,
splitoff) still request a window at creation (`open_managed(window=True)`),
because their product IS a new terminal on the desk; that path stays
transactional (`WindowRequired` ⇒ the fresh session was torn down).

`reconcile()` runs at dashboard startup: it disarms the legacy
`destroy-unattached` option on sessions created under the old window invariant
(which would otherwise die on their next detach), and reaps sessions whose
pane no longer runs a `claude` (a lingering shell from before pane-exec, or a
short-circuited launch).

Nothing else ends a session on a timer. Every autonomous path on this machine
is `claude --print` — the scheduler's wakes and resume-forks, the assistant
daemon, the relay, scribe — and a `--print` process exits when its turn ends,
holding no tmux at all. A managed session therefore exists only because a hand
made it: a board tap, a phone attach, Open in Terminal, New, splitoff,
displace, or a handoff opening a terminal for the user. A sweep over those can only
ever close a window someone opened on purpose, so there isn't one.

Ending a session still closes any iTerm windows attached to it
(`close_windows`) — a window left behind at a dead prompt looks like work and
isn't, and the desk stops matching the app.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .hostenv import spawn_path
from . import hostenv

def _bundled_tmux() -> str | None:
    """The tmux the Hub shipped, next to its own Python. A clean Mac has no
    tmux on PATH, so a managed session can only spawn if we use the one in the
    bundle — sibling of sys.executable (…/Contents/MacOS/tmux). None when this
    host is not running from the sealed bundle (a dev/venv run), where
    which()/Homebrew is the right answer."""
    try:
        exe = Path(sys.executable)
        for base in (exe.parent, exe.resolve().parent):
            cand = base / "tmux"
            if cand.is_file():
                return str(cand)
    except (OSError, ValueError):
        pass
    return None


_TMUX = _bundled_tmux() or shutil.which("tmux") or "/opt/homebrew/bin/tmux"
_REG = hostenv.state_dir() / "jremote_open.json"
# Dedicated tmux socket, shared by the daemon and iTerm. Overridable so tests
# can exercise the real teardown paths against a throwaway server instead of
# The user's live sessions.
_SOCK = os.environ.get("JREMOTE_TMUX_SOCK", "jremote")
_PATH = spawn_path()

# Set in the environment of the phone's `tmux attach` client (pty.py) so window
# truth can tell a mirror from a window. Read back off the live client process,
# not held in memory, so a dashboard restart can't lose the distinction and
# silently promote a stranded phone client into a "window".
PHONE_CLIENT_ENV = "JREMOTE_PHONE_CLIENT"

# How long a fresh session waits for its Mac window before it is destroyed.
# Generous: iTerm's API handshake alone can take ~15s on a cold app.
WINDOW_TIMEOUT = 30.0


class WindowRequired(RuntimeError):
    """No terminal window could be opened on the Mac, so no session may exist."""


class TakeoverFailed(RuntimeError):
    """The claude already holding this transcript would not let go, so the
    managed session that was standing up for it was torn back down."""


def _t(*args) -> list[str]:
    return [_TMUX, "-L", _SOCK, *args]


def _type_argv(name: str, text: str) -> list[list[str]]:
    """The tmux calls that type `text` into `name`'s input box — as written.

    One `send-keys -l "$text"` gets two things wrong, and both are silent:

    * **A newline is dropped, not typed.** Both TUIs read a bare LF as
      nothing, so "one\\ntwo" lands as "onetwo" — which is how a share-sheet
      comment and the path it was shared with arrived glued into one word.
      `M-Enter` (ESC CR) is the insert-a-newline key both claude and codex
      honour; a bare `Enter` would submit the half-typed message instead.
    * **A line starting with `-` is parsed as a flag.** tmux answers "unknown
      flag" and types *nothing* — one dash at the front of a message, or of
      any line in a bulleted one, and the whole send vanishes. `--` ends
      option parsing.

    Verified against both CLIs in a throwaway tmux server (2026-08-26): LF
    glues, M-Enter renders a real second line, and the submitted transcript
    carries the "\\n".
    """
    argv: list[list[str]] = []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for i, line in enumerate(lines):
        if i:
            argv.append(_t("send-keys", "-t", name, "M-Enter"))
        if line:
            argv.append(_t("send-keys", "-t", name, "-l", "--", line))
    return argv


def _name(sid: str) -> str:
    return "jr-" + sid[:8]


def compose_shim() -> Path:
    """The compose editor shim, beside this package.

    One answer, exported, because the test that pins the shim into `VISUAL`
    used to compute the path a second time — and computed it from a different
    starting point. Both were right while the package sat two levels inside a
    larger tree; after the move the module found the shim and the test looked
    for it one directory up, so the suite failed against a working feature.
    A path two places derive independently is a path they will eventually
    disagree about.
    """
    return Path(__file__).resolve().parent / "bin" / "jremote-compose-editor"


def _compose_exports(sid: str) -> str:
    """The env that lets compose lift the input box out whole (`composer.py`).

    `VISUAL`, not `EDITOR`: the CLI resolves VISUAL first, and this machine's
    `~/.claude/settings.json` already sets `EDITOR=cot -w` for the user's own
    ctrl+G — which must keep working exactly as it does. VISUAL wins for
    managed panes only, and the shim execs the fallback for any file that is
    not an agent prompt, so `git commit` in this pane is unaffected.
    """
    from .composer import compose_dir
    shim = compose_shim()
    if not shim.exists():
        return ""
    return (f"export VISUAL={shlex.quote(str(shim))}; "
            f"export JREMOTE_SID={shlex.quote(sid)}; "
            f"export JREMOTE_COMPOSE_DIR={shlex.quote(str(compose_dir()))}; ")


def open_names() -> set[str]:
    """Names of all managed tmux sessions — one call, for board enrichment."""
    r = subprocess.run(_t("list-sessions", "-F", "#{session_name}"),
                       capture_output=True, text=True)
    if r.returncode != 0:
        return set()
    return {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}


def _is_phone_client(pid: str) -> bool:
    """Is this tmux client the phone's PTY mirror rather than a Mac window?

    Read off the live process's environment, not a table this process keeps,
    so a dashboard restart can never lose track of a client it forked and
    then count it as a window.

    Unknown answers say "window". Nothing's life depends on this call — it
    decides the board's honest "an iTerm window is showing this" display fact,
    and which ttys `close_windows` closes when a session ends."""
    try:
        import psutil
        return psutil.Process(int(pid)).environ().get(PHONE_CLIENT_ENV) == "1"
    except Exception:
        return False


def attached_names() -> set[str]:
    """Names of managed sessions a **Mac window** is displaying right now.

    This is the board's window truth and the invariant's test, so the phone's
    own `tmux attach` is deliberately excluded: a mirror on the phone is not an
    indication on the desk, and treating it as one would let a remote caller
    hold a session open with nothing on screen here."""
    r = subprocess.run(
        _t("list-clients", "-F", "#{client_session}\t#{client_pid}"),
        capture_output=True, text=True)
    if r.returncode != 0:
        return set()
    names = set()
    for ln in r.stdout.splitlines():
        name, _, pid = ln.partition("\t")
        if name.strip() and not _is_phone_client(pid.strip()):
            names.add(name.strip())
    return names


def has_window(sid: str) -> bool:
    """Is a Mac terminal window displaying this session's managed tmux?"""
    return _name(sid) in attached_names()


def client_ttys(sid: str) -> list[str]:
    """The ttys of the Mac terminal windows attached to this session.

    What `close_windows` needs to close the windows a session was living in,
    collected **before** the teardown — once the tmux session is gone tmux has
    no clients to name, and the windows would be left behind for good.

    The phone's own client is excluded for the same reason it is excluded from
    window truth: it is a mirror, not a window on the desk (and its forked PTY
    is no iTerm session anyway)."""
    r = subprocess.run(
        _t("list-clients", "-t", _name(sid), "-F", "#{client_tty}\t#{client_pid}"),
        capture_output=True, text=True)
    if r.returncode != 0:
        return []
    out = []
    for ln in r.stdout.splitlines():
        tty, _, pid = ln.partition("\t")
        if tty.strip() and not _is_phone_client(pid.strip()):
            out.append(tty.strip())
    return out


def close_windows(ttys) -> None:
    """Close the Mac terminal windows on these ttys — best effort, never fatal.

    The other half of the invariant: closing a window ends its session, so
    ending a session closes its window. Without this, killing from the phone
    leaves an empty prompt sitting on the desk that looks like work and isn't.

    Failure here is cosmetic (a window outlives its session) and must never
    turn a successful close into a failed one, so every error is logged and
    swallowed."""
    ttys = sorted({t for t in (ttys or []) if t})
    if not ttys:
        return
    infra = hostenv.package_root()
    try:
        r = subprocess.run([sys.executable, "-m", "jstack_host.iterm_window", "--close", *ttys],
                           cwd=str(infra), capture_output=True, timeout=20)
        if r.returncode != 0:
            _log("closing the window(s) on %s failed: %s" %
                 (", ".join(ttys),
                  r.stderr.decode(errors="replace").strip()[:300] or "<no stderr>"))
    except (OSError, subprocess.SubprocessError) as e:
        _log(f"closing the window(s) on {', '.join(ttys)} failed: {type(e).__name__}: {e}")


def _wait_for_window(sid: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if has_window(sid):
            return True
        time.sleep(0.25)
    return False


def pane_ttys() -> dict[str, str]:
    """{pane_tty: session_name} for every pane on the managed socket.

    The tty a `claude` inside tmux holds is the PANE's, allocated by the tmux
    server — it exists whether or not any window displays it, and it survives
    every iTerm window close. So a pane tty proves a tmux session, never a
    window; only `attached_names()` proves a window. This map is what lets the
    board tell the two apart instead of reading a pane tty as a window."""
    r = subprocess.run(_t("list-panes", "-a", "-F", "#{pane_tty}\t#{session_name}"),
                       capture_output=True, text=True)
    if r.returncode != 0:
        return {}
    out = {}
    for ln in r.stdout.splitlines():
        tty, _, name = ln.partition("\t")
        if tty.strip() and name.strip():
            out[tty.strip()] = name.strip()
    return out


# ── Open-session registry (sid → agent), so a freshly-opened session shows on
#    the board immediately, before its JSONL exists. Written by two processes
#    (the dashboard and the desk-side spawn CLI), so mutation takes an flock
#    and the save is an atomic replace — a reader never sees a torn file. ──

def _reg_load() -> dict:
    """The registry as a dict — always, whatever is actually on disk.

    Missing, unreadable or not-JSON has always read as empty. Valid JSON of the
    wrong *shape* is the same corruption and gets the same answer, because the
    alternative is each caller raising its own unrelated error far from the
    cause: `.items()` in `open_registry`, a setitem in `record_open`, a
    `list.pop(str)` TypeError in `reconcile`. That last one runs at dashboard
    startup, so one malformed file takes the whole reconcile pass down with it.

    Said out loud, though — unlike a missing file, a wrong-shaped one means
    something wrote nonsense here, and returning empty silently would report
    "no open sessions" for what is really "the registry is broken"."""
    try:
        d = json.loads(_REG.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(d, dict):
        _log(f"open registry {_REG} holds a {type(d).__name__}, not an object "
             "— reading as empty; the next record_open rewrites it")
        return {}
    return d


def _reg_save(d: dict) -> None:
    _REG.parent.mkdir(parents=True, exist_ok=True)
    tmp = _REG.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    os.replace(tmp, _REG)


def _reg_mutate(fn) -> None:
    """Read-modify-write under an exclusive lock — the spawn CLI and the
    dashboard both record here, and an unlocked pair loses whichever write
    lands first."""
    import fcntl
    _REG.parent.mkdir(parents=True, exist_ok=True)
    with open(_REG.with_suffix(".lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            d = _reg_load()
            fn(d)
            _reg_save(d)
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def record_open(sid: str, agent: str, name: str = "",
                engine: str = "claude", model: str = "",
                tag: str = "") -> None:
    """`name` is the spawn's display title ("Handoff · X") — what the board
    shows for the row until the session's first message gives it a preview.

    `engine` is recorded because the spawn is the only place that reliably
    knows it. The process scan can name an engine from a live pid, but a Codex
    session writes no Claude transcript, so it reaches the board through this
    registry — and a row that cannot say which CLI it runs is a row the app
    cannot label. Written only when it is not the default, so existing
    registry entries keep their exact shape and read as claude.

    `model` is recorded for the same reason and one more: it is the ONLY
    record of it. The model is passed on the command line and then lives
    nowhere a later reader can reach — Claude's transcript names the model per
    message, but a session with no turns yet has no transcript, and Codex
    writes none we parse at all. Without this, a row spawned on Sonnet is
    indistinguishable from one spawned on Opus for as long as it sits idle,
    which is exactly when the user is deciding whether to type into it.

    `tag` is the subject the session was opened on, and it is recorded for a
    third reason on top of those two: two rows for the same seat are otherwise
    identical on the board, and which subject each one is sitting on is the
    only thing that tells them apart.

    **Those three CARRY OVER a re-registration; everything else is replaced.**
    Every reopen path — board tap, PTY attach, displace, fork — registers again
    with nothing but a sid and an agent, because that is all a reopen knows. A
    plain overwrite would drop the engine (so a reopened Codex row relabels
    itself claude), the model, and the pin — three facts the spawn is the only
    witness to and no later reader can recover. They are properties of the
    SESSION, not of this registration, so a caller that says nothing about them
    is not asking to clear them. A caller that passes one still wins, which is
    how a session gets re-pinned to a different subject."""
    def _put(d: dict) -> None:
        prior = d.get(sid) or {}
        entry = {"agent": agent}
        if prior.get("transcript"):
            entry["transcript"] = prior["transcript"]
        if name:
            entry["name"] = name
        eng = engine if (engine and engine != "claude") else prior.get("engine", "")
        if eng and eng != "claude":
            entry["engine"] = eng
        if model or prior.get("model"):
            entry["model"] = model or prior["model"]
        if tag or prior.get("tag"):
            entry["tag"] = tag or prior["tag"]
        d[sid] = entry

    _reg_mutate(_put)


def record_close(sid: str) -> None:
    _reg_mutate(lambda d: d.pop(sid, None))


def record_transcript(sid: str, path: str) -> None:
    """Attach an engine-owned transcript to its registered board handle."""
    def mutate(d: dict) -> None:
        if sid in d:
            d[sid]["transcript"] = path
    _reg_mutate(mutate)


def reopen_target(sid: str) -> dict | None:
    """What it takes to bring a session that is not open back up:
    `{cwd, agent, engine, resume_id}` — or None when nothing on disk can
    answer, which means the row is a record and not something that reopens.

    Every reopen path used to inline the Claude half of this and only that:
    walk `~/.claude/projects` for `{sid}.jsonl`. **A Codex session never
    writes one**, so every Codex reopen — the app's reattach, the desk's Open
    in Terminal — resolved to "session not found" and a closed Codex session
    was a new one rather than a resumed one.

    The two engines are asked in the order they can answer. Claude's project
    dir carries both the cwd and the seat in its name. Codex's rollout carries
    them in its own `session_meta`, and the binding from a board handle to
    that file is `store.transcript_path` — durable on purpose, because the
    open registry forgets a session the instant it ends. `resume_id` is
    Codex's own thread id, the only thing `codex resume` accepts; jRemote's
    sid stays the board handle, so the row keeps its identity across the
    reopen."""
    from .hostenv import project_dir_to_agent
    projects = Path.home() / ".claude" / "projects"
    if projects.exists():
        for pd in projects.iterdir():
            if not (pd / f"{sid}.jsonl").exists():
                continue
            from .transcripts import _project_dir_to_cwd
            cwd = _project_dir_to_cwd(pd.name)
            if not (cwd and Path(cwd).exists()):
                return None
            parsed = project_dir_to_agent(pd.name)
            return {"cwd": cwd, "agent": parsed[0] if parsed else "",
                    "engine": "claude", "resume_id": ""}

    from . import codex_transcript
    from .store import get_store
    path = get_store().transcript_path(sid)
    if not path or str(codex_transcript.root()) not in path:
        return None
    meta = codex_transcript.metadata(Path(path))
    cwd = str(meta.get("cwd") or "")
    native = str(meta.get("session_id") or meta.get("id") or "")
    if not (cwd and native and Path(cwd).exists()):
        return None
    # The seat, from the cwd — a rollout has no project-dir name to parse.
    from .hostenv import active_agents, workspace
    agent = ""
    for base in active_agents():
        try:
            Path(cwd).resolve().relative_to(workspace(base).resolve())
        except (OSError, ValueError):
            continue
        agent = base
        break
    return {"cwd": cwd, "agent": agent, "engine": "codex",
            "resume_id": native}


def open_registry() -> dict:
    """{sid: {'agent': base, 'name'?: title}} for sessions whose managed tmux
    is still alive."""
    live = {n[3:] for n in open_names() if n.startswith("jr-")}
    return {sid: info for sid, info in _reg_load().items() if sid[:8] in live}


def is_open(sid: str) -> bool:
    return subprocess.run(_t("has-session", "-t", _name(sid)),
                          capture_output=True).returncode == 0


def _disarm_destroy_unattached(sid_name: str) -> None:
    """Legacy migration: sessions created under the old window invariant had
    `destroy-unattached` armed at attach, which under the board invariant would
    kill them on their next detach. Idempotent, harmless on sessions that never
    had it set."""
    subprocess.run(_t("set-option", "-t", sid_name, "destroy-unattached", "off"),
                   capture_output=True)


def _log(msg: str) -> None:
    """Diagnostics for the window path, on stderr.

    stderr and not stdout: the daemon's stdout is block-buffered into a file
    nobody tails, so a print there is a line written months later. Every step
    that can silently cost a session says why here."""
    print(f"jremote: {msg}", file=sys.stderr, flush=True)


def _attach_window_or_die(sid: str) -> None:
    """Put a Mac window on this **freshly created** session, or destroy it.

    Only the `window=True` creation path (desk-side spawns, whose product IS a
    new terminal on the desk) comes through here: a handoff that opens nowhere
    visible is a failed handoff, so that creation stays transactional. Every
    failure — iTerm down, API consent missing, the user closing the window as it
    opens — lands in the same place: the fresh session is torn down and
    `WindowRequired` raised. Sessions that already ran are never torn down for
    lacking a window; under the board invariant a window is a viewer, not life
    support."""
    why = "no window attached within %.0fs" % WINDOW_TIMEOUT
    try:
        launch_terminal(sid)
        attached = _wait_for_window(sid, WINDOW_TIMEOUT)
    except Exception as e:  # noqa: BLE001 — every failure means: no session
        attached = False
        why = f"{type(e).__name__}: {e}"
    if not attached:
        _log(f"window for {sid[:8]} failed — {why}; destroying the session")
        record_close(sid)
        subprocess.run(_t("kill-session", "-t", _name(sid)), capture_output=True)
        raise WindowRequired(
            "could not open a terminal window on the Mac for this spawn")


def _session_ages() -> dict[str, float]:
    """{session_name: seconds since creation} for every managed session."""
    r = subprocess.run(_t("list-sessions", "-F",
                          "#{session_name}\t#{session_created}"),
                       capture_output=True, text=True)
    if r.returncode != 0:
        return {}
    now = time.time()
    out = {}
    for ln in r.stdout.splitlines():
        name, _, created = ln.partition("\t")
        try:
            out[name.strip()] = max(0.0, now - float(created))
        except ValueError:
            continue
    return out


def reconcile(grace: float = 60.0) -> list[str]:
    """Startup pass for the board invariant. Returns the sids reaped.

    Three duties:
      1. Disarm the legacy `destroy-unattached` on every managed session —
         sessions created under the old window invariant would otherwise be
         destroyed by the tmux server on their next detach.
      2. Reap sessions whose pane no longer runs an agent CLI (a bare shell
         left by the pre-exec era, or a launch whose prelude short-circuited).
         Life is bound to the process; an agent-less pane is a dead session
         still occupying the board. `grace` spares freshly created sessions
         whose agent hasn't started yet.
      3. Drop registry rows whose tmux session is gone. `record_close` only
         fires on the close path, so a session whose claude exited on its own
         leaves its row behind forever. Consumers already read through
         `open_registry`, which filters to live sessions — so this is hygiene
         on the file, not a correctness fix, and it happens last: a row is
         only litter once the reaping above has settled what is alive.

    Liveness is judged the way the board judges it — the pane's tty against the
    live agent scan — never by pane_current_command string-matching. The scan
    is engine-blind (Claude and Codex both), and it must stay that way: a
    Codex pane read as dead here would be silently reaped on every dashboard
    restart, which looks exactly like a session that ended on its own."""
    ages = _session_ages()
    known = {_name(sid): sid for sid in _reg_load()}
    agent_ttys = set()
    try:
        from .procscan import get_claude_processes
        for p in get_claude_processes().get("processes", []):
            if p.get("tty"):
                agent_ttys.add(p["tty"])
    except Exception as e:  # noqa: BLE001 — a failed scan must reap nothing
        _log(f"reconcile: agent scan failed ({type(e).__name__}: {e}); "
             "disarming only")
        agent_ttys = None
    reaped = []
    panes = pane_ttys()  # {pane_tty: session_name}
    ttys_by_name: dict[str, list[str]] = {}
    for tty, name in panes.items():
        ttys_by_name.setdefault(name, []).append(tty)
    for name in open_names():
        if not name.startswith("jr-"):
            continue
        _disarm_destroy_unattached(name)
        if agent_ttys is None or ages.get(name, 0.0) < grace:
            continue
        if any(t in agent_ttys for t in ttys_by_name.get(name, [])):
            continue
        sid = known.get(name, name[3:])
        close_managed(sid, review=False)  # nothing left to exit cleanly
        reaped.append(sid)
    # Duty 3 — and only if the socket answered. `open_names()` returns an empty
    # set both for "no sessions" and for a tmux that failed to answer, and on
    # the second reading this would wipe the registry for every live session.
    if open_names():
        live = {n[3:] for n in open_names() if n.startswith("jr-")}

        def _drop_dead_rows(d: dict) -> None:
            for sid in [s for s in d if s[:8] not in live]:
                del d[sid]

        _reg_mutate(_drop_dead_rows)
    return reaped


def open_managed(sid: str, cwd: str, resume: bool = True, displace=None,
                 nudge: str | None = None, extra: str = "",
                 prelude: str = "", window: bool = False,
                 engine: str = "", model: str = "",
                 tag: str = "", resume_id: str = "") -> None:
    """Bring up `sid` as a managed session. Idempotent.

    `engine` picks the agent CLI the pane execs — "codex", or "claude" (what
    an unregistered sid falls back to, so every caller predating the picker is
    unchanged). `model` pins which model that CLI runs; empty leaves the CLI
    to its own default. **Both are read back from the open registry when the
    caller says nothing**, for the same reason the pin below is: every reopen
    path passes a sid and a cwd and nothing else, because that is all a reopen
    knows, while the engine is a property of the SESSION that only its spawn
    ever witnessed. Defaulting to claude there put `claude --resume` in the
    pane of a Codex session — the wrong CLI, against an id it has never heard
    of. Everything else on this path is
    engine-blind on purpose: the tmux options, the registered-first board row,
    the process-bound life, reconcile's tty check and the PTY socket all work
    on bytes and process liveness, not on which CLI is running.

    `tag` opens the session on a SUBJECT: exported as `JSTACK_TIMELINE_TAG`
    into the pane, where jStack's SessionStart hook swaps the seat's injected
    history for that tag's — every agent's work on it — and files this
    session's own entries under it. Env and not a CLI flag because it is not
    the CLI's business: both engines run the same hooks, and the pin has to
    survive whatever the pane's shell does before `exec`. Caller-validated
    against the minted vocabulary; an unknown tag reaching here would leave
    the hook to fall back to the seat, which is safe but not what was asked.

    Left unset, the pin is READ BACK from the open registry rather than
    dropped. Every reopen path — board tap, PTY attach, displace, splitoff —
    calls this with `resume=True` and no tag, and the hook re-injects on every
    SessionStart. Without the read-back, a pinned session reopened from the
    board would come up reading its seat's history instead of its subject's:
    the same row, silently changing what it knows, which is the exact drift
    the pin exists to prevent. An explicit tag still wins, so a caller can
    re-pin an existing session by passing a different one.

    Registered-first, not window-first: the caller records the sid on the open
    registry, the board row exists from the session's first instant, and no
    terminal window is required — every client is a viewer. `window=True` is
    the desk-side spawn's ask (a handoff's product is a new terminal on the
    desk): an iTerm window is attached before `claude` starts, and that
    creation stays transactional — no window ⇒ the fresh session is destroyed
    and `WindowRequired` raised.

    `displace` is the takeover hook: a callable run **after** the managed
    session (the terminal the work is moving to) exists and **before**
    `claude` starts, to end a raw Mac `claude` still holding this transcript.
    It returns False when the old process would not exit, and then this
    session is torn back down (`TakeoverFailed`) rather than becoming a second
    writer on one transcript.

    `nudge` is the takeover's auto-continue: text typed into `claude` once it
    is back at its prompt. A takeover SIGKILLs a session mid-turn, and `--resume`
    alone leaves that turn dead at a waiting prompt — the caller passes the
    continue message when the session it displaced was actually working.

    `extra` is spliced verbatim after the standard claude flags — the desk-side
    spawn CLI's briefing/name args (`--append-system-prompt "$VAR"`, `--name …`),
    pre-quoted by the caller for the pane's shell. `prelude` is prepended to the
    claude invocation in that same shell line — the read-brief-then-delete idiom
    whose variable `extra` references. Both empty for every phone-driven open.

    Re-entry on an already-open session never duplicates; with `window=True`
    it attaches an iTerm window if none is showing (idempotent viewer, never
    a teardown).

    Raises `WindowRequired` (window=True and none could be opened — fresh
    session only) / `TakeoverFailed` (transcript never freed) — a fresh
    session never survives either raise.
    """
    name = _name(sid)
    if is_open(sid):
        if window and not has_window(sid):
            launch_terminal(sid)
        return
    # One read for all three session facts a reopen doesn't carry. The caller
    # always wins — that is how a session is re-pinned, or reopened on a model
    # it wasn't spawned with.
    prior = _reg_load().get(sid) or {}
    engine = engine or prior.get("engine") or "claude"
    model = model or prior.get("model") or ""
    env = {**os.environ, "PATH": _PATH}
    # The sealed Hub bundles Homebrew's tmux, whose ncurses looks for terminfo
    # in a Homebrew prefix a clean Mac doesn't have; without this every session
    # spawns spewing "can't find terminfo database". The system database is
    # always present, so it anchors the search path.
    env.setdefault("TERMINFO_DIRS",
                   "/usr/share/terminfo:/opt/homebrew/share/terminfo")
    subprocess.run(_t("new-session", "-d", "-s", name, "-c", cwd),
                   check=True, env=env)
    # Mouse on, server-wide: wheel events scroll tmux copy-mode, which is how
    # both iTerm on the Mac and the phone's terminal scroll these sessions.
    subprocess.run(_t("set-option", "-g", "mouse", "on"),
                   capture_output=True, env=env)
    # Extended keys, server-wide: without this tmux collapses Shift+Enter (and
    # every other modified key) to a bare Enter, so claude submits instead of
    # inserting a newline when the Mac terminal is attached through tmux. The
    # outer terminal must also advertise the feature (extkeys). Server options,
    # idempotent to re-set per session-create.
    subprocess.run(_t("set-option", "-s", "extended-keys", "always"),
                   capture_output=True, env=env)
    subprocess.run(_t("set-option", "-as", "terminal-features", "xterm*:extkeys"),
                   capture_output=True, env=env)
    # Clipboard passthrough: the default (external) forwards only tmux's own
    # copies; `on` also forwards OSC 52 the CLI emits, so a copy made inside
    # claude lands on the attached device's clipboard (the app's clipboardCopy
    # delegate writes it there). Scoped to this socket — never ~/.tmux.conf.
    subprocess.run(_t("set-option", "-g", "set-clipboard", "on"),
                   capture_output=True, env=env)
    # No status bar: nothing reads it (every watcher uses capture-pane, which
    # never includes it) and the app/iTerm views don't need a session-name
    # strip — it's one more row of terminal instead.
    subprocess.run(_t("set-option", "-g", "status", "off"),
                   capture_output=True, env=env)
    if window:
        # The desk spawn's window comes up before `claude` does. Until this
        # returns, the session holds an idle shell and nothing else — so the
        # failure path tears down a session that never ran anything.
        _attach_window_or_die(sid)
    # The managed session (the terminal the work moves to) now exists, so the
    # old holder is allowed to end.
    if displace is not None and not displace():
        _log(f"takeover for {sid[:8]} aborted — the Mac's claude never exited; "
             "destroying the session that was standing up for it")
        record_close(sid)
        subprocess.run(_t("kill-session", "-t", name), capture_output=True)
        raise TakeoverFailed(
            "the claude on the Mac wouldn't exit — close that window there")
    inner = _inner_command(sid, resume, extra, prelude, engine, model,
                           resume_id)
    # Claude's one-time "trust this folder?" dialog would otherwise park a
    # managed session — opened for a phone that cannot answer it — on a gate
    # before it reads its first prompt (the welcome nudge, a resume, anything).
    # `_auto_accept_bypass` watches for a different prompt and never clears it.
    # Pre-accept it for the workspace, the way the desk answers it once by hand.
    # Codex trust rides its own CLI flags in `_inner_command`, not this file.
    if engine != "codex":
        _trust_workspace(cwd)
    # Run in the pane's shell with a known PATH; drop the nested-session markers
    # (CLAUDECODE, CLAUDE_CODE_CHILD_SESSION) — a pane spawned by a daemon that
    # was itself restarted from inside a Claude session inherits them, and
    # CLAUDE_CODE_CHILD_SESSION makes claude skip transcript persistence
    # entirely (no .jsonl, no resume, no timeline selfwrite).
    launched_at = time.time()
    # The pin rides the same export line as PATH — quoted, because a tag is
    # caller-supplied text and this string is typed into a shell.
    pin = tag or prior.get("tag", "")
    pin = f"export JSTACK_TIMELINE_TAG={shlex.quote(pin)}; " if pin else ""
    # Fullscreen by env, never by settings. Claude arms a boot canary in
    # ~/.claude.json (pending[pid], cleared 10s after the first frame) on every
    # fullscreen launch it owns, and a pane killed before that mark leaves the
    # record behind — close, takeover and reconcile all kill mid-boot here. The
    # next `claude` on this Mac reads a stale record as a failed start and drops
    # to the classic renderer; two of them turn fullscreen off machine-wide.
    # CLAUDE_CODE_NO_FLICKER=1 is the env entry path, which never arms it.
    # Anchor the pane in its workspace by absolute path, in the pane's own shell
    # — never trust tmux's `-c` start-directory alone. `-c` is resolved against
    # the tmux SERVER's cwd, and a long-lived server whose cwd was rebuilt out
    # from under it (a host reinstall replaces ~/jStack; the server keeps a
    # descriptor on the now-deleted inode) can no longer resolve `-c` at all —
    # every new pane then silently inherits the server's DELETED cwd. Newer
    # `claude` refuses to start in a deleted directory and exits on the spot, so
    # the pane dies before it ever attaches and the phone sees a thread that
    # never opens a terminal. `cd` re-walks the path from root in the pane, immune
    # to the server's state; `|| exit 1` fails the pane cleanly if the workspace
    # is genuinely gone rather than exec-ing claude into a bad directory.
    enter = f"cd {shlex.quote(cwd)} || exit 1; "
    boot = _write_boot(sid,
                       f"export PATH={_PATH}; export CLAUDE_CODE_NO_FLICKER=1; "
                       f"{pin}{_compose_exports(sid)}"
                       f"unset CLAUDECODE CLAUDE_CODE_CHILD_SESSION CODEX_THREAD_ID CLAUDE_CODE_SESSION_ID; {enter}", inner)
    subprocess.run(_t("send-keys", "-t", name, "-l", f"source {boot}"), check=True)
    subprocess.run(_t("send-keys", "-t", name, "Enter"), check=True)
    # Each engine has its own unanswerable-from-a-phone startup prompt, and
    # both are dismissed the same way: watch the pane, act only once the
    # prompt is actually on screen. Neither watcher's string appears in the
    # other's CLI, so they are not interchangeable.
    if engine == "codex":
        from . import codex_transcript
        codex_transcript.bind_open_session(sid, launched_at, cwd)
        _auto_skip_codex_update(name)
    else:
        _auto_accept_bypass(name)
    if nudge:
        _nudge_when_ready(name, nudge, engine)


#: Where a pane's boot line is staged. `/tmp`, not `$TMPDIR`: macOS hands each
#: process a ~58-char per-user folder, and this path is the one part of the
#: typed line that still has to be typed.
_BOOT_DIR = Path("/tmp")

#: Stale boot files are swept at this age. Long enough that a pane still
#: coming up never has its own file pulled away, short enough that /tmp does
#: not accumulate one per session forever.
_BOOT_TTL = 3600

#: The tty line-discipline's canonical-mode limit. Input past this is DROPPED,
#: silently, and a fresh pane is still in canonical mode when it is typed to.
_MAX_CANON = 1024


def _write_boot(sid: str, exports: str, inner: str) -> str:
    """Stage the pane's boot command as a file; return the path to `source`.

    **Nothing typed into a pane may scale with caller input.** The boot command
    is delivered by `send-keys` into a shell that has only just started, and a
    fresh pane's tty is still in CANONICAL mode — where the line discipline
    drops everything past MAX_CANON (1024 bytes) and says nothing. The exports
    alone are ~820 of that, so the budget was always nearly spent: a takeover
    whose focus text ran long crossed it, the line landed cut mid-quote, and
    zsh sat at a `quote>` continuation forever. The window was up with the
    briefing in it and `claude` had never been reached — a spawn that looks
    like a spawn and runs nothing.

    So the command goes to a file and the pane is typed a `source` whose length
    is fixed by the sid. Bounded by construction, not by trusting any caller to
    keep its prose short.

    Not self-deleting. An `rm` inside the line would race the shell still
    reading it, and the failure mode of getting that wrong is exactly the one
    this function exists to remove. Stale files are swept here instead.
    """
    _BOOT_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - _BOOT_TTL
    for old in _BOOT_DIR.glob("jremote-boot-*.sh"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass
    path = _BOOT_DIR / f"jremote-boot-{sid[:8]}.sh"
    path.write_text(f"{exports}{inner}\n")
    path.chmod(0o600)
    return str(path)


def _inner_command(sid: str, resume: bool, extra: str = "",
                   prelude: str = "", engine: str = "claude",
                   model: str = "", resume_id: str = "") -> str:
    """The agent invocation the pane's shell runs.

    bypassPermissions (Claude) / the config's danger-full-access + never
    (Codex): a phone-driven session can't answer tool or skill prompts, so it
    must never block on one — same posture as the headless turn path.

    `exec`: the pane process IS the agent, so the tmux server destroys the
    session the moment it exits — `/exit`, a crash, a kill. That is the board
    invariant's ending bound to the process, with no shell husk left behind
    for reconcile to sweep.

    **Codex ids are not ours.** Claude adopts an id we choose
    (`--session-id`), which is what lets jRemote register a board row before
    the CLI starts and then resume by that same id. Codex has no such flag —
    it mints its own thread id — so for `engine="codex"` the `sid` here is a
    BOARD handle only, and `resume=True` requires the caller to pass Codex's
    own id in `sid`. Resuming a Codex session by a jRemote sid would silently
    open the picker instead, which on a phone is a session that never starts.

    `resume_id` is what to resume, when that is not the board handle — which
    is only ever the Codex case above. `reopen_target` recovers it from the
    rollout the board row was folded from; empty means resume `sid` itself,
    which is right for Claude and for a Codex spawn that was handed its own id.

    Trust is said TWICE for Codex, and both spellings are load-bearing. Codex
    gates hooks behind a trust prompt keyed to each hook's content hash, so
    editing any hook script re-arms it; unanswered, our timeline injection and
    guardrails simply do not run, and a phone has no way to answer. The CLI
    flag covers the invocation, but observed behaviour is that it leans on
    already-persisted trust — a first run against a fresh or freshly-edited
    hook set can still skip the hooks. `-c bypass_hook_trust=true` sets it as
    config, which does not. Belt and braces on purpose: the failure here is
    silent and total, and a session that runs with no guardrails looks exactly
    like one that runs with them. Sandbox and approvals are already set in
    ~/.codex/config.toml, so only trust needs saying on the command line."""
    target = resume_id or sid
    if engine == "codex":
        flags = "--dangerously-bypass-hook-trust -c bypass_hook_trust=true"
        inner = f"codex resume {target} {flags}" if resume else f"codex {flags}"
    else:
        flags = "--permission-mode bypassPermissions"
        inner = (f"claude --resume {target} {flags}" if resume
                 else f"claude --session-id {sid} {flags}")
    # The model flag, quoted by engines.model_flag — Claude's 1M ids carry
    # brackets the pane's shell would otherwise glob. Ahead of `extra` so a
    # caller passing its own --model there still wins, which is what a
    # verbatim splice is for.
    if model:
        from . import engines
        inner += " " + engines.model_flag(engine, model)
    if extra:
        inner += " " + extra
    inner = "exec " + inner
    if prelude:
        inner = prelude + inner
    return inner


def _auto_skip_codex_update(name: str) -> None:
    """Codex opens with "Update available!" and waits on a 1/2/3 choice before
    it will draw its prompt. A phone cannot answer it, so a jRemote-spawned
    Codex session parks there forever — the board shows a live session, the
    terminal shows a menu, and nothing anywhere says the session never
    started. It is the same class of failure as the unanswered hook-trust
    prompt, and it re-arms on its own: OpenAI ships a release and every Codex
    spawn is blocked again until someone opens one and notices.

    Answers **2 (Skip)**, not 3 (Skip until next version). 3 writes
    `dismissed_version` into `~/.codex/version.json`, which would hide the
    update from the user on the desk too — this is a phone-driven pane getting out
    of its own way, not a decision about upgrading. Skipping costs nothing:
    the notice comes back next launch, where a human can act on it.

    Matched on "Skip until next version", which appears nowhere else in the
    CLI, so a stray keypress can never land in real input. Polled longer than
    the bypass watcher because the prompt is gated on a network round trip to
    the releases API, not on local startup."""
    script = (
        f'for i in $(seq 1 60); do '
        f'  p="$({_TMUX} -L {_SOCK} capture-pane -p -t {name} 2>/dev/null)"; '
        f'  if printf "%s" "$p" | grep -q "Skip until next version"; then '
        f'    {_TMUX} -L {_SOCK} send-keys -t {name} -l "2"; '
        f'    {_TMUX} -L {_SOCK} send-keys -t {name} Enter; break; '
        f'  fi; sleep 0.5; done'
    )
    subprocess.Popen(["bash", "-c", script], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def _trust_workspace(cwd: str) -> None:
    """Record `cwd` as a trusted directory in Claude's own config, so an
    interactive managed session opens on its work instead of the one-time
    "trust this folder?" gate.

    Claude keeps folder trust in ~/.claude.json under
    projects[<dir>].hasTrustDialogAccepted; an interactive session in an
    untrusted directory stops on the dialog before it reads a first prompt, and
    a phone has no way to answer it. On the user's own machine the dialog was
    accepted by hand once — this is that same acceptance, made ahead of a
    launch the daemon owns.

    Scoped to the daemon's own instance root: the host auto-trusts only the
    agent-workspace tree it resolves sessions into, never an arbitrary path a
    client might open — that path is exactly what the dialog is there to guard.
    Idempotent: a directory already trusted is left untouched, so this never
    rewrites the CLI's shared file when it has nothing to add (and never races
    a concurrent claude's write for no reason)."""
    try:
        root = hostenv.instance_root().resolve()
        target = Path(cwd).expanduser().resolve()
        if target != root and root not in target.parents:
            return
    except Exception:
        return
    cfg = Path.home() / ".claude.json"
    try:
        data = json.loads(cfg.read_text()) if cfg.exists() else {}
    except (OSError, ValueError):
        return  # a config we cannot parse is one we will not clobber
    if not isinstance(data, dict):
        return
    key = str(target)
    projects = data.get("projects")
    if not isinstance(projects, dict):
        projects = {}
    entry = projects.get(key)
    if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True:
        return  # already trusted — no write
    if not isinstance(entry, dict):
        entry = {}
    entry["hasTrustDialogAccepted"] = True
    entry.setdefault("allowedTools", [])
    projects[key] = entry
    data["projects"] = projects
    tmp = cfg.with_name(cfg.name + f".jstack-{os.getpid()}")
    try:
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, cfg)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _auto_accept_bypass(name: str) -> None:
    """bypassPermissions shows a one-time startup warning whose default is
    'No, exit'. A phone can't answer it, so accept it here — but only when it's
    actually on screen (so we never inject a stray keypress into real input).
    Detached so the open endpoint returns immediately."""
    script = (
        f'for i in $(seq 1 25); do '
        f'  p="$({_TMUX} -L {_SOCK} capture-pane -p -t {name} 2>/dev/null)"; '
        f'  if printf "%s" "$p" | grep -q "Yes, I accept"; then '
        f'    {_TMUX} -L {_SOCK} send-keys -t {name} Down; '
        f'    {_TMUX} -L {_SOCK} send-keys -t {name} Enter; break; '
        f'  fi; sleep 0.3; done'
    )
    subprocess.Popen(["bash", "-c", script], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


# Per engine: (the line that proves the TUI is up and idle, the startup prompt
# whose watcher above may still be answering). Both are strings the CLI draws
# and nothing else does, so neither can be matched out of a transcript on
# screen. Codex's marker is its empty-composer placeholder — present on a fresh
# session and on a resume, gone the moment anything is typed, which is only
# ever after this check.
_READY = {
    "claude": ("bypass permissions on", "Yes, I accept"),
    "codex": ("Ask Codex to do anything", "Skip until next version"),
}


def _nudge_when_ready(name: str, text: str, engine: str = "claude") -> None:
    """Type `text` into the session once the agent CLI is actually at its prompt.

    Readiness is a line only the live TUI draws, so the keys can only land in
    the input box — never in the shell that is still launching it. Each engine
    has its own, paired with the startup prompt that must be *off* screen
    first: while that prompt is still up its watcher hasn't answered it yet,
    and the two detached watchers must never race keys into one dialog.
    Bounded: if the prompt never shows (the CLI died, resume failed), nothing
    is sent."""
    if not text:
        return  # nothing to type, and an empty `typing` below is a bash syntax error
    marker, blocker = _READY.get(engine, _READY["claude"])
    typing = "; ".join(shlex.join(a) for a in _type_argv(name, text))
    script = (
        f'for i in $(seq 1 120); do '
        f'  p="$({_TMUX} -L {_SOCK} capture-pane -p -t {name} 2>/dev/null)"; '
        f'  if printf "%s" "$p" | grep -qi {shlex.quote(marker)} && '
        f'     ! printf "%s" "$p" | grep -q {shlex.quote(blocker)}; then '
        f'    sleep 0.5; '
        f'    {typing}; '
        f'    sleep 0.2; '
        f'    {_TMUX} -L {_SOCK} send-keys -t {name} Enter; break; '
        f'  fi; sleep 0.5; done'
    )
    subprocess.Popen(["bash", "-c", script], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def send_input(sid: str, text: str) -> bool:
    """Type `text` + Enter into the managed session's stdin. False if not open."""
    if not is_open(sid):
        return False
    engine = (open_registry().get(sid) or {}).get("engine", "claude")
    if engine == "codex":
        from .codex_commands import translate, workspace
        text = translate(text, workspace(sid) if text.lstrip().startswith("/") else "")
    name = _name(sid)
    for argv in _type_argv(name, text):
        subprocess.run(argv, check=True)
    if engine == "codex":
        time.sleep(0.3)  # let Codex finish its paste burst before submitting
    subprocess.run(_t("send-keys", "-t", name, "Enter"), check=True)
    return True


def send_input_after_compact(sid: str, text: str, transcript: str,
                             timeout: float = 600.0) -> bool:
    """Type `text` once a NEW compaction has landed in `transcript`.

    For the caller that wants a heavy session to compact before it reads the
    next message: send `/compact` with `send_input`, then this, and the message
    lands in the freed window instead of the last of the old one.

    The wait is on the transcript, never the screen. A compaction writes one
    `system`/`compact_boundary` line, so the boundaries already on file when
    this is called are counted first and the watcher waits for one more — the
    only way to tell this compaction from the session's earlier ones. The
    footer that `_nudge_when_ready` matches cannot do it: the TUI draws it
    mid-turn too, so it proves the CLI is up and nothing about what it is
    doing.

    Detached and bounded, the same shape as `_nudge_when_ready`. On timeout it
    types anyway: a message from the user that never arrives is a worse failure
    than one that arrives in a heavy window, and the caller's fallback is
    exactly the behaviour it had before compaction was in the path.
    """
    if not text or not is_open(sid):
        return False
    name = _name(sid)
    argv = [*_type_argv(name, text), _t("send-keys", "-t", name, "Enter")]
    script = (
        "import json,os,subprocess,sys,time\n"
        "path,argv,deadline=sys.argv[1],json.loads(sys.argv[2]),time.time()+float(sys.argv[3])\n"
        "def n():\n"
        "    try:\n"
        "        with open(path,'rb') as f:\n"
        "            return sum(1 for ln in f if b'compact_boundary' in ln)\n"
        "    except OSError:\n"
        "        return -1\n"
        "base=n()\n"
        "while time.time()<deadline:\n"
        "    time.sleep(2)\n"
        "    c=n()\n"
        "    if c>=0 and base>=0 and c>base: break\n"
        "    if base<0: base=c\n"
        "time.sleep(2)\n"
        "for a in argv: subprocess.run(a)\n"
    )
    subprocess.Popen([sys.executable, "-c", script, transcript,
                      json.dumps(argv), str(timeout)],
                     start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def close_managed(sid: str, review: bool = True) -> bool:
    """Close a managed session **and the window showing it**. False if not open.

    review=True  (Close with intent): EOF so `claude` exits cleanly and its
      SessionEnd review hook fires (spawned detached, survives), then tear down.
    review=False (mode switch): hard-kill the pane so no SessionEnd hook / review
      runs — we're just detaching the terminal, not ending the work.

    Either way the Mac windows attached to it close, so the desk ends up where
    closing the window by hand would have left it. Their ttys are collected
    first: after the teardown tmux can no longer name the clients it had."""
    if not is_open(sid):
        return False
    name = _name(sid)
    ttys = client_ttys(sid)
    # Preserve the native transcript identity before record_close drops its
    # board binding. Codex may still be exiting when the teardown kills tmux,
    # so its SessionEnd hook is insufficient on this path.
    review_cmd = []
    if review:
        from .messages import _find_session_file
        from .codex_transcript import metadata
        from . import plugin_paths
        transcript = _find_session_file(sid)
        spawn = plugin_paths.jstack_bin("session-review-spawn")
        if transcript and spawn.is_file():
            review_cmd = [str(spawn), metadata(transcript).get("id") or sid,
                          str(transcript)]
    record_close(sid)
    if not review:
        # SIGKILL the pane's claude before tearing the session down —
        # kill-session alone SIGHUPs it, it exits "cleanly", and its
        # SessionEnd hook spawns a review nobody asked for.
        r = subprocess.run(_t("list-panes", "-t", name, "-F", "#{pane_pid}"),
                           capture_output=True, text=True)
        for pid_s in r.stdout.split():
            try:
                pid = int(pid_s)
            except ValueError:
                continue
            subprocess.run(["pkill", "-9", "-P", str(pid)], capture_output=True)
            subprocess.run(["kill", "-9", str(pid)], capture_output=True)
        subprocess.run(_t("kill-session", "-t", name))
        close_windows(ttys)
        return True
    subprocess.run(_t("send-keys", "-t", name, "C-c"))
    subprocess.run(_t("send-keys", "-t", name, "C-d"))
    # Give claude a moment to exit + fire its hook, then kill the lingering shell
    # pane and close the window it was in — detached so the endpoint returns
    # immediately, one script so the window never closes ahead of the exit.
    steps = [f"sleep 2", f"{shlex.quote(_TMUX)} -L {shlex.quote(_SOCK)} "
             f"kill-session -t {shlex.quote(name)} 2>/dev/null"]
    if review_cmd:
        # The engine's atomic claim deduplicates this fallback with a native
        # hook that did run. Dispatch only after the source process is gone.
        steps.append(shlex.join(review_cmd))
    if ttys:
        steps.append(" ".join([shlex.quote(sys.executable), "-m", "jstack_host.iterm_window",
                               "--close", *(shlex.quote(t) for t in ttys)]))
    subprocess.Popen(
        ["bash", "-c", "; ".join(steps)],
        cwd=str(hostenv.package_root()),   # so `-m jstack_host.…` resolves
        start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return True


def attach_command(sid: str) -> str:
    """Shell command that attaches a terminal to the managed session."""
    return f"{_TMUX} -L {_SOCK} attach -t {_name(sid)}"


def launch_terminal(sid: str) -> None:
    """Attach an iTerm window to the managed session — the Open in iTerm verb.

    A viewer on demand: the app's Open in iTerm, the desk-side spawn's window.
    Detaching it later (closing the window) ends nothing — the session's life
    is its claude process.

    Primary path: iTerm's Python API (`jstack_host.iterm_window` — unix socket, no
    Apple events/TCC; needs EnableAPIServer + one-time consent). Fallback:
    `open -a iTerm <file.command>`, which lands as a tab in the running
    instance (`open -na` throws iTerm's "session ended / profile Default"
    error, and AppleScript is TCC-blocked for the daemon).

    Never takes the keyboard: `-g` keeps iTerm from being pulled to the front,
    and the API path hands focus back to whatever session held it. A window
    that grabs focus types the user's next sentence into a fresh shell — visible on
    the desk is the requirement, frontmost never was."""
    infra = hostenv.package_root()
    subprocess.run(["open", "-g", "-a", "iTerm"], check=False)  # API needs iTerm up
    try:
        r = subprocess.run([sys.executable, "-m", "jstack_host.iterm_window", attach_command(sid)],
                           cwd=str(infra), capture_output=True, timeout=15)
        if r.returncode == 0:
            return
        _log(f"iterm api path failed for {sid[:8]} (rc={r.returncode}): "
             f"{r.stderr.decode(errors='replace').strip()[:400] or '<no stderr>'}"
             " — falling back to the .command tab")
    except subprocess.TimeoutExpired:
        _log(f"iterm api path timed out for {sid[:8]} after 15s "
             "— falling back to the .command tab")
    cmd_file = Path("/tmp") / f"jr-attach-{sid[:8]}.command"
    cmd_file.write_text("#!/bin/bash\nset +e\nexec " + attach_command(sid) + "\n")
    cmd_file.chmod(0o755)
    subprocess.run(["xattr", "-c", str(cmd_file)], capture_output=True)  # strip any xattrs
    subprocess.run(["open", "-g", "-a", "iTerm", str(cmd_file)], check=False)
