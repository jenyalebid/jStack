"""A hub follows a git ref: it asks what HEAD is, and builds that commit itself.

Checking and building are two acts here. In the release channel this replaces
they were one — the refresh that answered "is there an update" had to stream
every artifact of a release before it would offer it, so opening the Info
window's 300s tick pulled a hundred megabytes. `check` costs one request and
forty bytes of answer; `build` is explicit and never runs from the tick.

What this hub builds, this hub signs, with an Ed25519 key minted here and kept
here. That is the honest claim for these bytes — a publisher's key would be a
borrowed one — so the built package carries the public half as its
`release-trust.json` and the hub pins that key the moment it first builds.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

import httpx

from . import release_manifest as releases


def repository(value: str) -> str:
    value = re.sub(r"^(https://github.com/|git@github.com:)", "", value).removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise releases.ReleaseError("release source must name a GitHub OWNER/REPO")
    return value


#: A branch name is a path, and a ref name is pasted into a URL and compared
#: against signed content — so what a hub will follow is bounded here rather
#: than wherever it happens to be used. No leading dash, no traversal, no
#: spaces. Any branch-shaped name passes, which is the VM and feature-branch
#: door: a hub is moved onto one by naming it.
CHANNEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,63}\Z")

SHA = re.compile(r"[a-f0-9]{40}\Z")

#: Nothing is checked more often than this, whoever asks.
FLOOR = 300


def channel_ref(config: dict) -> str:
    """Which ref this hub follows.

    Absent from the config on every hub installed before a ref could be
    chosen, and what those hubs hold came off main — so absent is `stable`,
    not "unset". `stable` is the name main answers to; anything else is a
    branch this hub was deliberately moved onto.
    """
    name = str(config.get("channel") or releases.STABLE_CHANNEL).strip()
    if not CHANNEL.fullmatch(name) or ".." in name:
        raise releases.ReleaseError("release channel must name a branch")
    return name


def branch(ref: str) -> str:
    """The git branch a ref name selects. `stable` is this repo's main."""
    return "main" if ref == releases.STABLE_CHANNEL else ref


def release_date() -> str:
    """Calendar date for the build, independent of the client build counter."""
    from datetime import date
    return date.today().isoformat()


def source_identity(date: str, stack: str, client: str, dependencies: dict) -> str:
    """The build's name: the day, the commit, and a hash of every source in it.

    Lives here rather than in `publish_release`, which is the only other
    caller, because a build now happens on the hub and a publication does not
    happen at all — one definition, so the two can never name the same sources
    differently. Fleet jobs key by release: a client-only fix must not look
    already installed.
    """
    sources = {"stack": stack, "client": client, "dependencies": dependencies}
    fingerprint = hashlib.sha256(releases.canonical(sources)).hexdigest()[:16]
    return f"{date}-{stack[:8]}-{fingerprint}"


def head(client: httpx.Client, repo: str, ref: str) -> str:
    """The newest commit on `ref` — the whole cost of a check.

    `application/vnd.github.sha` answers with the 40 bytes of the sha instead
    of the commit object, which is the cheapest question GitHub will answer
    about a branch. A proxy that ignores the media type hands back the object,
    so the sha is read out of that rather than failing on a working answer.
    """
    response = client.get(f"https://api.github.com/repos/{repo}/commits/{ref}",
                          headers={"Accept": "application/vnd.github.sha"})
    response.raise_for_status()
    value = response.text.strip()
    if not SHA.fullmatch(value):
        try:
            value = str(json.loads(response.text).get("sha", "")).strip()
        except ValueError:
            value = ""
        if not SHA.fullmatch(value):
            raise releases.ReleaseError("GitHub did not answer with a commit sha")
    return value


def held(config: dict) -> tuple[str, str]:
    """The build this hub already holds: its release ID and its source commit.

    Read through the same signature check every other reader of this feed
    uses, so a feed this hub cannot verify surfaces as a failed check rather
    than as a silent "up to date".
    """
    feed = config.get("feed_dir")
    if not feed:
        raise releases.ReleaseError("no feed directory is configured")
    latest = Path(feed) / "latest.json"
    if not latest.exists():
        return "", ""
    manifest = releases.verify(json.loads(latest.read_text()), config.get("public_key", ""),
                               promoted=not config.get("candidate_test", False))
    return manifest["release"], manifest["sources"]["stack"]


