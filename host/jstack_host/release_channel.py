"""The public GitHub release channel, cached and verified by each hub.

Only immutable published stack releases are offers. Drafts, prereleases and
client-only releases never enter this channel. Leaves follow their parent.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
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
        latest = feed / "latest.json"
        if latest.exists():
            current = releases.verify(json.loads(latest.read_text()), config["public_key"])
            if (isinstance(current.get("build"), int) and isinstance(manifest.get("build"), int)
                    and manifest["build"] < current["build"]):
                raise releases.ReleaseError("public channel would downgrade the current release")
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


def publish(directory: Path, repo: str, public_key: str) -> None:
    """Upload only distributable artifacts; private evidence stays on the publisher."""
    from .update_macos import command
    repo = repository(repo)
    envelope = json.loads((directory / "manifest.json").read_text())
    manifest = releases.verify(envelope, public_key)
    if manifest["schema"] == releases.NATIVE_SCHEMA:
        raise releases.ReleaseError("native owner publication requires qualified Services self-update")
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
                 "--title", "jStack " + manifest["components"]["stack"]["version"],
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
