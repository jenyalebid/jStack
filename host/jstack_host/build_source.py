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


def source_origin(machine: str) -> dict:
    """The marker saying these bytes were built by the machine that will run
    them, in the one spelling both readers of it know.

    It goes in two places and has to mean the same thing in both: inside the
    signed manifest, where it excuses a release from the acceptance receipts
    a publication carries, and inside the built bundle, where it tells the
    sealed installer to ask the pinned key rather than a signing team.
    """
    return {"kind": releases.SOURCE_BUILD, "machine": machine}


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


def assemble(*, release_id: str, notes: str, sequence: int, repo: str, ref: str,
             machine: str, sha: str, client_sha: str, dependencies: dict,
             components: dict, compatibility: dict) -> dict:
    """The one shape of a manifest this machine signs.

    A hub rebuilding itself and an installer building the release it is about
    to install produce the same document from different inputs. Two spellings
    of it would be two answers to what a locally built release *is*, and only
    one of them is inside the signature.
    """
    return {
        "schema": releases.SCHEMA, "release": release_id, "notes": notes,
        "sequence": sequence,
        "channel": {"github_repo": repo, "name": ref},
        # Inside the signed bytes, because it is what excuses this manifest
        # from the acceptance receipts a publication carries.
        "origin": source_origin(machine),
        "sources": {"stack": sha, "client": client_sha},
        "client_packages": dependencies,
        "components": components,
        "compatibility": compatibility,
        "mobile": {"source": client_sha, "status": "not_distributed"},
        "receipts": {}}


def land(feed: Path, output: Path, envelope: dict) -> Path:
    """Put a signed build's artifacts in the feed under the release's name."""
    from .update_supervisor import atomic_json
    manifest = envelope["manifest"]
    release_id = manifest["release"]
    feed.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".build-", dir=feed))
    for entry in manifest["components"].values():
        shutil.copy2(output / entry["file"], staging / entry["file"])
        releases.check_artifact(staging / entry["file"], entry)
    atomic_json(staging / "manifest.json", envelope)
    # A directory here with no manifest is the wreckage of a build that died
    # between the rename and the write; it holds nothing anybody can verify,
    # so it is replaced rather than left to block this one.
    shutil.rmtree(feed / release_id, ignore_errors=True)
    os.rename(staging, feed / release_id)
    return feed / release_id


#: The one way past the adopted-machine refusal below, and it is typed, never
#: defaulted: `JSTACK_BUILD_DESPITE_LEAVES=1 jstack-host updates build`. The
#: machine that publishes for a fleet has to build eventually, and re-keying
#: its leaves by hand afterwards is a decision somebody makes on purpose.
DESPITE_LEAVES = "JSTACK_BUILD_DESPITE_LEAVES"


def adopted() -> list[str]:
    """The machines that take their updates from this one.

    Exactly the rows `updates/queue` will send a job to: a forgotten row takes
    nothing, and a row with no bound credential has no updater to strand.
    """
    try:
        from .store import get_store
        rows = get_store().list_hosts()
    except Exception as exc:  # a store that cannot be read is not "no leaves"
        raise releases.ReleaseError(
            "could not read this hub's adopted machines: " + str(exc)) from exc
    return [row["name"] or row["key"] for row in rows
            if not row["deleted"] and row["device_id"]]


def build_refusal(root: Path, config: dict) -> str:
    """Why this machine must not build, or `""` if it may.

    One rule with two readers: `build()` raises it, and the window asks it
    before drawing a Rebuild button — a button that answers with an error is
    the same lie as a check that reports state it cannot observe.
    """
    if config.get("managed") or (root.parent / "parent.json").exists():
        return "a managed machine takes its builds from its parent"
    if not config.get("github_repo"):
        return "this host has no source repository to build from"
    # A build mints this hub's own key and rotates `public_key` to it
    # (`_offer`). A leaf verifies its parent's jobs against the key it pinned
    # when it installed, so the first build under it makes every future job
    # unverifiable there — and the key that would fix it only arrives inside an
    # update the leaf now refuses. #144 holds the fix; this refusal holds the
    # fleet.
    names = adopted()
    if names and os.environ.get(DESPITE_LEAVES) != "1":
        return ("this hub has adopted machines (" + ", ".join(names) + ") and a build "
                "would rotate the release key they trust, leaving them unable to verify "
                "any update from this hub — see #144. Forget them, or build with "
                f"{DESPITE_LEAVES}=1 and re-adopt them afterwards.")
    return ""


