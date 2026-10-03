"""Seeing what is running — the process scan behind every board row.

`ps` and `psutil` answer the same on any Mac, so none of this needs the host's
dialect: which binary is an agent CLI, which pid holds which terminal, how to
read a session id out of an argv. It lived in `routes/system.py` only because
the dashboard was the first thing that wanted it.

The one host fact it does need is the label the user gave a session, and that
comes through `hostenv` — a host with no label store answers `{}` and the rows
fall back to what the process itself says.

The HTTP route stays in `routes/system.py` and calls `get_claude_processes()`
here, so `/api/claude-processes` is unchanged for every existing caller.
"""

import re
import subprocess
import time
from pathlib import Path

import psutil

from .hostenv import session_labels

_HOME = str(Path.home())
_HOME_PREFIX = _HOME + "/"


def iter_procs(attrs):
    """Yield (Process, info-dict) pairs whose info this caller alone owns.

    psutil caches Process instances in a module-global table and shares them
    across every caller; `process_iter(attrs=…)` writes each scan's result
    onto that shared instance as `.info`. Two scans running at once — a
    board_watch tick in its thread and any HTTP process route — therefore
    overwrite each other's `.info` mid-loop, and a reader asking for a key
    the other scan didn't request raises KeyError on a process that has one
    (`proc.info["name"]`, read off the narrower ["pid", "cmdline"] dict).
    Rare, and it failed two ways: the loops that swallowed KeyError dropped
    live processes from their answer, and the one that didn't took out a
    whole board observation.

    `as_dict` returns a fresh dict, so the attrs a loop asks for are the
    attrs it reads. Same per-process cost as `process_iter(attrs=…)`, which
    calls exactly this underneath. Scan processes through here, never
    through `proc.info`.

    The attr fetch is caught wide because macOS raises SystemError/OSError out
    of proc_cmdline on processes that die mid-read — the same transient that
    once filled dashboard.err from the phone poll (`routes/phone.py`). One bad
    PID skips; it never takes the scan with it."""
    for proc in psutil.process_iter():
        try:
            info = proc.as_dict(attrs=attrs)
        except (psutil.NoSuchProcess, psutil.AccessDenied,
                psutil.ZombieProcess, SystemError, OSError):
            continue
        yield proc, info


def _agent_engine(name: str | None, cmdline: list) -> str | None:
    """Which agent CLI this process is — "claude", "codex", or None.

    Binary-identity match only. The Claude native binary reports psutil name
    "claude.exe" with bare "claude" argv[0]; never match path substrings (that
    missed real sessions and caught zsh shell-snapshot children as fake ones).

    Codex is installed via npm, and that shape matters: the process a tmux pane
    execs is the **node wrapper** (`node /opt/homebrew/bin/codex`), which spawns
    the real Rust binary as a CHILD. Only the child is matched here. Matching
    the wrapper too would report one session twice on the board; matching only
    the wrapper's name ("node") would catch every unrelated node process on the
    machine. The child shares the pane's tty, which is what `managed.reconcile`
    actually judges liveness by — so binding to the child keeps a Codex pane
    alive across a dashboard restart instead of being reaped as a dead shell.
    """
    n = name or ""
    arg0 = cmdline[0] if cmdline else ""
    if n in ("claude", "claude.exe") or arg0 == "claude" or arg0.endswith("/claude"):
        return "claude"
    if n in ("codex", "codex.exe") or arg0 == "codex" or arg0.endswith("/codex"):
        if _subcommand(cmdline) in _CODEX_SERVERS:
            return None
        return "codex"
    return None


#: Codex subcommands that run a server, never a session. The app-server daemon
#: outlives whatever started it — one lingered for a day as two permanent
#: Headless cards, with Kill on them wired to Codex's own server (#344).
#: `exec-server` is the ChatGPT desktop app's bridge to a cloud environment;
#: the app respawns it and its app-server the moment either is killed, so a
#: Kill on their cards brought them straight back.
_CODEX_SERVERS = frozenset({"app-server", "exec-server", "mcp-server"})

