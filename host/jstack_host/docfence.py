"""Which files on this machine a connected client is allowed to read.

The host hands the app absolute paths — a rule, a skill, a SYSTEM.md, a seat's
CLAUDE.md — and the app asks for them back. Without a fence that round trip is
"any file on this Mac, over the network", on a machine that also stores
credentials. So every read of a document goes through `fenced_path()`, and the
answer is checked on the *resolved* path: half of what the inventory lists is a
symlink into a plugin checkout, and a symlink is exactly how a fence that only
looked at the name would be walked around.

This used to live in the embedding host's own inventory module, and the package
reached back out for it with a relative import that escaped the package. That
worked while the package was a folder inside a bigger tree and raised
ImportError everywhere else — which is to say `show()` was dead on every
standalone host, and the fence, the one thing a host must not lose, was the
piece that did not come along. It belongs here: the package serves the route,
so the package owns the rule.

The roots are asked of `hostenv`, not spelled — an instance whose agents live
somewhere other than `~/Agents` has to be able to read its own agents' files.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import hostenv, plugin_paths

HOME = Path.home()

# Past this, a client is scrolling a file nobody reads on a phone. No rule,
# skill or SYSTEM.md on a jStack machine is within an order of magnitude of it.
MAX_READ_BYTES = 400_000


def read_roots() -> tuple[Path, ...]:
    """Every directory a document may be read out of.

    The `~/.claude` tree (what a session loads, plus `plans` — the authored
    plan a stage row points back to), this instance's agent root (seats and
    their workspaces), `~/Systems` (the canonical docs), and every
    live marketplace checkout — derived rather than hardcoded to one path,
    because a plugin from a directory marketplace publishes its rows at the
    checkout's real location, and the fence has to admit exactly the set that
    resolution can produce or the viewer opens a row it cannot read.
    """
    claude = HOME / ".claude"
    fixed = (claude / "rules", claude / "skills", claude / "commands",
             claude / "plugins" / "cache", claude / "seats", claude / "plans",
             hostenv.instance_root(), HOME / "Systems")
    try:
        live = tuple(plugin_paths.live_marketplace_roots().values())
    except Exception:  # noqa: BLE001 — a broken marketplace file narrows the
        live = ()      # fence, which is the safe direction to fail in.
    return fixed + live


def fenced_path(raw: str) -> Path:
    """The resolved path a client is allowed to read, or PermissionError.

    Markdown only, under the roots above, after symlinks are followed. Both
    conditions are checked on the resolved path.

    Public because the fence has a second caller: anything that hands the app
    a `jremote://doc` link (see `showdoc.py`) has to know *before* opening a
    window whether the file will come back through `/context/file` at all. One
    fence, asked in two places — a second copy of this rule is a second thing
    to forget to tighten.
    """
    path = Path(os.path.expanduser(raw.strip())).resolve()
    if path.suffix.lower() != ".md" or not any(path.is_relative_to(r)
                                               for r in read_roots()):
        raise PermissionError(f"{raw!r} is outside the context roots")
    return path


# The model never sees bytes. Four bytes per token is the ratio the rest of the
# stack already estimates with, so two meters never disagree about one file.
BYTES_PER_TOKEN = 4


def file_text(raw: str) -> dict:
    """One fenced document's text, by the path the host published.

    PermissionError outside the fence, FileNotFoundError when it isn't there —
    the caller turns those into 403 and 404, which are different answers and
    have to stay different: "you may not read that" and "there is nothing
    there" are the two facts a client needs to tell apart.
    """
    path = fenced_path(raw)
    if not path.is_file():
        raise FileNotFoundError(f"no file {raw!r}")
    data = path.read_bytes()
    text = data[:MAX_READ_BYTES].decode("utf-8", errors="replace")
    return {
        "path": str(path),
        "name": path.name,
        "text": text,
        "bytes": len(data),
        "tokens": round(len(data) / BYTES_PER_TOKEN),
        "truncated": len(data) > MAX_READ_BYTES,
    }