def skip(root: Path, config: dict) -> bool:
    """A hub that follows a ref of its own. Everyone else has a parent, a
    fleet owner, or a candidate pushed at it by hand."""
    return bool(not config.get("github_repo") or config.get("managed")
                or config.get("candidate_test") or (root.parent / "parent.json").exists())


def check(root: Path, config: dict, *, client=None, now=None) -> dict | None:
    """Ask what HEAD is. Download nothing, build nothing, install nothing."""
    from .update_supervisor import atomic_json
    if skip(root, config):
        return None
    now = time.time() if now is None else now
    status_file = root / "channel.json"
    status = json.loads(status_file.read_text()) if status_file.exists() else {}
    if now - status.get("checked", 0) < FLOOR:
        return status
    repo = repository(config["github_repo"])
    ref = channel_ref(config)
    owned = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=True, trust_env=False)
    try:
        sha = head(client, repo, branch(ref))
        release, installed = held(config)
        answer = {"checked": now, "ref": ref, "head": sha, "release": release,
                  "status": "current" if installed == sha else "behind"}
        atomic_json(status_file, answer)
        return answer
    except Exception as exc:
        atomic_json(status_file, {"checked": now, "ref": ref, "status": "failed",
                                  "detail": str(exc)})
        raise
    finally:
        if owned:
            client.close()


def phase(root: Path) -> dict:
    """What the build half is doing, for a window that has to say something.

    A `building` record outlives the process that wrote it if that process is
    killed, and a window stuck on "Building…" forever is a check that lies —
    so a record older than any real build reads as stalled instead.
    """
    try:
        value = json.loads((root / "build.json").read_text())
    except (OSError, ValueError):
        return {"state": "idle"}
    if not isinstance(value, dict) or not value.get("state"):
        return {"state": "idle"}
    if value["state"] == "building" and time.time() - value.get("started", 0) > 6 * 3600:
        return {**value, "state": "stalled"}
    return value


def build_key(root: Path) -> tuple[bytes, str]:
    """This hub's own signing key: minted once, never leaving this machine."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    path = root / "build-key"
    if not path.exists():
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(base64.b64encode(Ed25519PrivateKey.generate().private_bytes_raw()).decode())
    if path.stat().st_mode & 0o177:
        raise releases.ReleaseError("the local build key is reachable beyond this account")
    private = base64.b64decode(path.read_text().strip(), validate=True)
    public = Ed25519PrivateKey.from_private_bytes(private).public_key().public_bytes_raw()
    return private, base64.b64encode(public).decode()


def fetch(source: Path, repo: str, ref: str) -> str:
    """Bring `ref` into this hub's own checkout; answer the commit it names."""
    from .update_macos import command
    if not (source / ".git").is_dir():
        source.mkdir(parents=True, exist_ok=True)
        command(["git", "init", "--quiet", str(source)], timeout=120)
    origin = f"https://github.com/{repo}.git"
    existing = subprocess.run(["git", "-C", str(source), "remote", "get-url", "origin"],
                              capture_output=True, text=True, timeout=60)
    command(["git", "-C", str(source), "remote",
             "set-url" if not existing.returncode else "add", "origin", origin], timeout=60)
    # No --depth: `rev-list --count` below is the only ordering a hub has, and
    # a shallow clone counts the commits it was handed rather than the ones
    # that exist.
    command(["git", "-C", str(source), "fetch", "--force", "origin", ref], timeout=3600)
    return command(["git", "-C", str(source), "rev-parse", "FETCH_HEAD"]).strip()


def component(path: Path, version: str) -> dict:
    return {"file": path.name, "version": version, "bytes": path.stat().st_size,
            "sha256": releases.digest(path)}