#: Codex global flags that take a separate value. The ChatGPT app launches
#: `codex -c features.code_mode_host=true app-server`; without knowing `-c`
#: consumes the next word, the subcommand read as that config pair and the
#: server walked past `_CODEX_SERVERS` onto the board.
_CODEX_VALUE_FLAGS = frozenset({
    "-c", "--config", "--enable", "--disable", "--remote",
    "--remote-auth-token-env", "-i", "--image", "-m", "--model",
    "--local-provider", "-p", "--profile", "-s", "--sandbox", "--add-dir",
    "-a", "--ask-for-approval", "-C", "--cd"})


#: A Codex thread id, as it appears bare in argv — `codex exec resume <id>`
#: names its target positionally, with no flag to key on.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _subcommand(cmdline: list) -> str:
    """The first bare word after argv[0] — `exec` in `codex exec resume …`.

    Scanned rather than indexed at [1] because a global flag may sit in front
    of the subcommand, and `--flag value` must not have its value read as one.
    """
    args = iter(cmdline[1:])
    for arg in args:
        if arg in _CODEX_VALUE_FLAGS:
            next(args, None)
            continue
        if arg.startswith("-"):
            continue
        return arg
    return ""


def _codex_native_id(cmdline: list) -> str:
    """Codex's own thread id, when this argv is a `codex exec resume <id>` run.

    '' for anything else — an interactive pane, a fresh `codex exec` with no
    session to resume, `claude` in any shape. The first UUID-shaped element
    wins rather than the element at a fixed position, because the prompt text
    follows the id and a prompt is free to quote one; and because the flags
    jRemote passes (`--json`, `--model m`, the bypasses) are not a fixed
    prefix."""
    if _subcommand(cmdline) != "exec":
        return ""
    for arg in cmdline[1:]:
        if _UUID_RE.match(arg):
            return arg
    return ""


def _is_claude_proc(name: str | None, cmdline: list) -> bool:
    """True for any agent-CLI process the board tracks (Claude or Codex).

    Kept under its original name because every caller is a jRemote board
    surface, and the board invariant is engine-blind: no managed session may
    exist off the board, whichever CLI is running in the pane."""
    return _agent_engine(name, cmdline) is not None


def _tty_map(pids=None) -> dict:
    """{pid: "/dev/ttysNNN"} for every process holding a terminal, read fresh.

    Not `psutil.Process.terminal()`. That resolves a process's tty device
    number through a map of `/dev` built on first use and **memoized for the
    life of the process** — and macOS creates `/dev/ttysNNN` nodes on demand,
    as terminals are opened. So in a daemon that has been up for days, every
    window opened since it started resolves to `None`: the session reads as
    having no terminal at all, drops out of the board's On Mac section into
    Headless, and — since a headless run has no idle state — shows a working
    dot that never goes out. The truth about a window on the desk must not
    depend on how long this daemon has been running.

    `ps` is asked instead, once per scan, and answers about the live /dev.

    A failure RAISES — same law as the process scan it feeds. Answering `{}`
    said "nothing on this Mac owns a terminal", every session read headless,
    and headless was enough to skip the board's turn clock: mid-turn rows
    asserted turn_open=False and the notify watcher pushed a done into a turn
    that was still running. A look that failed must not answer as a look that
    found nothing.

    `pids` narrows the question to those processes. Asking about the whole
    machine costs ~35ms of `ps` walking twelve hundred processes to keep the
    four rows the scan is building; asked about four pids it is a rounding
    error. Callers wanting the full map — the board's own tty lookup, the
    system route — simply pass nothing, and an *empty* list is answered
    without asking at all, because bare `ps -p` with no pid lists everything
    and would silently restore the cost this argument exists to avoid."""
    out = {}
    if pids is not None:
        pids = list(pids)
        if not pids:
            return out
        argv = ["ps", "-o", "pid=,tty=", "-p", ",".join(str(p) for p in pids)]
    else:
        argv = ["ps", "-Ao", "pid=,tty="]
    r = subprocess.run(argv, capture_output=True,
                       text=True, timeout=10)
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue        # no controlling terminal ("??"), or a blank line
        pid, tty = parts
        if tty in ("??", "-", "?"):
            continue
        try:
            out[int(pid)] = tty if tty.startswith("/") else f"/dev/{tty}"
        except ValueError:
            continue
    return out


#: Every CLI jRemote can spawn into a managed pane, by the name `ps` reports.
#: `codex-code-mode-host` is deliberately absent — it is a helper the codex CLI
#: starts, so counting it would let a pane read as alive on a dead session.
_ENGINE_COMMS = ("claude", "claude.exe", "codex")