def build(root: Path, config: dict, *, ref: str | None = None, now=None) -> dict:
    """Build the Hub from the newest commit on this hub's ref, and offer it.

    Explicit by construction: nothing in the supervisor's tick reaches here.
    The result lands in the feed in the shape a downloaded release landed in,
    so stage/install/verify take over from here unchanged.
    """
    from .update_supervisor import atomic_json
    refusal = build_refusal(root, config)
    if refusal:
        raise releases.ReleaseError(refusal)
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
        built_by = source_origin(config.get("machine", ""))
        identity = {"release": release_id, "sha": sha, "version": version, "date": date,
                    "github_repo": repo, "sequence": sequence, "channel": ref,
                    "origin": built_by}
        app = build_hub.build(stack, output, version, config.get("signing"),
                              release_id=release_id, github_repo=repo, date=date,
                              trust_key=public, channel=ref, origin=built_by)
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
        manifest = assemble(
            release_id=release_id, notes=f"built on this hub from {repo}@{ref} ({sha[:8]})",
            sequence=sequence, repo=repo, ref=ref, machine=config.get("machine", ""),
            sha=sha, client_sha=client_sha, dependencies=dependencies,
            components={"stack": component(archive, version),
                        "menubar": component(menu, build_hub.bundle_version(identity, version)),
                        "client": item},
            # The client artifact is the one this hub already holds, so what it
            # will run on is the previous manifest's answer, not a new claim.
            compatibility=previous["compatibility"])
        envelope = releases.sign(manifest, private)
        land(feed, output, envelope)
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


def compatibility_of(hub: Path, client: Path) -> dict:
    """What this build runs on, read off the two bundles it is made of.

    A publication states this by hand. An installer has no hand to state it
    with, and a wrong claim here is a release offered to a Mac that cannot run
    it — so it is read back from the bundles, and the release runs on neither
    of them unless both do.
    """
    import platform
    from .update_macos import bundle_info
    minimums = []
    for bundle in (hub, client):
        value = str(bundle_info(bundle).get("LSMinimumSystemVersion", "")).strip()
        if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", value):
            raise releases.ReleaseError(f"{bundle.name} states no minimum macOS version")
        minimums.append(value)
    return {"protocol": 1, "rollback": True, "platform": "macos",
            "architecture": platform.machine(),
            "minimum_os": max(minimums, key=lambda v: tuple(int(p) for p in v.split(".")))}


def client_component(client: Path, output: Path) -> tuple[dict, str]:
    """The installed jRemote, archived as this build's client artifact.

    jRemote is not built here and its release is downloaded once, by the
    installer that verified it. The honest bytes to offer are therefore the
    ones already on the disk. `JStackSourceCommit` is the commit they were
    built from; without it the manifest cannot name a client revision and no
    later build can carry one forward.
    """
    from .update_macos import bundle_info, command
    info = bundle_info(client)
    commit = str(info.get("JStackSourceCommit", "")).strip()
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise releases.ReleaseError(
            f"{client.name} does not record the commit it was built from")
    archive = output / (client.stem + ".zip")
    command(["/usr/bin/ditto", "-c", "-k", "--keepParent", str(client), str(archive)],
            timeout=900)
    return component(archive, str(info["CFBundleVersion"])), commit


def installable(app: Path) -> None:
    """Refuse a bundle the sealed installer would reject, before it is moved.

    The same question `install_signed.identity` asks, asked here because there
    it is asked with the bundle already in /Applications — a failure nine
    frames down, after the whole build. What it can still catch is a build
    whose seal did not take: an ad-hoc signature is enough for a bundle this
    machine built, an absent or broken one is not, on any path.
    """
    from . import app_services
    try:
        app_services.verify(app, "live.jstack.hub")
    except Exception as exc:
        raise releases.ReleaseError(
            "this Mac built a Hub its own sealed installer will not adopt — the "
            f"bundle's signature does not hold over what was built: {exc}") from exc


