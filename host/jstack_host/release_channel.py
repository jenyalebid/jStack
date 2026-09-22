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


#: A branch name is a path, and a channel name is pasted into a URL and
#: compared against signed content — so what a hub will follow is bounded
#: here rather than wherever it happens to be used. No leading dash, no
#: traversal, no spaces.
CHANNEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,63}\Z")


def channel_name(config: dict) -> str:
    """Which release line this hub follows.

    Absent from the config on every hub installed before this existed, and the
    releases those hubs already hold came off main — so absent is `stable`,
    not "unset". A hub only leaves the stable line when somebody puts a branch
    name in its config, which is the whole of the switch: the selector below
    then matches that name against the channel inside each signed manifest.
    """
    name = str(config.get("channel") or releases.STABLE_CHANNEL).strip()
    if not CHANNEL.fullmatch(name) or ".." in name:
        raise releases.ReleaseError("release channel must name a branch")
    return name


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
        wanted = channel_name(config)
        stable = wanted == releases.STABLE_CHANNEL
        # A side-branch release is published as a GitHub prerelease, which is
        # what keeps it invisible to every hub that did not ask for it: the
        # stable line's filter has always dropped prereleases and still does.
        # A hub following a branch lifts that filter for itself alone.
        offers = [r for r in response.json() if not r["draft"]
                  and r["tag_name"].startswith(TAG_PREFIX)
                  and (stable is not True or not r["prerelease"])]
        # The channel a release belongs to is inside its signed manifest, not
        # in its tag — a tag is attacker-writable metadata and the name of the
        # line a hub follows is a trust decision. So the offers are walked
        # newest first and each manifest is verified before its channel is
        # read; the first that verifies AND names this hub's channel wins.
        release = manifest = envelope = None
        for offer in offers:
            tag = releases.identifier(offer["tag_name"])
            base = f"https://github.com/{repo}/releases/download/{tag}"
            found = client.get(base + "/manifest.json")
            if found.status_code != 200:
                continue          # a release without a stack manifest is not an offer
            candidate = releases.verify(found.json(), config["public_key"])
            if tag != TAG_PREFIX + candidate["release"]:
                raise releases.ReleaseError("release tag does not match signed manifest")
            # Manifests published before channels existed carry no name, and
            # they all came off main — so absent reads as stable rather than
            # as "no channel", which would strand every hub on the old feed.
            if (candidate.get("channel", {}).get("name") or releases.STABLE_CHANNEL) != wanted:
                continue
            release, manifest, envelope = offer, candidate, found.json()
            break
        if release is None:
            atomic_json(status_file, {"checked": now, "status": "not_published",
                                      "channel": wanted})
            return
        tag = release["tag_name"]
        base = f"https://github.com/{repo}/releases/download/{tag}"
        latest = feed / "latest.json"
        if latest.exists():
            current = releases.verify(json.loads(latest.read_text()), config["public_key"])
            # Two counts are only comparable when they count along the same
            # line, so this compares sequences only when the release in hand
            # came off the channel being followed. A hub deliberately moved to
            # a branch is not downgrading — it is changing which history it
            # measures against, and its old count means nothing on the new one.
            held = current.get("channel", {}).get("name") or releases.STABLE_CHANNEL
            if held == wanted:
                if (isinstance(current.get("sequence"), int)
                        and isinstance(manifest.get("sequence"), int)
                        and manifest["sequence"] < current["sequence"]):
                    raise releases.ReleaseError(
                        "public channel would downgrade the current release")
                # The counter-based guard this replaced compared `build`, and
                # it stopped firing the day releases stopped carrying one —
                # silently, because a comparison between two Nones is simply
                # skipped. Keeping it costs nothing and covers a hub that
                # still holds a pre-sequence release from the counter era.
                if (isinstance(current.get("build"), int)
                        and isinstance(manifest.get("build"), int)
                        and manifest["build"] < current["build"]):
                    raise releases.ReleaseError(
                        "public channel would downgrade the current release")
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
