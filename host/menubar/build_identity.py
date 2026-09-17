"""Reserve a source-build identity without touching the running installation."""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def reserve(repo: Path, state: Path) -> dict:
    release = repo / "host/release-identity.json"
    if release.exists():
        sha = json.loads(release.read_text())["sha"]
        dirty = False
    else:
        sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        dirty = bool(subprocess.check_output(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"], text=True))
    version = json.loads((repo / "plugins/jstack/.claude-plugin/plugin.json").read_text())["version"]
    state.mkdir(parents=True, exist_ok=True)
    with (state / "build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        counter = state / "build.json"
        number = int(json.loads(counter.read_text())["build"]) + 1 if counter.exists() else 1
        fd, temporary = tempfile.mkstemp(dir=state, prefix="build-")
        with os.fdopen(fd, "w") as stream:
            json.dump({"build": number}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, counter)
    return {"build": number, "sha": sha, "version": f"{version}+dev.{number}.{sha[:8]}" +
            (".dirty" if dirty else "")}


if __name__ == "__main__":
    result = reserve(Path(sys.argv[1]), Path(sys.argv[2]))
    print(result["version"], result["build"], result["sha"])
