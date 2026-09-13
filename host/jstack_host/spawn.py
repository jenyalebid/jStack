"""Open a managed (phone-drivable) session from the desk — the CLI face of
`open_managed` for desk-side spawners.

The `open-terminal-here` override calls this when a spawn command (handoff,
splitoff, an external audit) runs from inside a managed session: the new
session should land managed too, not as a raw Mac-only window. The spawn's
product is a new terminal — and it opens **where the spawn was driven**: the
sid is registered up front, so the board shows a real, named, phone-drivable
row immediately, then the origin session's driver decides the window. Typed
on the Mac (or from nowhere the host can see): a jRemote thread window on
the desk, as ever (the app is the terminal now; iTerm stays an on-demand
viewer). Typed on a device: no Mac window — the device gets the open frame
down its own PTY socket and shows a window per its own setting, or nothing
(the board row is the visibility, same as every phone-driven open). Under
the board invariant a window that could not come up leaves a live, drivable
session rather than tearing down finished work; only a machine with no
jRemote app at all refuses upfront (exit 75, before anything exists) so the
adapter can fall back to a raw window.

Run from the Infrastructure root:

    .venv/bin/python3 -m jstack_host.spawn --cwd DIR [--sid SID]
        [--resume] [--name TITLE] [--prompt-file PATH] [--first-prompt TEXT]
        [--claude-args "FLAT"]

`--prompt-file` is the adapter's briefing contract: the pane's shell reads the
file into a variable and deletes it before `claude` starts, so a multi-line
briefing never has to survive quoting into tmux and nothing lingers on disk.
`--first-prompt` is the other half of a spawn that is a TASK rather than a
staged context (`/takeover`): the briefing is standing mandate in system-prompt
space, this is claude's positional argument, so the session starts working
instead of waiting at an empty box. `--claude-args` is one flat, pre-tokenized
string spliced verbatim after the standard flags — exactly the stock adapter's
pass-through contract.

Prints the sid on success. Exit 75 when the jRemote app is not installed (and
then no session exists), so the adapter falls back to a raw window.
"""

import argparse
import os
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

_DASHBOARD = "http://127.0.0.1:9090/api/jremote/v1"


def build_shell_parts(name: str, prompt_file: str, claude_args: str,
                      first_prompt: str = "") -> tuple[str, str]:
    """(prelude, extra) for `open_managed` — the read-brief idiom and the
    flags that reference it, quoted for the pane's shell.

    `first_prompt` is claude's POSITIONAL argument, so it goes last — after the
    flags and after the caller's pass-through args, which is the only place
    `claude [options] [prompt]` accepts it. It is shell-quoted rather than
    spliced verbatim: unlike `claude_args`, this string is user prose and may
    carry quotes, `$`, or backticks that the pane's shell would otherwise eat.
    """
    prelude, extra = "", ""
    if prompt_file:
        q = shlex.quote(prompt_file)
        prelude = f'__JR_SP="$(cat {q})" && rm -f {q} && '
        extra = '--append-system-prompt "$__JR_SP"'
    if name:
        extra += (" " if extra else "") + f"--name {shlex.quote(name)}"
    if claude_args:
        extra += (" " if extra else "") + claude_args
    if first_prompt:
        extra += (" " if extra else "") + shlex.quote(first_prompt)
    return prelude, extra


def agent_base_for(cwd: str) -> str:
    """The agent this workspace belongs to, '' for a non-agent dir — the same
    resolution the board applies to the session's project dir."""
    from .hostenv import project_dir_to_agent
    loc = str(Path(cwd).expanduser()).rstrip("/")
    parsed = project_dir_to_agent(loc.replace("/", "-").replace(".", "-"))
    return parsed[0] if parsed else ""