def bootstrap(checkout: Path, output: Path, key_dir: Path, *, repo: str, ref: str,
              client: Path | None = None, signing: dict | None = None,
              machine: str = "") -> dict:
    """Build the release the installer running this is about to install.

    `_build` is the same act on a hub that already exists: it fetches the ref
    into its own source dir and carries the client artifact and the
    compatibility block forward from the release it holds. An installing Mac
    holds neither. It has a checkout already on the ref and the client it just
    installed, so those are what it names; from the manifest down it is the
    same code, signed with the same machine key.

    Without a client there is no complete manifest to sign — `COMPONENTS` wants
    all three — so the Hub is still built and installed and the feed stays
    empty, which is what `--no-app` asks for.
    """
    from . import build_hub
    from .update_macos import command
    from .update_supervisor import atomic_json
    repo = repository(repo)
    ref = channel_ref({"channel": ref})
    private, public = build_key(key_dir)
    sha = command(["git", "-C", str(checkout), "rev-parse", "HEAD"]).strip()
    if not SHA.fullmatch(sha):
        raise releases.ReleaseError("the checkout is not on a commit this build can name")
    sequence = int(command(["git", "-C", str(checkout), "rev-list", "--count", sha]).strip())
    date = release_date()
    output.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="install-build-", suffix=".noindex"))
    stack = work / "stack"
    try:
        command(["git", "-C", str(checkout), "worktree", "add", "--detach", str(stack), sha],
                timeout=600)
        # Out of the commit, not the working tree: the version this release
        # declares has to be the one inside the bytes it ships.
        version = json.loads(
            (stack / "plugins/jstack/.claude-plugin/plugin.json").read_text())["version"]
        item, client_sha = client_component(client, output) if client else (None, "")
        release_id = source_identity(date, sha, client_sha, {})
        built_by = source_origin(machine)
        identity = {"release": release_id, "sha": sha, "version": version, "date": date,
                    "github_repo": repo, "sequence": sequence, "channel": ref,
                    "origin": built_by}
        app = build_hub.build(stack, output, version, signing, release_id=release_id,
                              github_repo=repo, date=date, trust_key=public,
                              channel=ref, origin=built_by)
        menu = output / "menubar-notarized.zip"
        if signing:
            build_hub.notarize(app, output, signing)
            shutil.move(output / "hub-notarized.zip", menu)
        else:
            command(["/usr/bin/ditto", "-c", "-k", "--keepParent", str(app), str(menu)],
                    timeout=900)
        installable(app)
        compatibility = compatibility_of(app, client) if item else None
        shutil.rmtree(app, ignore_errors=True)
        answer = {"release": release_id, "sha": sha, "ref": ref, "version": version,
                  "menubar": str(menu), "public_key": public, "offer": bool(item)}
        if not item:
            return answer
        # After the Hub is built, because `build_hub` archives HEAD and refuses
        # a dirty `host/`: these two files exist only in the tarball the
        # installing machine unpacks, and `stage()` reads the identity back out
        # of exactly this tree.
        (stack / "host/jstack_host/release-trust.json").write_text(
            json.dumps({"algorithm": "Ed25519", "public_key": public}, indent=2) + "\n")
        from .sourcestamp import fingerprint
        (stack / "host/release-identity.json").write_text(json.dumps({
            **identity, "package_sha256": fingerprint(stack / "host/jstack_host")}))
        archive = output / "stack.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            for path in sorted(stack.iterdir()):
                if path.name != ".git":
                    bundle.add(path, arcname=path.name)
        manifest = assemble(
            release_id=release_id,
            notes=f"built by the installer from {repo}@{ref} ({sha[:8]})",
            sequence=sequence, repo=repo, ref=ref, machine=machine, sha=sha,
            client_sha=client_sha, dependencies={},
            components={"stack": component(archive, version),
                        "menubar": component(menu, build_hub.bundle_version(identity, version)),
                        "client": item},
            compatibility=compatibility)
        atomic_json(output / "manifest.json", releases.sign(manifest, private))
        return answer
    finally:
        subprocess.run(["git", "-C", str(checkout), "worktree", "remove", "--force", str(stack)],
                       capture_output=True, timeout=300)
        shutil.rmtree(work, ignore_errors=True)


def seed(root: Path, output: Path) -> dict:
    """Make what the installer built this hub's first offer.

    `inherited()` refuses on an empty feed, so a hub that never lands its own
    first release can never build a second one — and a hub with no feed serves
    no leaf. The install that produced these bytes lands them itself, through
    the writer `updates build` uses.
    """
    from .update_supervisor import atomic_json
    config_path = root / "config.json"
    config = json.loads(config_path.read_text())
    _, public = build_key(root)
    if config.get("public_key") != public:
        raise releases.ReleaseError(
            "this hub does not trust the key its own installer signed with")
    envelope = json.loads((output / "manifest.json").read_text())
    manifest = releases.verify(envelope, public,
                               promoted=not config.get("candidate_test", False))
    feed = Path(config["feed_dir"])
    land(feed, output, envelope)
    atomic_json(feed / "latest.json", envelope)
    return {"release": manifest["release"], "feed": str(feed)}


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Build a release on this machine and offer it.")
    actions = parser.add_subparsers(dest="action", required=True)
    build_args = actions.add_parser("bootstrap")
    build_args.add_argument("--checkout", type=Path, required=True)
    build_args.add_argument("--output", type=Path, required=True)
    build_args.add_argument("--key-dir", type=Path, required=True)
    build_args.add_argument("--client", type=Path)
    build_args.add_argument("--repo", required=True)
    build_args.add_argument("--ref", required=True)
    build_args.add_argument("--machine", default="")
    build_args.add_argument("--signing", type=Path,
                            help="release configuration carrying a signing block")
    seed_args = actions.add_parser("seed")
    seed_args.add_argument("--root", type=Path, required=True)
    seed_args.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "bootstrap":
        signing = json.loads(args.signing.read_text()).get("signing") if args.signing else None
        result = bootstrap(args.checkout, args.output, args.key_dir, repo=args.repo, ref=args.ref,
                           client=args.client, signing=signing, machine=args.machine)
    else:
        result = seed(args.root, args.output)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