def _engine_ttys_from_ps(comms: tuple = _ENGINE_COMMS) -> dict:
    """{pid: tty} for every named agent CLI holding a terminal, from `ps` alone.

    A second pair of eyes for the system test: the board's window count must be
    checkable against something that shares none of its machinery, or the check
    and the thing checked fail together and the test stays green while the app
    is wrong. `ps` knows about processes and terminals and nothing about us."""
    out = {}
    try:
        r = subprocess.run(["ps", "-Ao", "pid=,tty=,comm="], capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return out
    for line in r.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3:
            continue
        pid, tty, comm = parts
        if tty in ("??", "-", "?"):
            continue
        if Path(comm.strip()).name not in comms:
            continue
        try:
            out[int(pid)] = tty if tty.startswith("/") else f"/dev/{tty}"
        except ValueError:
            continue
    return out


def _claude_ttys_from_ps() -> dict:
    """The claude-only reading. The window count wants exactly this: a stray
    terminal running some other CLI is not a jRemote window."""
    return _engine_ttys_from_ps(("claude", "claude.exe"))


def get_claude_processes():
    """Return all running Claude Code processes with session details."""
    labels = session_labels()

    # One walk over the machine, not two, and only the attributes the walk
    # actually decides on.
    #
    # Both questions asked of every process here — "is this the API proxy" and
    # "is this an agent CLI" — are answered by name and cmdline alone. The old
    # shape asked twice: once for proxy pids, then again for ten attributes,
    # paying `create_time`, `memory_info`, `status`, `cwd` and `terminal` on
    # ~1200 processes to keep them for the four that matched. Those are fetched
    # per match now, below. This runs every second inside the process serving
    # live terminals, so the walk it does not do is a stall nobody sees on the
    # other end of one (#30).
    #
    # `cpu_percent` is gone from the fetch entirely: it was never read. The row
    # asks `proc.cpu_percent(interval=0)` further down, and psutil measures
    # that as a delta since the previous call on the same instance — so the
    # fetch here silently became the baseline microseconds earlier and the row
    # reported the noise of that gap, 0.0 for most rows and 581% for one. Asked
    # once per scan it measures across the tick, which is the number the board
    # was always meant to show.
    # `ppid` is not in the walk either, and it is the expensive one — 20ms of
    # the pass on its own, for a value only the matched rows read.
    proxy_pids = set()
    matched = []
    for proc, info in iter_procs(["pid", "name", "cmdline"]):
        cmdline = info.get("cmdline") or []
        cmd_str = " ".join(cmdline)
        # Checked before the engine test, not after it: a proxy process is
        # never a row of its own, only a parent to recognise, and testing it
        # first is what keeps it out of both places at once.
        if "claude-max-api" in cmd_str or "standalone.js" in cmd_str:
            proxy_pids.add(info["pid"])
            continue
        engine = _agent_engine(info.get("name"), cmdline)
        if engine is None:
            continue
        matched.append((proc, info, cmdline, cmd_str, engine))

    # One `ps`, asked only about the processes that turned out to be rows.
    ttys = _tty_map([info["pid"] for _p, info, _c, _s, _e in matched])

    # Enriched only now that the walk is done — `source` reads `proxy_pids`,
    # and a child can be walked before the parent that explains it.
    processes = []
    for proc, info, cmdline, cmd_str, engine in matched:
        try:
            try:
                info.update(proc.as_dict(attrs=[
                    "ppid", "create_time", "memory_info", "status", "cwd",
                    "terminal"]))
            except (psutil.NoSuchProcess, psutil.AccessDenied,
                    psutil.ZombieProcess, SystemError, OSError):
                continue        # died mid-scan — same drop `iter_procs` makes

            mem = info["memory_info"]
            mem_mb = (mem.rss / (1024 * 1024)) if mem else 0
            elapsed = time.time() - (info["create_time"] or time.time())

            session_id = None
            if "--resume" in cmd_str:
                parts = cmdline
                for i, p in enumerate(parts):
                    if p == "--resume" and i + 1 < len(parts):
                        session_id = parts[i + 1]
                        break
            if not session_id and "--session-id" in cmd_str:
                parts = cmdline
                for i, p in enumerate(parts):
                    if p == "--session-id" and i + 1 < len(parts):
                        session_id = parts[i + 1]
                        break

            # `codex exec resume <thread-id> <text>` states its target as a
            # bare positional, and that id is Codex's own thread — never a
            # board handle, so it cannot go in `session_id`. Carried on its own
            # so the board can resolve it to the card it belongs to instead of
            # minting an anonymous `pid-` row beside it (#281). The first
            # UUID-shaped element wins: the prompt text follows the id, so a
            # position-based read would trip over a prompt that quotes one.
            native_id = _codex_native_id(cmdline) if engine == "codex" else ""

            # Identify source by parent process, cmdline, and working directory
            ppid = info.get("ppid")
            proc_cwd = ""
            try:
                proc_cwd = info.get("cwd") or ""
            except Exception:
                pass
            if ppid in proxy_pids or "claude-max-api" in proc_cwd:
                source = "proxy"
            elif "vscode" in cmd_str:
                source = "vscode"
            elif "-p" in cmdline or "--print" in cmdline:
                source = "cli-pipe"
            elif engine == "codex" and _subcommand(cmdline) == "exec":
                # Codex's non-interactive mode, and the exact thing jRemote
                # runs for a phone chat-turn (`turns.py`). It is a pipe run
                # under another name — calling it "cli" said a person had
                # launched it in a terminal, which is how it came to read as a
                # loose interactive session.
                source = "cli-pipe"
            else:
                source = "cli"

            # Build label
            label = labels.get(session_id, "") if session_id else ""
            if not label and source == "proxy":
                # Try to extract model from cmdline for context
                model = ""
                for i, p in enumerate(cmdline):
                    if p == "--model" and i + 1 < len(cmdline):
                        model = cmdline[i + 1].split("/")[-1]
                        break
                label = f"Proxy ({model})" if model else "Proxy session"
            elif not label and source == "vscode":
                label = "IDE session"

            # Shorten home paths
            cwd = proc_cwd
            if cwd.startswith(_HOME_PREFIX):
                cwd = "~/" + cwd[len(_HOME_PREFIX):]
            elif cwd == _HOME:
                cwd = "~/"

            # A spawn's own title (`claude --name …`) and whether a briefing
            # was injected into its system prompt — what lets the board show a
            # handoff window as "Handoff · X" carrying work instead of an
            # anonymous empty prompt. Exact argv elements, never substring on
            # the joined cmdline: the briefing text itself may mention flags.
            window_name = ""
            model = ""
            for i, p in enumerate(cmdline):
                if p == "--name" and i + 1 < len(cmdline):
                    window_name = cmdline[i + 1]
                elif p in ("--model", "-m") and i + 1 < len(cmdline):
                    # Pinned on the command line and recorded nowhere else. A
                    # headless run has no registry row to hold it (the registry
                    # is gated on a live tmux), so without this the board could
                    # not say what a worker was running while it ran — even
                    # though the answer was sitting in its own argv.
                    model = cmdline[i + 1]
            briefed = ("--append-system-prompt" in cmdline
                       or "--append-system-prompt-file" in cmdline)

            processes.append({
                "pid": info["pid"],
                "session_id": session_id,
                "native_id": native_id,
                "label": label,
                "source": source,
                # Which agent CLI is running here. Always present and always a
                # real engine — a row exists only because _agent_engine named
                # one — so a consumer never has to guess "claude" from absence.
                "engine": engine,
                # Controlling terminal, or None for a headless run. The one
                # observable fact about whether a terminal owns this process —
                # argv shape only says how it was launched. Resolved from a
                # fresh `ps` (see `_tty_map`), never psutil's per-process
                # memoized /dev map, which goes stale the moment a new terminal
                # is opened and calls every window since startup headless.
                "tty": ttys.get(info["pid"]) or info.get("terminal"),
                "location": cwd,
                "window_name": window_name,
                "model": model,
                "briefed": briefed,
                "memory_mb": round(mem_mb, 1),
                "cpu": proc.cpu_percent(interval=0),
                "status": info["status"],
                "uptime_minutes": round(elapsed / 60, 1),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    processes.sort(key=lambda p: p["uptime_minutes"], reverse=True)
    return {"processes": processes, "count": len(processes)}