def inherited(config: dict) -> dict:
    """The client artifact this build carries forward, and where it lives.

    jRemote's Mac app is closed source and is not built here. What a hub can
    honestly do is keep serving the exact client bytes it already holds and
    already verified, so a locally built Hub names that same artifact and the
    same client commit. A hub with nothing in its feed has no client to carry
    and cannot produce a complete release.
    """
    feed = Path(config["feed_dir"])
    if not (feed / "latest.json").exists():
        raise releases.ReleaseError(
            "this hub holds no release to carry a client artifact forward from")
    manifest = releases.verify(json.loads((feed / "latest.json").read_text()),
                               config.get("public_key", ""),
                               promoted=not config.get("candidate_test", False))
    item = manifest["components"]["client"]
    releases.check_artifact(feed / manifest["release"] / item["file"], item)
    return manifest


def build(root: Path, config: dict, *, ref: str | None = None, now=None) -> dict:
    """Build the Hub from the newest commit on this hub's ref, and offer it.

    Explicit by construction: nothing in the supervisor's tick reaches here.
    The result lands in the feed in the shape a downloaded release landed in,
    so stage/install/verify take over from here unchanged.
    """
    from .update_supervisor import atomic_json
    if config.get("managed") or (root.parent / "parent.json").exists():
        raise releases.ReleaseError("a managed machine takes its builds from its parent")
    ref = channel_ref({"channel": ref} if ref else config)
    progress = root / "build.json"
    atomic_json(progress, {"state": "building", "ref": ref,
                           "started": time.time() if now is None else now})
    try:
        result = _build(root, config, ref)
        atomic_json(progress, {"state": "built", "ref": ref, "release": result["release"],
                               "finished": time.time()})
        return result
    except Exception as exc:
        atomic_json(progress, {"state": "failed", "ref": ref, "detail": str(exc),
                               "finished": time.time()})
        raise


