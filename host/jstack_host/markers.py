"""Where a hook records that it has already said something — spelled once.

A marker is a file whose existence means "this session has been told this":
written by the plugin's hooks, read back by `environment.announced` so a screen
can tell a setting that is in play from one that is merely armed. The
convention lives in the host because the host is the lower layer — the plugin
imports `jstack_host` and never the reverse, so a path both sides must spell
identically can only have one home, and this is it.

Stdlib only, and no `store` import, deliberately. `plan-mode-watch.py` reads a
marker on every prompt and every Stop of every session on the machine and
decides FROM it whether it needs the host at all; reaching a path in /tmp
through a module that opens a database would put ~30ms of store import on that
fast path, where this costs nothing measurable. `inject-path-rules.py` may not
import even this — it is stdlib-only by its own contract and keeps its own copy
of `safe_dir_name`, which `test_environment.py` reads beside this one so
neither can move alone.
"""

import os
import re
from pathlib import Path

DEFAULT_CACHE_ROOT = Path("/tmp/jstack-rule-cache")


def safe_dir_name(name: str) -> str:
    """Arbitrary text as one path component, byte-for-byte the hooks' copy."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:128] or "_"


def cache_root() -> Path:
    """The marker root: `JSTACK_CACHE_ROOT`, else `/tmp/jstack-rule-cache`.

    Read per call rather than captured at import, because the thing that moves
    it is a test pointing it at a tmpdir — in-process for the host's, in the
    environment for the plugin's, and a constant folded at import would serve
    the first of those the machine's real root.
    """
    override = os.environ.get("JSTACK_CACHE_ROOT")
    return Path(override).expanduser() if override else DEFAULT_CACHE_ROOT


def session_cache(session_id: str) -> Path:
    """One session's marker dir — a path, and deliberately not a mkdir.

    The writing side creates it. A reader that created it would leave a
    directory behind for every session that was merely LOOKED at, which is
    every session the Work screen is opened on, under a root nothing ever comes
    back to sweep.

    An empty id lands under `_unknown` rather than at the root, matching the
    hooks: markers for a session nobody can name are worth nothing, but at the
    root they would be read as some other session's.
    """
    return cache_root() / safe_dir_name(session_id or "_unknown")


def announce_marker(session_id: str, key: str, value: str) -> Path:
    """The file `env-announce.py` writes once a setting has spoken.

    Keyed on the VALUE and not on the setting alone: a flip mid-session has to
    re-arm the line rather than be swallowed by the marker the old value left.
    That is also what makes the file evidence rather than a counter — the name
    says which value did the speaking, and nothing else could tell
    `environment.announced` that the value in force now is the one that was
    said.
    """
    return session_cache(session_id) / (
        f"env-{safe_dir_name(key)}-{safe_dir_name(value)}.marker")
