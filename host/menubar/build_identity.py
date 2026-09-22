"""Name a source build of the bar app: the commit it came from, and the day.

No counter. A build number answered "which one is newer" by counting, which
is a fact about this machine's build history rather than about the software —
two machines building the same commit disagreed, and the number told nobody
which source either was running. The commit says what the code is and the
date says when it was taken, so the identity is `<version>+<date>.<sha8>`
and whichever commit is promoted to production is by definition the newest
release there is.

Provenance comes from whichever of three sources actually knows: a release
archive carries its identity in `release-identity.json`, a checkout has git,
and a copied tree has neither — `live-vm-test.sh` rsyncs with
`--exclude '.git'`, so the test rigs run exactly that third case. An
unprovenanced build is named as one rather than dying: it is the same
judgement `sourcestamp.capture()` already makes ("such an install cannot
drift from a tree it does not have"), and the old code's unhandled
`rev-parse` failure took the rig's whole menubar stage down with a traceback.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path


def source(repo: Path) -> tuple[str, bool]:
    """`(sha, dirty)` for `repo` — `("", False)` when nothing knows."""
    release = repo / "host/release-identity.json"
    if release.exists():
        # A release is sealed: the sha is a fact of the archive, and there is
        # no tree here that could have moved off it.
        return json.loads(release.read_text())["sha"], False
    try:
        sha = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            text=True, stderr=subprocess.DEVNULL).strip()
        dirty = bool(subprocess.check_output(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
            text=True, stderr=subprocess.DEVNULL))
        return sha, dirty
    except (OSError, subprocess.SubprocessError):
        return "", False


def reserve(repo: Path, state: Path | None = None) -> dict:
    """The identity to stamp into the bundle being built out of `repo`.

    `state` is accepted and unused: the caller used to pass the directory the
    build counter lived in, and there is no counter to keep now.
    """
    sha, dirty = source(repo)
    version = json.loads(
        (repo / "plugins/jstack/.claude-plugin/plugin.json").read_text())["version"]
    day = date.today()
    stamp = sha[:8] if sha else "nosource"
    return {
        "sha": sha,
        "date": day.isoformat(),
        # CFBundleVersion has to sort, and this bundle never sees the App
        # Store — the day it was built is both orderable and true. jRemote is
        # the one product that still owes TestFlight a counter.
        "bundle": day.strftime("%Y%m%d"),
        "version": f"{version}+{day.isoformat()}.{stamp}" + (".dirty" if dirty else ""),
    }


if __name__ == "__main__":
    result = reserve(Path(sys.argv[1]))
    print(result["version"], result["bundle"], result["sha"])
