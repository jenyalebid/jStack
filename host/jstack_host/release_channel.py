"""The public GitHub release channel, cached and verified by each hub.

Only immutable published stack releases are offers. Drafts, prereleases and
client-only releases never enter this channel. Leaves follow their parent.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

import httpx

from . import release_manifest as releases

TAG_PREFIX = "stack-release-"


def repository(value: str) -> str:
    value = re.sub(r"^(https://github.com/|git@github.com:)", "", value).removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise releases.ReleaseError("release source must name a GitHub OWNER/REPO")
    return value


def refresh(root: Path, config: dict, *, client=None, now=None) -> None:
    """Cache a complete signed release before advancing the local offer."""
    from .update_supervisor import atomic_json
    if (not config.get("github_repo") or config.get("managed") or config.get("candidate_test")
            or (root.parent / "parent.json").exists()):
        return
    now = time.time() if now is None else now
    status_file = root / "channel.json"
    status = json.loads(status_file.read_text()) if status_file.exists() else {}
    if now - status.get("checked", 0) < 300:
        return
    repo = repository(config["github_repo"])
    feed = Path(config["feed_dir"])
    owned = client is None
    client = client or httpx.Client(timeout=60, follow_redirects=True, trust_env=False)
    try:
        response = client.get(f"https://api.github.com/repos/{repo}/releases?per_page=100")
        response.raise_for_status()
        release = next((r for r in response.json() if not r["draft"] and not r["prerelease"]
                        and r["tag_name"].startswith(TAG_PREFIX)), None)
        if release is None:
            atomic_json(status_file, {"checked": now, "status": "not_published"})
            return
        tag = release["tag_name"]
        releases.identifier(tag)
        base = f"https://github.com/{repo}/releases/download/{tag}"
        response = client.get(base + "/manifest.json")
        response.raise_for_status()
        envelope = response.json()
        manifest = releases.verify(envelope, config["public_key"])
        if tag != TAG_PREFIX + manifest["release"]:
            raise releases.ReleaseError("release tag does not match signed manifest")
        destination = feed / manifest["release"]
        feed.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if json.loads((destination / "manifest.json").read_text()) != envelope:
                raise releases.ReleaseError("published release identity changed")
            for item in manifest["components"].values():
                releases.check_artifact(destination / item["file"], item)
        else:
            staging = Path(tempfile.mkdtemp(prefix=".download-", dir=feed))
            for item in manifest["components"].values():
                path = staging / item["file"]
                received = 0
                with client.stream("GET", base + "/" + item["file"]) as response:
                    response.raise_for_status()
                    with path.open("wb") as out:
                        for chunk in response.iter_bytes():
                            received += len(chunk)
                            if received > item["bytes"]:
                                raise releases.ReleaseError("artifact exceeds its signed size")
                            out.write(chunk)
                releases.check_artifact(path, item)
            atomic_json(staging / "manifest.json", envelope)
            os.rename(staging, destination)
        atomic_json(feed / "latest.json", envelope)
        atomic_json(status_file, {"checked": now, "status": "checked", "release": manifest["release"]})
    except Exception as exc:
        atomic_json(status_file, {"checked": now, "status": "failed", "detail": str(exc)})
        raise
    finally:
        if owned:
            client.close()


def publish(directory: Path, repo: str) -> None:
    """Upload only distributable artifacts; private evidence stays on the publisher."""
    from .update_macos import command
    repo = repository(repo)
    envelope = json.loads((directory / "manifest.json").read_text())
    manifest = envelope["manifest"]
    tag = TAG_PREFIX + manifest["release"]
    assets = [directory / "manifest.json"]
    assets.extend(directory / item["file"] for item in manifest["components"].values())
    # Draft until the last upload succeeds. No consumer sees a partial release.
    command(["gh", "release", "create", tag, "--repo", repo, "--draft",
             "--title", "jStack " + manifest["components"]["stack"]["version"],
             "--notes", manifest.get("notes", ""), *map(str, assets)], timeout=900)
    command(["gh", "release", "edit", tag, "--repo", repo, "--draft=false", "--latest=false"])
