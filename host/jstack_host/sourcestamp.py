"""Which source this process is actually serving — captured as the bytes load.

The host is an editable install: the package imports straight out of a git
checkout, and a restart ships whatever sits in that tree at that instant,
committed or not. So "which version is running" is a fact about the past —
the tree as of this process's startup — and until now nothing recorded it.
The tree moves on, the version label stays the same, and the machine holds
four copies of jStack (checkout, plugin cache, running host, app build) with
no way to say whether the one answering requests is any of them.

Captured once per process and cached, because the whole point is WHEN it is
read: `server.py` fills it in lifespan and `embed.declare()` fills it as the
embedding server starts, both moments when the loaded bytes and the tree are
still the same thing. A stamp computed lazily at first probe would describe
the tree as of the probe — the exact lie this module exists to end.

Empty sha means the package is not served from a git checkout (a real pip
install). That is not a failure: such an install cannot drift from a tree it
does not have, and its identity is the package version instead.
"""

from __future__ import annotations

import subprocess
import json
import hashlib
import re
from pathlib import Path

_PKG = Path(__file__).resolve().parent

_stamp: dict | None = None


def fingerprint(package: Path) -> str:
    value = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        value.update(str(path.relative_to(package)).encode() + b"\0" + path.read_bytes())
    return value.hexdigest()


def _git(*args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(_PKG), *args],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def capture() -> dict:
    """`{"sha", "dirty", "root"}` for the source this process loaded.

    `dirty` covers the served package directory only — an uncommitted edit in
    the repo's docs does not change what this process runs, but one in
    `jstack_host/` means the sha alone no longer names the serving bytes.
    """
    global _stamp
    if _stamp is None:
        sha = _git("rev-parse", "HEAD")
        _stamp = {
            "sha": sha,
            "dirty": bool(sha) and bool(_git("status", "--porcelain", "--", str(_PKG))),
            "root": _git("rev-parse", "--show-toplevel") if sha else "",
        }
        # Read the version belonging to these loaded sources, not whichever
        # editable distribution happens to be registered in the interpreter.
        manifest = _PKG.parent.parent / "plugins/jstack/.claude-plugin/plugin.json"
        if manifest.is_file():
            _stamp["version"] = json.loads(manifest.read_text())["version"]
        # An immutable release archive has no .git. Its identity is packaged
        # into the signed stack artifact, not copied from desired fleet state.
        identity = _PKG.parent / "release-identity.json"
        if identity.is_file():
            data = json.loads(identity.read_text())
            _stamp.update(sha=data["sha"], release=data["release"],
                          dirty=data.get("package_sha256") != fingerprint(_PKG))
            if data.get("version"):
                _stamp["version"] = data["version"]
            if data.get("date"):
                _stamp["date"] = data["date"]
            # A debug build — a branch that is not a line, or a commit no
            # bump covers — says so to `/host` and the menu, never passing
            # for the release its version would otherwise name.
            if data.get("debug"):
                _stamp["debug"] = True
            # Where these bytes were published from. A signed bundle has no
            # `.git` to ask, and the one caller that needed this — the joiner
            # file's installer URL — was asking git inside the app bundle and
            # getting nothing, so no installed Hub could write a joiner file.
            if data.get("github_repo"):
                _stamp["github_repo"] = data["github_repo"]
    return dict(_stamp)


def github_repo() -> str:
    """`owner/name` this distribution was published from, or "" in a checkout
    that has no origin on GitHub."""
    stamp = capture()
    if repo := stamp.get("github_repo"):
        return repo
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([\w.-]+/[\w.-]+?)(?:\.git)?",
                         _git("remote", "get-url", "origin"))
    return match[1] if match else ""


def describe(stamp: dict | None = None) -> str:
    """The stamp as one log-line token: `abc123def456`, `abc123def456+dirty`,
    or `not a checkout`."""
    s = stamp if stamp is not None else capture()
    if not s.get("sha"):
        return "not a checkout"
    return s["sha"][:12] + ("+dirty" if s.get("dirty") else "")