def _build(root: Path, config: dict, ref: str) -> dict:
    from . import build_hub
    from .update_macos import command
    from .update_supervisor import atomic_json
    repo = repository(config["github_repo"])
    feed = Path(config["feed_dir"])
    private, public = build_key(root)
    source = Path(config.get("source_dir") or root / "source")
    sha = fetch(source, repo, branch(ref))
    previous = inherited(config)
    client_sha = previous["sources"]["client"]
    dependencies = previous.get("client_packages", {})
    date = release_date()
    release_id = source_identity(date, sha, client_sha, dependencies)
    envelope_path = feed / release_id / "manifest.json"
    if envelope_path.exists():
        # This hub already built these exact sources today. Re-offer those
        # bytes rather than building different ones under the same name.
        envelope = json.loads(envelope_path.read_text())
        manifest = releases.verify(envelope, public)
        for item in manifest["components"].values():
            releases.check_artifact(feed / release_id / item["file"], item)
        return _offer(root, config, feed, envelope, public)
    work = Path(tempfile.mkdtemp(prefix="build-", suffix=".noindex", dir=root))
    stack, output = work / "stack", work / release_id
    output.mkdir(parents=True)
    try:
        command(["git", "-C", str(source), "worktree", "add", "--detach", str(stack), sha],
                timeout=600)
        version = json.loads(
            (stack / "plugins/jstack/.claude-plugin/plugin.json").read_text())["version"]
        # The only ordering a hub has. The identity is a date and two hashes,
        # and neither of those orders; this is the number of commits behind
        # this one on its own line, compared only against a build from the
        # same line. Never displayed.
        sequence = int(command(["git", "-C", str(source), "rev-list", "--count", sha]).strip())
        # Two counts are only comparable along one line, so this compares a
        # build against what this hub holds only when both came off the ref
        # being followed. Switching refs is how a hub is rolled back, and a
        # switch is not a downgrade — its old count means nothing on the new
        # line. What this does catch is a ref force-pushed backwards under a
        # hub that is following it.
        if (previous.get("channel", {}).get("name") or releases.STABLE_CHANNEL) == ref and (
                isinstance(previous.get("sequence"), int) and sequence < previous["sequence"]):
            raise releases.ReleaseError(f"{ref} is behind the build this hub already holds")
        identity = {"release": release_id, "sha": sha, "version": version, "date": date,
                    "github_repo": repo, "sequence": sequence, "channel": ref}
        app = build_hub.build(stack, output, version, config.get("signing"),
                              release_id=release_id, github_repo=repo, date=date,
                              trust_key=public)
        menu = output / "menubar-notarized.zip"
        if config.get("signing"):
            build_hub.notarize(app, output, config["signing"])
            shutil.move(output / "hub-notarized.zip", menu)
        else:
            # Nothing to notarize: without `signing` the bundle is ad-hoc
            # signed. The archive has the same shape either way, and it is
            # `stage()`'s codesign check that decides whether it installs.
            command(["/usr/bin/ditto", "-c", "-k", "--keepParent", str(app), str(menu)],
                    timeout=600)
        shutil.rmtree(app, ignore_errors=True)
        # After the Hub is built, because `build_hub` archives HEAD and
        # refuses a dirty `host/`: these two files exist only in the tarball
        # the installing machine unpacks, and `stage()` reads the identity
        # back out of exactly this tree.
        trust = stack / "host/jstack_host/release-trust.json"
        trust.write_text(json.dumps({"algorithm": "Ed25519", "public_key": public}, indent=2) + "\n")
        from .sourcestamp import fingerprint
        (stack / "host/release-identity.json").write_text(json.dumps({
            **identity, "package_sha256": fingerprint(stack / "host/jstack_host")}))
        archive = output / "stack.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            for path in sorted(stack.iterdir()):
                if path.name != ".git":
                    bundle.add(path, arcname=path.name)
        item = previous["components"]["client"]
        shutil.copy2(feed / previous["release"] / item["file"], output / item["file"])
        manifest = {
            "schema": releases.SCHEMA, "release": release_id,
            "notes": f"built on this hub from {repo}@{ref} ({sha[:8]})",
            "sequence": sequence,
            "channel": {"github_repo": repo, "name": ref},
            # Inside the signed bytes, because it is what excuses this
            # manifest from the acceptance receipts a publication carries.
            "origin": {"kind": releases.SOURCE_BUILD, "machine": config.get("machine", "")},
            "sources": {"stack": sha, "client": client_sha},
            "client_packages": dependencies,
            "components": {"stack": component(archive, version),
                           "menubar": component(menu, build_hub.bundle_version(identity, version)),
                           "client": item},
            # The client artifact is the one this hub already holds, so what it
            # will run on is the previous manifest's answer, not a new claim.
            "compatibility": previous["compatibility"],
            "mobile": {"source": client_sha, "status": "not_distributed"},
            "receipts": {}}
        envelope = releases.sign(manifest, private)
        feed.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".build-", dir=feed))
        for entry in manifest["components"].values():
            shutil.copy2(output / entry["file"], staging / entry["file"])
            releases.check_artifact(staging / entry["file"], entry)
        atomic_json(staging / "manifest.json", envelope)
        # A directory here with no manifest is the wreckage of a build that
        # died between the rename and the write; it holds nothing anybody can
        # verify, so it is replaced rather than left to block this one.
        shutil.rmtree(feed / release_id, ignore_errors=True)
        os.rename(staging, feed / release_id)
        return _offer(root, config, feed, envelope, public)
    finally:
        subprocess.run(["git", "-C", str(source), "worktree", "remove", "--force", str(stack)],
                       capture_output=True, timeout=300)
        shutil.rmtree(work, ignore_errors=True)


def _offer(root: Path, config: dict, feed: Path, envelope: dict, public: str) -> dict:
    """Pin this hub's trust to its own key, then offer what it built.

    One pinned key at a time, rotated at the one moment a hub stops taking
    someone else's bytes and starts producing its own. The order matters: a
    `latest.json` this hub's configured key cannot verify is a feed that reads
    as broken to every route that serves it. In place as well as on disk,
    because the caller is holding this dict and the supervisor shares it.
    """
    from .update_supervisor import atomic_json
    config_path = root / "config.json"
    if config_path.exists():
        saved = json.loads(config_path.read_text())
        if saved.get("public_key") != public:
            atomic_json(config_path, {**saved, "public_key": public})
    config["public_key"] = public
    atomic_json(feed / "latest.json", envelope)
    return {"release": envelope["manifest"]["release"], "public_key": public}
