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
import re
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
        pass
    # A copied tree: `live-vm-test.sh` rsyncs with `--exclude '.git'` and
    # leaves this behind instead, because the end doing the copying is the
    # only end that knows which commit it took.
    copied = repo / "host/copied-from.json"
    if copied.exists():
        data = json.loads(copied.read_text())
        if data.get("sha"):
            return data["sha"], bool(data.get("dirty"))
    return "", False


#: `build_hub.RELEASE_VERSION`, restated: this script runs before any package
#: is importable, so the formula is defined twice and a test holds them equal.
RELEASE_VERSION = re.compile(r"(\d{2})\.(\d{1,2})\.(\d+)\Z")


def bundle_number(day: date, version: str, sha: str) -> str:
    """`YYYYMMDD.N.<int(sha8, 16)>` — `build_hub.bundle_version`'s formula.

    The day alone let two builds of different commits on one day share a
    CFBundleVersion. N is the month's release count out of a `YY.M.N`
    version, 0 for any other shape; the commit rides as a number because
    CFBundleVersion is digits and dots.
    """
    match = RELEASE_VERSION.fullmatch(str(version))
    try:
        commit = int(sha[:8], 16) if sha else 0
    except ValueError:
        commit = 0
    return f"{day.strftime('%Y%m%d')}.{int(match[3]) if match else 0}.{commit}"


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
        # Store — the day it was built leads, so it is both orderable and
        # true. jRemote is the one product that still owes TestFlight a counter.
        "bundle": bundle_number(day, version, sha),
        "version": f"{version}+{day.isoformat()}.{stamp}" + (".dirty" if dirty else ""),
    }


if __name__ == "__main__":
    result = reserve(Path(sys.argv[1]))
    print(result["version"], result["bundle"], result["sha"])
