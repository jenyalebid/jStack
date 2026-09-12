"""The record an embedded host leaves behind for the shell.

`install_host` writes a LaunchAgent, and that plist is the one record of what
an installed host *is* — which port, which state dir, which profile. Every
read command adopts it before it looks at anything
(`adopt_installed_environment`), because a shell has none of that environment
and would otherwise answer about a host it invented.

A host mounted into another server has no LaunchAgent at all, by design, and
so it had no record. That is the whole of #34. The profile describing it is
importable only from the embedding server's own import root, so a bare
`jstack-host` in a terminal imported nothing, resolved the *default* profile,
and reported confidently on a host that does not exist on this Mac: `not
installed`, `NO TOKEN`, a state dir nothing was reading. Read plainly that
says the host is down. It was up and serving all day, and the wrong answer led
to a second host being installed beside the real one — a fresh empty state dir
and an empty device roster, while the live host carried on.

So an embedded host leaves this instead: one small JSON file at a fixed path,
written by the embedding server as it starts.

**Answers, not only a pointer.** The import root is in here so the profile can
be imported where that is possible — and then the live profile wins, because a
marker is a copy made at declare time and the profile is the host's own truth.
But usually it is not possible: the embedding server runs its own virtualenv
with its own modules, and `pip install jstack-host` into some other Python has
no way to import a profile that pulls them in. A marker that only pointed
would be a marker that works on one Mac's PATH and not the next, which is the
same class of failure it exists to end. So the answers travel too.

**A fixed path, resolved at call time.** `~/.local/state/jremote/` is where a
host with no configuration at all resolves its state — the one directory a
process that knows nothing will look in — which is exactly the situation a
shell-side command is in before it has adopted anything. `Path.home()` rather
than the module constant `hostenv.HOME`: this file is written by one process
and read by another, and the tests must not write into the real one.

Nothing secret is recorded here. The token *path* is a fact about the host;
the token is not, and a marker in a world-readable state dir is no place for
one.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import hostenv

#: Override for a second instance on one Mac, and for the tests.
MARKER_ENV = "JREMOTE_EMBED_MARKER"


def marker_path() -> Path:
    env = os.environ.get(MARKER_ENV, "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "state" / "jremote" / "embedded.json"


def _import_root() -> str:
    """The directory the profile module was imported from, or ''.

    Taken off the module's own spec rather than guessed from `sys.path`: the
    profile is already imported by the time anyone calls `declare`, and its
    origin is the one exact answer. A package origin ends in `__init__.py`, so
    the root is two levels up there and one level up for a plain module.
    """
    mod = sys.modules.get(hostenv.profile_module_name())
    origin = getattr(getattr(mod, "__spec__", None), "origin", "") or ""
    if not origin or origin == "built-in":
        return ""
    path = Path(origin).resolve()
    if path.name == "__init__.py":
        path = path.parent
    return str(path.parent)


def _agent_label() -> str:
    """The launchd job this host actually runs under, or ''.

    An embedded host has no agent of its own, but it is not unmanaged: the
    server it is mounted into has one, and that job is what a Restart or a
    Shut Down has to act on. Nothing else on disk records it, so the menu bar
    resolved the package default `com.jremote.host`, found no plist, decided
    nothing was installed and hid Restart and Shut Down on the one Mac whose
    hub you would actually operate — twice now, because the label lived only
    in whatever the installing shell happened to export.

    `XPC_SERVICE_NAME` is launchd's own answer, set in the environment of every
    job it starts, so this is read and not guessed. Empty when the server was
    started by hand from a terminal, which is correct: there is then no agent
    to offer, and the installer's backfill skips an empty value.
    """
    label = os.environ.get("XPC_SERVICE_NAME", "")
    # launchd gives an un-jobbed process a placeholder of this shape rather
    # than nothing at all; it names no plist, and pinning it would put the
    # menu back to hunting for an agent that cannot exist.
    return "" if label.startswith("0x") or label == "unknown" else label


def declare(*, port: int, server: str = "", root: str | Path = "") -> Path | None:
    """Record this process as the host embedded in `server`, on `port`.

    Called by the embedding server, at the point where it knows its own port —
    which is the only place that fact exists. `jstack_host` is mounted as a set
    of routers; nothing in it is told what address the surrounding application
    will bind, and a marker that guessed would be the `serving 9090 — correct
    by luck` this issue is already about.

    Returns None and writes nothing when this is not an embedded host. The
    profile's `embedded_in` is what decides — the same answer `doctor` and
    `status` grade on — so a standalone host that called this by mistake
    cannot leave a record claiming otherwise.
    """
    profile = hostenv.profile()
    server = server or getattr(profile, "embedded_in", "") or ""
    if not server:
        return None
    record = {
        "server": server,
        "port": int(port),
        "root": str(root) or _import_root(),
        "profile_module": hostenv.profile_module_name(),
        "profile": getattr(profile, "name", ""),
        "state_dir": str(hostenv.state_dir()),
        "token_path": str(hostenv.token_path()),
        "agent_label": _agent_label(),
    }
    path = marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written whole and moved into place. A reader that catches this file half
    # written gets no JSON, falls back to the default profile, and reports the
    # host as absent — the exact failure being fixed, arriving intermittently.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(record, indent=2) + "\n")
    tmp.replace(path)
    return path


def read() -> dict:
    """The marker, or `{}`. Never raises: a machine with no embedded host is
    the ordinary case, and so is a file somebody hand-edited into nonsense."""
    try:
        data = json.loads(marker_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def clear() -> None:
    """Forget the embedded host — for a server that stops mounting one."""
    marker_path().unlink(missing_ok=True)


def adopt() -> bool:
    """Point this process at the embedded host the marker describes.

    The import root first, and the recorded answers only if importing the
    profile from it did not work. A live profile is the host's own truth and
    the marker is a copy of it; preferring the copy would mean a host that
    moved its state dir kept being reported at the old one until something
    happened to rewrite the file.

    The root is *appended* to `sys.path`, never prepended. It is another
    application's import root — on the machine this was written for it holds a
    `lib/` and a `dashboard/` — and putting it ahead of everything would let it
    shadow this package's own imports, and the standard library's, for the rest
    of the process. Appending is enough: the one name being resolved out of it
    is the profile module, which nothing else provides.

    Everything it sets goes *beneath* the environment, like the plist does —
    an explicit export or `--state-dir` still wins.

    Returns whether a marker was there at all.
    """
    record = read()
    if not record:
        return False

    root = str(record.get("root") or "")
    if root and Path(root).is_dir() and root not in sys.path:
        sys.path.append(root)
    module = str(record.get("profile_module") or "")
    if module:
        os.environ.setdefault("JREMOTE_PROFILE_MODULE", module)
    hostenv.reset_profile()
    if getattr(hostenv.profile(), "embedded_in", ""):
        return True

    # It did not import here — the usual case, and not a fault. Fall back to
    # what it answered on the machine where it did.
    for key, field in (("JREMOTE_STATE_DIR", "state_dir"),
                       ("JREMOTE_TOKEN_PATH", "token_path")):
        value = str(record.get(field) or "")
        if value:
            os.environ.setdefault(key, value)
    hostenv.reset_profile()
    return True


def server() -> str:
    """What this host is embedded in, or '' — the profile's own answer first.

    The one accessor `status` and `doctor` both ask, because the two of them
    disagreeing about the same machine is half of #34: `doctor` had an
    `embedded_in` branch from the day it was written and `status` did not, so
    one called the host embedded and healthy while the other called it
    uninstalled.
    """
    declared = getattr(hostenv.profile(), "embedded_in", "") or ""
    return str(declared) or str(read().get("server") or "")


def port() -> int | None:
    """The port the embedded host declared, or None. The counterpart of
    `install_host.installed_port` — same question, other kind of record."""
    value = read().get("port")
    return value if isinstance(value, int) and 1 <= value <= 65535 else None
