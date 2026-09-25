"""Where the host package is, for a hook running from whichever copy of the plugin.

`parents[3]` of a hook is the repo only in a checkout. Claude Code runs an
installed plugin from its cache — `~/.claude/plugins/cache/<marketplace>/jstack/
<version>/`, Codex the same shape under `~/.codex/plugins/cache/` — where
`parents[3]` holds no `host/`, so every hook that composed the path itself
exited 0 having done nothing on every installed machine, while the suites,
run from a checkout, passed. Every host import goes through `load()` here.

A load that fails leaves the hook silent — a hook that raises is switched off
for the session — and one line in `~/.claude/jstack/host-load.txt` saying why,
which `jstack-doctor` reads; a successful load removes it. Stdlib-only and no
subprocess: `attention.py` runs this beside every tool call of every session,
and the checkout case costs one stat, the cache case one small JSON read more.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

#: The plugin directory this copy of the hooks belongs to.
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "jstack"
#: What makes a directory a host checkout rather than a directory of that name.
PACKAGE_INIT = Path("host") / "jstack_host" / "__init__.py"
VENV_PYTHON = Path("host") / ".venv" / "bin" / "python3"
REEXEC = "JSTACK_HOST_REEXEC"

# Popped, not read: the marker is for this process's decision alone, and a
# child the hook spawns — a store helper, another hook — must not inherit a
# "never re-exec" it did not earn.
_REEXECED = os.environ.pop(REEXEC, "") == "1"

_UNSET = object()
_resolved = _UNSET
_stdin: "bytes | None" = None


def reason_path() -> Path:
    """Where the last failed load says why. Beside `review.json` and the
    session-files state, the directory the plugin's hooks already own."""
    return Path.home() / ".claude" / "jstack" / "host-load.txt"


def _is_checkout(path: "Path | None") -> bool:
    return path is not None and (path / PACKAGE_INIT).is_file()


