"""Publishing a release to GitHub. Nothing here is how a hub gets one.

A hub builds the commit it follows and signs the result itself —
`build_source`. This remains so a final release can still be cut with the
machinery the deployed fleet is running, which is how that fleet inherits the
change. Retiring it is its own phase.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from . import release_manifest as releases
#: One definition of each, in the module that follows a ref. Re-exported
#: because `publish` and both installers already import them from here.
from .build_source import CHANNEL, channel_ref as channel_name, repository  # noqa: F401

TAG_PREFIX = "stack-release-"


def publish(directory: Path, repo: str, public_key: str) -> None:
    """Upload only distributable artifacts; private evidence stays on the publisher."""
    from .update_macos import command
    repo = repository(repo)
    envelope = json.loads((directory / "manifest.json").read_text())
    manifest = releases.verify(envelope, public_key)
    tag = TAG_PREFIX + manifest["release"]
    assets = [directory / "manifest.json"]
    assets.extend(directory / item["file"] for item in manifest["components"].values())
    for item in manifest["components"].values():
        releases.check_artifact(directory / item["file"], item)
    existing = subprocess.run(["gh", "api", f"repos/{repo}/releases/tags/{tag}"],
                              capture_output=True, text=True, timeout=60)
    if existing.returncode and "HTTP 404" not in existing.stderr:
        raise releases.ReleaseError("could not inspect the publication: " + existing.stderr)
    state = json.loads(existing.stdout) if not existing.returncode else None
    if state is not None:
        # Never overwrite a released artifact. Retries verify it byte-for-byte;
        # a draft can resume only if its uploaded manifest still matches ours.
        with tempfile.TemporaryDirectory(prefix="jstack-published-") as temporary:
            downloaded = Path(temporary)
            if not state["draft"] or any(a["name"] == "manifest.json" for a in state["assets"]):
                command(["gh", "release", "download", tag, "--repo", repo,
                         "--pattern", "manifest.json", "--dir", str(downloaded)], timeout=120)
                if json.loads((downloaded / "manifest.json").read_text()) != envelope:
                    raise releases.ReleaseError("published release identity changed")
            if not state["draft"]:
                for item in manifest["components"].values():
                    command(["gh", "release", "download", tag, "--repo", repo,
                             "--pattern", item["file"], "--dir", str(downloaded)], timeout=900)
                    releases.check_artifact(downloaded / item["file"], item)
                return
    else:
        command(["gh", "release", "create", tag, "--repo", repo, "--draft",
                 "--target", manifest["sources"]["stack"],
                 "--title", "jStack " + manifest["release"],
                 "--notes", manifest.get("notes", "")], timeout=120)
    # Draft until the last upload succeeds. No consumer sees a partial release.
    command(["gh", "release", "upload", tag, "--repo", repo, "--clobber", *map(str, assets)], timeout=900)
    with tempfile.TemporaryDirectory(prefix="jstack-upload-check-") as temporary:
        downloaded = Path(temporary)
        for path in assets:
            command(["gh", "release", "download", tag, "--repo", repo,
                     "--pattern", path.name, "--dir", str(downloaded)], timeout=900)
            if releases.digest(downloaded / path.name) != releases.digest(path):
                raise releases.ReleaseError("uploaded artifact differs from qualified bytes")
    command(["gh", "release", "edit", tag, "--repo", repo, "--draft=false", "--latest=false"])
    # One release action feeds every install door. The Mac app installer reads
    # the newest mac-app-* tag; leaving it behind is how a promoted stack
    # release still hands a five-day-old client to a fresh install.
    client = manifest["components"]["client"]
    app_tag = "mac-app-1.0-" + str(client["version"])
    probe = subprocess.run(["gh", "api", f"repos/{repo}/releases/tags/{app_tag}"],
                           capture_output=True, text=True, timeout=60)
    if probe.returncode and "HTTP 404" in probe.stderr:
        with tempfile.TemporaryDirectory(prefix="jstack-app-manifest-") as temporary:
            app_manifest = Path(temporary) / "latest.json"
            app_manifest.write_text(json.dumps({
                "file": client["file"], "build": int(client["version"]),
                "version": client["version"], "sha256": client["sha256"]}) + "\n")
            command(["gh", "release", "create", app_tag, "--repo", repo,
                     "--target", manifest["sources"]["stack"],
                     "--title", f"jRemote for Mac 1.0 ({client['version']})",
                     "--notes", "Client from release " + manifest["release"],
                     str(directory / client["file"]), str(app_manifest)], timeout=900)