def origin_sid() -> str:
    """The managed session this spawn was typed into, '' when not in one.

    A handoff/splitoff runs inside the origin session's own tmux pane, so
    the pane's env names it: $TMUX carries the socket (only the jremote
    socket counts) and $TMUX_PANE resolves to the `jr-<sid>` session name.
    Raw iTerm windows and headless runs have neither — no origin, and the
    window stays on the Mac."""
    tmux_env = os.environ.get("TMUX", "")
    pane = os.environ.get("TMUX_PANE", "")
    if not tmux_env or not pane:
        return ""
    sock = tmux_env.split(",")[0]
    from . import managed
    if Path(sock).name != managed._SOCK:
        return ""
    try:
        r = subprocess.run([managed._TMUX, "-S", sock, "display-message",
                            "-t", pane, "-p", "#S"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    name = (r.stdout or "").strip()
    if r.returncode != 0 or not name.startswith("jr-"):
        return ""
    # tmux names truncate (`jr-` + sid[:8]); the open registry holds the
    # full sid — same reverse mapping the reaper uses. Unknown to the
    # registry ⇒ no origin: the Mac fallback is the safe answer.
    for sid in managed.open_registry():
        if managed._name(sid) == name:
            return sid
    return ""


def _dashboard_post(url, json=None, headers=None, timeout=None):
    import httpx
    return httpx.post(url, json=json, headers=headers, timeout=timeout)


def _route_window(origin: str, new_sid: str, cwd: str) -> str:
    """Ask the dashboard where the spawn's window belongs.

    "device" — the driving device got the open frame, no Mac window.
    "none" — a device drove it but can't be reached: create quietly.
    "mac" — desk-driven, no origin, or *any* failure: today's behavior is
    the fallback, a desk handoff must still open somewhere."""
    if not origin:
        return "mac"
    try:
        from .devices import internal_token
        token = internal_token()
        if not token:
            return "mac"
        r = _dashboard_post(f"{_DASHBOARD}/sessions/{origin}/route-spawn",
                            json={"new_sid": new_sid, "cwd": cwd},
                            headers={"Authorization": f"Bearer {token}"},
                            timeout=5)
        if r.status_code != 200:
            return "mac"
        return r.json().get("route", "mac")
    except Exception:  # noqa: BLE001 — dashboard down ⇒ desk behavior
        return "mac"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--sid", default="")
    ap.add_argument("--resume", action="store_true",
                    help="resume an existing transcript (splitoff) instead of "
                         "starting fresh under --session-id")
    ap.add_argument("--name", default="")
    ap.add_argument("--prompt-file", default="")
    ap.add_argument("--first-prompt", default="",
                    help="claude's positional prompt — the new session starts "
                         "on it instead of waiting at an empty box")
    ap.add_argument("--claude-args", default="")
    a = ap.parse_args(argv)

    from . import desk, managed

    cwd = str(Path(a.cwd).expanduser())
    if not Path(cwd).is_dir():
        print(f"spawn: no such directory: {cwd}", file=sys.stderr)
        return 64
    # Refused before anything exists: no app ⇒ no window could ever come up,
    # and the adapter's raw-window fallback still has the untouched briefing.
    if not Path(desk.APP).exists():
        print(f"spawn: {desk.APP} not installed", file=sys.stderr)
        return 75
    sid = a.sid or str(uuid.uuid4())
    prelude, extra = build_shell_parts(a.name, a.prompt_file, a.claude_args,
                                       a.first_prompt)
    # Registered-first; the board row is the spawn's visibility from here on.
    managed.record_open(sid, agent_base_for(cwd), name=a.name)
    managed.open_managed(sid, cwd, resume=a.resume, extra=extra, prelude=prelude)
    # The window opens where the spawn was driven: a handoff typed on the
    # iPad gets its open frame down that device's own socket ("device"),
    # a device that can't be reached gets no window at all ("none" — the
    # board row is the visibility), and everything else keeps the Mac
    # window — including every failure, because a desk handoff must still
    # open somewhere.
    route = _route_window(origin_sid(), sid, cwd)
    if route == "none":
        print("spawn: device-driven — on the board, no window opened",
              file=sys.stderr)
    elif route != "device" and not desk.open_thread(sid, cwd):
        # The session is up, on the board, drivable from every device — a
        # window that failed to open is not a reason to end it.
        print("spawn: jRemote window did not open — session is on the board",
              file=sys.stderr)
    print(sid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
