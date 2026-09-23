"""Who took access away — the actor behind every revocation the store records.

The store writes the `access_audit` row in the same transaction as the
forget/revoke it describes (jStack#107: a leaf was forgotten and revoked with
nothing naming who). The caller is what the store cannot know, so the entry
point says it — a route wraps its call in `acting(from_request(...))`, the CLI
wraps every subcommand in `acting(from_cli(...))`. A ContextVar, not a
parameter threaded through grants/devices/hub_prefs: a parameter is what the
next caller forgets to pass. Nothing set still writes a row, marked
`unattributed` with the process and call stack — a lead, not a dead end.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import sys
import traceback
from collections.abc import Iterator

_actor: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "jstack_audit_actor", default=None)

#: Environment that names the agent session driving a CLI call. Only ids the
#: engines actually export — a guessed name would record nothing and look fine.
_SESSION_ENV = ("CLAUDE_CODE_SESSION_ID", "JSTACK_SESSION_ID")


@contextlib.contextmanager
def acting(actor: dict) -> Iterator[None]:
    token = _actor.set(actor)
    try:
        yield
    finally:
        _actor.reset(token)


def current() -> dict:
    """The actor for the revocation about to be written — never empty."""
    actor = _actor.get()
    if actor is not None:
        return actor
    return {"via": "unattributed", "origin": _process(),
            "detail": {"stack": _stack()}}


def from_request(request, device_id: str = "") -> dict:
    """A route's caller: the device it authenticated as, and from where."""
    name = ""
    if device_id:
        try:
            from . import devices
            name = (devices.row(device_id) or {}).get("name", "")
        except Exception:  # noqa: BLE001 — naming the actor must not fail the action
            pass
    headers = getattr(request, "headers", {}) or {}
    client = getattr(getattr(request, "client", None), "host", "") or ""
    url = getattr(request, "url", None)
    return {
        "via": f"{getattr(request, 'method', 'POST')} {getattr(url, 'path', '')}",
        "actor": device_id, "actor_name": name, "origin": client,
        "user_agent": headers.get("user-agent") or "",
        "detail": {k: v for k, v in (("build", headers.get("x-jremote-build")),)
                   if v},
    }


def from_cli(command: str) -> dict:
    """A CLI call: which subcommand, which process tree, which agent session."""
    detail = {"cwd": os.getcwd(), "argv": sys.argv[:8]}
    for key in _SESSION_ENV:
        if os.environ.get(key):
            detail["session"] = os.environ[key]
            break
    return {"via": f"cli:{command}", "actor": os.environ.get("USER", ""),
            "origin": _process(), "detail": detail}


def _process() -> str:
    ppid = os.getppid()
    return f"pid {os.getpid()} ppid {ppid} ({_command(ppid)})"


def _command(pid: int) -> str:
    try:
        import subprocess
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=2).stdout
        return out.strip()[:160] or "?"
    except Exception:  # noqa: BLE001
        return "?"


def _stack() -> list[str]:
    """The jStack frames that led here, innermost last, store frames dropped."""
    frames = []
    for f in traceback.extract_stack()[:-2]:
        if "jstack_host" in f.filename and not f.filename.endswith(
                ("audit.py", "store.py")):
            frames.append(f"{os.path.basename(f.filename)}:{f.lineno} {f.name}")
    return frames[-8:]