def claude_marketplaces(home: Path) -> "dict[str, Path]":
    """`{name: path}` for every directory-source entry Claude Code knows."""
    try:
        data = json.loads(
            (home / ".claude" / "plugins" / "known_marketplaces.json").read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for name, entry in (data.items() if isinstance(data, dict) else ()):
        source = entry.get("source") if isinstance(entry, dict) else None
        if (isinstance(source, dict) and source.get("source") == "directory"
                and source.get("path")):
            out[str(name)] = Path(str(source["path"])).expanduser()
    return out


_CODEX_TABLE = re.compile(
    r'^\[marketplaces\.(?:"([^"]+)"|([^\]\s]+))\]\s*$(.*?)(?=^\[|\Z)', re.M | re.S)


def codex_marketplaces(home: Path) -> "dict[str, Path]":
    """`{name: path}` for every local-source marketplace in Codex's config.

    Read by pattern, not `tomllib`: that module is 3.11+, and this runs under
    whatever `python3` the PATH gives — the 3.9 case is the one it exists for.
    """
    codex = Path(os.environ.get("CODEX_HOME") or home / ".codex")
    try:
        text = (codex / "config.toml").read_text()
    except OSError:
        return {}
    out = {}
    for match in _CODEX_TABLE.finditer(text):
        body = match.group(3)
        kind = re.search(r'^source_type\s*=\s*"([^"]*)"', body, re.M)
        source = re.search(r'^source\s*=\s*"([^"]*)"', body, re.M)
        if source and (kind is None or kind.group(1) == "local"):
            out[match.group(1) or match.group(2)] = Path(source.group(1)).expanduser()
    return out


def find_checkout(plugin_root: Path = PLUGIN_ROOT,
                  home: "Path | None" = None) -> "Path | None":
    """The host checkout serving the plugin at `plugin_root`, or None.

    A candidate counts only if it holds `host/jstack_host/__init__.py`; first
    hit wins. (1) The checkout the plugin sits in — dev and the suites.
    (2) The directory marketplace it was installed from: `install.sh` runs
    `claude plugin marketplace add "$CHECKOUT"`, recorded in
    `known_marketplaces.json` as `{"source": "directory", "path": …}`, and
    `codex_setup.py` runs `codex plugin marketplace add <checkout>`, recorded
    in `config.toml` as `[marketplaces.<name>] source = …`. The entry the
    cache path names is tried first, then any directory entry shipping this
    plugin's `_host.py` — a cache laid out under another name still resolves.
    """
    own = plugin_root.parents[1] if len(plugin_root.parents) > 1 else None
    if _is_checkout(own):
        return own
    home = home or Path.home()
    # cache/<marketplace>/<plugin>/<version>: the marketplace is two up, and
    # only when the segment above the version is this plugin's name.
    named = plugin_root.parent.parent.name if plugin_root.parent.name == PLUGIN_NAME else ""
    for store in (claude_marketplaces, codex_marketplaces):
        entries = store(home)
        if named and _is_checkout(entries.get(named)):
            return entries[named]
        for path in entries.values():
            if (_is_checkout(path)
                    and (path / "plugins" / PLUGIN_NAME / "hooks" / "_host.py").is_file()):
                return path
    return None


def checkout() -> "Path | None":
    """`find_checkout()` for this copy of the plugin, once per process."""
    global _resolved
    if _resolved is _UNSET:
        _resolved = find_checkout()
    return _resolved


def requires_python(root: Path) -> "tuple[int, ...]":
    """The host's `requires-python` floor, or () when it declares none."""
    try:
        text = (root / "host" / "pyproject.toml").read_text()
    except OSError:
        return ()
    match = re.search(r'^requires-python\s*=\s*"\s*>=\s*([\d.]+)', text, re.M)
    return tuple(int(p) for p in match.group(1).split(".") if p) if match else ()


def read_stdin() -> bytes:
    """The hook's stdin, read once and kept for a re-exec to hand over."""
    global _stdin
    if _stdin is None:
        _stdin = sys.stdin.buffer.read()
    return _stdin


def record(reason: str) -> None:
    """One line for the doctor. Never raises — the hook's silence is the
    contract, and this line is only its receipt."""
    try:
        path = reason_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        hook = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else "?"
        path.write_text(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')}\t{hook}\t"
                        f"{' '.join(reason.split())}\n")
    except OSError:
        pass


def _clear() -> None:
    try:
        reason_path().unlink()
    except OSError:
        pass


def _in_venv(root: Path) -> bool:
    try:
        return Path(sys.prefix).resolve() == (root / "host" / ".venv").resolve()
    except OSError:
        return False


def _reexec(root: Path, allowed: bool, reason: str) -> None:
    """Replace this process with the same hook under the checkout's venv.

    Same argv, same stdin bytes. Stdin is what a re-exec loses — bytes the
    first process read are gone from fd 0 — so a hook that reads its payload
    before loading takes it through `read_stdin()`, and the bytes go back on
    fd 0 through an unlinked temp file; a hook that loads first hands over an
    untouched fd 0. `JSTACK_HOST_REEXEC` marks the child, which never re-execs.
    Returns only when replacing is not possible, having recorded why.
    """
    target = root / VENV_PYTHON
    if not allowed:
        record(f"{reason}; this caller does not re-exec")
        return
    if _REEXECED:
        record(f"{reason}; already re-executed once under {sys.executable}")
        return
    if not target.is_file() or _in_venv(root):
        record(f"{reason}; no interpreter to re-exec under — {target} is absent")
        return
    if _stdin is not None:
        fd, path = tempfile.mkstemp(prefix="jstack-hook-stdin-")
        try:
            view = memoryview(_stdin)
            while view:
                view = view[os.write(fd, view):]
            os.lseek(fd, 0, os.SEEK_SET)
            os.dup2(fd, 0)
        finally:
            os.close(fd)
            os.unlink(path)
    env = dict(os.environ)
    env[REEXEC] = "1"
    sys.stdout.flush()
    sys.stderr.flush()
    os.execve(str(target), [str(target), *sys.argv], env)


def load(*names: str, reexec: bool = True):
    """`jstack_host.<name>` for each name — one module, or a tuple of them.

    A hook's shebang is `python3` off PATH: Apple's 3.9 on a stock Mac, where
    the host declares `requires-python >=3.11`. Below that floor, or on an
    import failing for a dependency outside the package, the hook re-execs
    under `<checkout>/host/.venv/bin/python3` (`_reexec`).

    Raises ImportError when the host cannot be loaded, after recording why;
    the callers already turn any exception into a silent exit 0. `reexec=False`
    is for a CLI that has already written output: replacing it mid-run would
    print that output twice.
    """
    root = checkout()
    if root is None:
        record(f"no host checkout for the plugin at {PLUGIN_ROOT}: not beside it, "
               "and no directory marketplace in known_marketplaces.json or "
               "Codex's config.toml holds host/jstack_host")
        raise ImportError("jstack_host: no host checkout found")
    host = str(root / "host")
    if host not in sys.path:
        sys.path.insert(0, host)
    floor = requires_python(root)
    if floor and sys.version_info[:len(floor)] < floor:
        _reexec(root, reexec, f"python {sys.version.split()[0]} at {sys.executable} is below "
                      f"the host's requires-python >={'.'.join(map(str, floor))}")
        raise ImportError("jstack_host: interpreter below requires-python")
    try:
        modules = tuple(importlib.import_module("jstack_host." + n) for n in names)
    except ImportError as error:
        missing = str(getattr(error, "name", "") or "")
        if missing != "jstack_host" and not missing.startswith("jstack_host."):
            _reexec(root, reexec, f"importing the host under {sys.executable} failed: {error}")
        else:
            record(f"the host at {host} does not provide what was asked: {error}")
        raise
    _clear()
    return modules[0] if len(modules) == 1 else modules
