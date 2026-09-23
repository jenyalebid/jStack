"""Build exact source snapshots, then promote only an evidenced candidate."""
from __future__ import annotations

import argparse
import base64
import copy
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from . import acceptance, release_manifest as releases
from .update_macos import command
from .update_supervisor import atomic_json


def snapshot(repo: Path, target: Path) -> str:
    sha = command(["git", "-C", str(repo), "rev-parse", "HEAD"]).strip()
    # A detached worktree has the exact commit AND the git metadata existing
    # product gates need. Other sessions' uncommitted work is not included and
    # does not stop a release of the named committed sources.
    command(["git", "-C", str(repo), "worktree", "add", "--detach", str(target), sha])
    return sha


def component(path: Path, version: str) -> dict:
    return {"file": path.name, "version": version, "bytes": path.stat().st_size,
            "sha256": releases.digest(path)}


def allocate_client_build(candidates: Path, minimum: int) -> int:
    """Reserve jRemote's next build number; failed builds never recycle it.

    This is the *client's* counter and only the client's. jRemote ships through
    TestFlight and the App Store, which reject a build that does not outrank the
    last one, so the counter is Apple's requirement rather than ours. The Hub
    stopped sharing it — see release_date().
    """
    candidates.mkdir(parents=True, exist_ok=True)
    with (candidates / "build-number.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = candidates / "build-number.json"
        previous = json.loads(state.read_text())["build"] if state.exists() else 0
        number = max(previous, minimum) + 1
        atomic_json(state, {"build": number})
        return number


def release_date() -> str:
    """Calendar date for the release, independent of the client build counter."""
    from datetime import date
    return date.today().isoformat()


def source_identity(date: str, stack: str, client: str, dependencies: dict) -> str:
    # Fleet jobs key by release: a client-only fix must not look already installed.
    sources = {"stack": stack, "client": client, "dependencies": dependencies}
    fingerprint = hashlib.sha256(releases.canonical(sources)).hexdigest()[:16]
    return f"{date}-{stack[:8]}-{fingerprint}"


def sign_hub(stack: Path, output: Path, version: str, config: dict) -> None:
    """Build the public, catalog-free Hub with the publisher's exact identity.

    Private capability definitions never leave the machine that runs them, so
    the published artifact carries an empty catalog. A publisher whose own Hub
    embeds capabilities configures `local_catalog`; the equal-identity variant
    built from it is stored machine-locally by promote(), never in the feed.
    """
    from . import build_hub
    identity = json.loads((stack / "host/release-identity.json").read_text())
    variants = [("menubar-notarized.zip", None)]
    catalog_path = config.get("local_catalog")
    if catalog_path:
        variants.append(("hub-catalog.zip", json.loads(Path(catalog_path).read_text())))
    for name, catalog in variants:
        destination = output / (name.removesuffix(".zip") + "-build")
        app = build_hub.build(stack, destination, version, config, catalog=catalog,
                              release_id=identity["release"], github_repo=identity["github_repo"],
                              date=identity["date"])
        build_hub.notarize(app, destination, config)
        shutil.copy2(destination / "hub-notarized.zip", output / name)


def build(config: dict, notes: str, reuse_client: Path | None = None) -> Path:
    candidates = Path(config["candidates_dir"])
    candidates.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="build-", suffix=".noindex", dir=candidates))
    stack, client = work / "stack", work / "Projects/client"
    client.parent.mkdir()
    stack_sha = snapshot(Path(config["stack_repo"]), stack)
    client_sha = snapshot(Path(config["client_repo"]), client)
    dependencies = {}
    for name, repository in config.get("client_packages", {}).items():
        releases.identifier(name)
        target = work / "Packages" / name
        target.parent.mkdir(exist_ok=True)
        dependencies[name] = snapshot(Path(repository), target)
    print(f"Release sources: stack {stack_sha}, client {client_sha}", flush=True)
    # The Hub's identity no longer reads the client's CURRENT_PROJECT_VERSION.
    # Deriving a Hub number from jRemote's project file is what made the two
    # products' versions look comparable when they count different things.
    date = release_date()
    release_id = source_identity(date, stack_sha, client_sha, dependencies)
    output = work / release_id
    output.mkdir()
    version = json.loads((stack / "plugins/jstack/.claude-plugin/plugin.json").read_text())["version"]
    from .release_channel import repository
    github_repo = repository(config.get("github_repo") or command(
        ["git", "-C", str(stack), "remote", "get-url", "origin"]).strip())
    # Ordering, now that nothing counts releases. The identity is a hash and a
    # date, and neither orders: hashes have no order at all, and two releases
    # cut on one day share a date. A hub receiving an offer has to answer "is
    # this ahead of what I run" with no git and no history — so the answer
    # travels inside the signed manifest or it cannot be asked.
    #
    # `rev-list --count` is that answer: the number of commits behind this one
    # on its own line. It is not a version and is never displayed — it exists
    # only to be compared against the sequence of the release a hub already
    # holds, and only within one channel, where the two counts share a root.
    sequence = int(command(["git", "-C", str(stack), "rev-list", "--count", stack_sha]).strip())
    # Which line this release belongs to. `stack_repo` is on whatever branch
    # the releaser checked out, and the snapshot is by sha, so building from a
    # side branch already worked — what was missing was the offer saying which
    # line it came from, so a hub can decline the ones that are not its own.
    channel = command(["git", "-C", str(config["stack_repo"]), "rev-parse",
                       "--abbrev-ref", "HEAD"]).strip()
    channel = releases.STABLE_CHANNEL if channel in ("main", "HEAD") else channel
    # Said out loud because nothing downstream does: a candidate built from a
    # side branch qualifies exactly like one from main, and only this name
    # decides whether any hub is ever offered it.
    print(f"Release channel: {channel}" + ("" if channel in offerable(config) else
          " — not an offerable channel; promote will refuse it without --channel"), flush=True)
    from .sourcestamp import fingerprint
    (stack / "host/release-identity.json").write_text(json.dumps({
        "release": release_id, "sha": stack_sha, "version": version, "date": date,
        "github_repo": github_repo, "sequence": sequence, "channel": channel,
        "package_sha256": fingerprint(stack / "host/jstack_host")}))
    archive = output / "stack.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for path in sorted(stack.iterdir()):
            if path.name != ".git":
                bundle.add(path, arcname=path.name)
    print("Building, signing and notarizing the Hub", flush=True)
    sign_hub(stack, output, version, config)
    app_output = work / "client-output"
    if reuse_client is not None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        private = base64.b64decode(Path(config["private_key"]).read_text().strip(), validate=True)
        public = base64.b64encode(Ed25519PrivateKey.from_private_bytes(private).public_key().public_bytes_raw()).decode()
        prior = releases.verify(json.loads((reuse_client / "candidate.json").read_text()), public, promoted=False)
        if prior["sources"]["client"] != client_sha or prior.get("client_packages", {}) != dependencies:
            raise releases.ReleaseError("client reuse requires identical committed client and shared-package sources")
        item = prior["components"]["client"]
        releases.check_artifact(reuse_client / item["file"], item)
        app_output.mkdir()
        shutil.copy2(reuse_client / item["file"], app_output / item["file"])
        atomic_json(app_output / "latest.json", {**item, "build": item["version"]})
        print("Reusing the signed client artifact from identical committed sources", flush=True)
        return seal(work, config, notes)
    app_script = client / "jRemote-Code/jRemote/release-mac.sh"
    print("Building, signing and notarizing client candidate", flush=True)
    log_path = work / "client-build.log"
    import re
    project = client / "jRemote-Code/jRemote/jRemote.xcodeproj/project.pbxproj"
    current = max(map(int, re.findall(r"CURRENT_PROJECT_VERSION = (\d+);", project.read_text())))
    client_build = allocate_client_build(candidates, current)
    with log_path.open("w") as log:
        process = subprocess.run(["bash", str(app_script), "--build-number", str(client_build), "--candidate-dir", str(app_output),
                                  "--notes", notes], timeout=2400, stdout=log, stderr=subprocess.STDOUT,
                                 env={**os.environ, "JSTACK_CHECKOUT": str(stack)})
    if process.returncode:
        raise releases.ReleaseError(f"client build failed; see {log_path}\n{log_path.read_text()[-4000:]}")
    return seal(work, config, notes)


def seal(work: Path, config: dict, notes: str) -> Path:
    """Resume after completed signing without building different bytes."""
    from . import build_hub
    stack, client = work / "stack", work / "Projects/client"
    identity = json.loads((stack / "host/release-identity.json").read_text())
    release_id, stack_sha = identity["release"], identity["sha"]
    release_id = releases.identifier(release_id)
    output, app_output = work / release_id, work / "client-output"
    for repository in (stack, client, *sorted((work / "Packages").glob("*"))):
        command(["git", "-C", str(repository), "diff", "--quiet", "HEAD", "--"])
    client_sha = command(["git", "-C", str(client), "rev-parse", "HEAD"]).strip()
    dependencies = {path.name: command(["git", "-C", str(path), "rev-parse", "HEAD"]).strip()
                    for path in sorted((work / "Packages").glob("*"))}
    version = json.loads((stack / "plugins/jstack/.claude-plugin/plugin.json").read_text())["version"]
    archive, menu = output / "stack.tar.gz", output / "menubar-notarized.zip"
    app_manifest = json.loads((app_output / "latest.json").read_text())
    releases.identifier(app_manifest["file"])
    app = app_output / app_manifest["file"]
    releases.check_artifact(app, app_manifest)
    destination = output / app.name
    shutil.copy2(app, destination)
    if config.get("local_catalog") and not (output / "hub-catalog.zip").is_file():
        raise releases.ReleaseError("configured private capability variant was not built for this candidate")
    manifest = {"schema": releases.SCHEMA, "release": release_id, "notes": notes,
                "build": identity.get("build"),
                "sequence": identity.get("sequence"),
                "channel": {"github_repo": identity.get("github_repo"),
                            "name": identity.get("channel") or releases.STABLE_CHANNEL},
                "sources": {"stack": stack_sha, "client": client_sha},
                "client_packages": dependencies,
                "components": {"stack": component(archive, version),
                               "menubar": component(menu, build_hub.bundle_version(identity, version)),
                               "client": component(destination, str(app_manifest["build"]))},
                "compatibility": {"protocol": 1, "rollback": True, "platform": "macos",
                                  "architecture": "arm64", "minimum_os": "26.0"},
                "mobile": {"source": client_sha, "status": "not_distributed"},
                "receipts": {}}
    private_key = base64.b64decode(Path(config["private_key"]).read_text().strip(), validate=True)
    atomic_json(output / "candidate.json", releases.sign(manifest, private_key, promoted=False))
    return output


def offerable(config: dict) -> list[str]:
    """The channels this publisher's hubs follow; a release off them reaches no one."""
    return list(config.get("channels") or [releases.STABLE_CHANNEL])


def channel_of(manifest: dict) -> str:
    return manifest.get("channel", {}).get("name") or releases.STABLE_CHANNEL


def check_channel(manifest: dict, channels: list[str], explicit: str | None) -> str:
    """Refuse to publish onto a line no hub follows unless it was named on purpose.

    Candidate 104 was built from a worktree on a fix branch and carried that
    branch as its channel. It could pass all nine journeys and still never be
    offered to a production hub, because they all follow `stable`.
    """
    name = channel_of(manifest)
    if explicit is not None and explicit != name:
        raise releases.ReleaseError(
            f"candidate is on channel {name!r}, not the requested {explicit!r}")
    if name not in channels and explicit is None:
        raise releases.ReleaseError(
            f"candidate is on channel {name!r}, which no configured hub channel "
            f"({', '.join(channels)}) follows; pass --channel {name} to publish it anyway")
    return name


def candidate_manifest(candidate: Path, private_key: bytes) -> dict:
    """The signed candidate's own manifest — never a manifest handed to us."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.from_private_bytes(private_key)
    public = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    envelope = json.loads((candidate / "candidate.json").read_text())
    return copy.deepcopy(releases.verify(envelope, public, promoted=False))


def qualify(config: dict, candidate: Path, receipts: Path, private_key: bytes) -> dict:
    """Run the configured acceptance runner over this candidate, then read it.

    The runner's exit status is a hint; the receipts it wrote are the evidence.
    A runner that dies half way leaves the journeys it never reached missing,
    and missing is what keeps promotion closed.
    """
    runner = config.get("acceptance")
    if not runner:
        raise releases.ReleaseError(
            "configure 'acceptance' with the acceptance runner's command line")
    manifest = candidate_manifest(candidate, private_key)
    print(channel_line(manifest, offerable(config)), flush=True)
    receipts.mkdir(parents=True, exist_ok=True)
    argv = [*runner, "--candidate", str(candidate), "--receipts", str(receipts)]
    print("Running acceptance: " + " ".join(argv), flush=True)
    finished = subprocess.run(argv, timeout=config.get("acceptance_timeout", 6 * 3600))
    state = acceptance.inspect(receipts, manifest)
    for name, entry in state.items():
        print(f"  {name}: {entry['state']}" + (f" — {entry['detail']}" if entry["detail"] else ""),
              flush=True)
    if finished.returncode:
        print(f"acceptance runner exited {finished.returncode}", flush=True)
    return state


def channel_line(manifest: dict, channels: list[str]) -> str:
    name = channel_of(manifest)
    return f"Release channel: {name}" + ("" if name in channels else
                                         f" — NOT offerable (hubs follow {', '.join(channels)})")


def promote(candidate: Path, receipts_dir: Path, feed: Path, private_key: bytes,
            *, local_components: str | None = None,
            channels: list[str] | None = None, channel: str | None = None) -> dict:
    manifest = candidate_manifest(candidate, private_key)
    check_channel(manifest, channels or [releases.STABLE_CHANNEL], channel)
    # One door. Every journey is a genuine pass over these exact artifacts, or
    # this raises and names the ones that are not.
    manifest["receipts"] = acceptance.gate(receipts_dir, manifest)
    envelope = releases.sign(manifest, private_key)
    for item in manifest["components"].values():
        releases.check_artifact(candidate / item["file"], item)
    feed.mkdir(parents=True, exist_ok=True)
    destination = feed / manifest["release"]
    if destination.exists():
        if json.loads((destination / "manifest.json").read_text()) != envelope:
            raise releases.ReleaseError("release ID already published with different contents")
    else:
        staging = Path(tempfile.mkdtemp(prefix=".promote-", dir=feed))
        for item in manifest["components"].values():
            shutil.copy2(candidate / item["file"], staging / item["file"])
        shutil.copytree(receipts_dir, staging / "receipts")
        atomic_json(staging / "manifest.json", envelope)
        os.rename(staging, destination)
    variant = candidate / "hub-catalog.zip"
    if variant.is_file():
        # This machine's own updater refuses the catalog-free public artifact;
        # its equal-identity variant must be in place before the feed offers
        # the release. Machine-local storage only — never the published feed.
        if not local_components:
            raise releases.ReleaseError(
                "candidate carries a private capability variant; configure local_components")
        store = Path(local_components) / manifest["release"]
        store.mkdir(parents=True, exist_ok=True)
        target = store / variant.name
        if not target.exists() or releases.digest(target) != releases.digest(variant):
            shutil.copy2(variant, target)
    # Single publication point. Every referenced artifact and evidence receipt
    # already exists, and the prior release directory remains untouched.
    atomic_json(feed / "latest.json", envelope)
    return envelope


def deploy(release: str, *, port: int = 9090, timeout: int = 2700, poll: int = 15) -> dict:
    """Update this hub and its eligible leaves to a promoted release, and watch.

    Eligibility is the hub's own answer — a machine it has heard from recently
    that reports an update supervisor. A machine that is offline, or too old to
    have one, is reported unreached; it is never counted as deployed because
    nothing was asked of it. Reaching `current` is the hub's independent
    confirmation of the running release, not the leaf's own claim.
    """
    import httpx
    from . import devices
    base = f"http://127.0.0.1:{port}/api/jremote/v1/updates"
    headers = {"Authorization": "Bearer " + devices.internal_token()}
    with httpx.Client(timeout=30, trust_env=False) as client:
        def inventory() -> dict:
            answer = client.get(base + "/inventory", headers=headers)
            answer.raise_for_status()
            return answer.json()

        before = inventory()
        if before.get("release") != release:
            raise releases.ReleaseError(
                f"this hub offers {before.get('release')}, not the promoted {release}")
        eligible = sorted(row["machine"] for row in before["machines"] if row.get("supervisor"))
        unmanaged = sorted(row["machine"] for row in before["machines"] if not row.get("supervisor"))
        if not eligible:
            raise releases.ReleaseError("no machine on this hub can accept a managed update")
        queued = client.post(base + "/queue", headers=headers,
                             json={"target": "all", "request_id": "release-" + release})
        queued.raise_for_status()
        deadline = time.monotonic() + timeout
        states: dict[str, str] = {}
        while True:
            rows = {row["machine"]: row for row in inventory()["machines"]}
            states = {machine: rows.get(machine, {}).get("state", "unknown")
                      for machine in eligible}
            settled = all(state in ("current", "failed", "rolled_back", "cancelled")
                          for state in states.values())
            if settled or time.monotonic() > deadline:
                break
            time.sleep(poll)
    result = {"release": release, "eligible": eligible, "states": states,
              "unreached": sorted(m for m, s in states.items() if s != "current"),
              "no_supervisor": unmanaged, "jobs": queued.json()}
    if result["unreached"]:
        raise releases.ReleaseError(
            "promoted, but these machines did not reach the release: "
            + ", ".join(f"{m} ({states[m]})" for m in result["unreached"]))
    return result


def ship(config: dict, candidate: Path, receipts: Path, private_key: bytes,
         *, deploy_after: bool, channel: str | None = None) -> dict:
    """The one release action: qualify the exact candidate, promote, deploy.

    Nothing is published on a green unit suite. `qualify` produces receipts and
    `promote` re-reads them through the same gate every installing machine
    trusts, so an interrupted or partial acceptance run stops here.
    """
    # Refused before the gate is spent, not after six hours of journeys.
    check_channel(candidate_manifest(candidate, private_key), offerable(config), channel)
    qualify(config, candidate, receipts, private_key)
    envelope = promote(candidate, receipts, Path(config["feed_dir"]), private_key,
                       local_components=config.get("local_components"),
                       channels=offerable(config), channel=channel)
    release = envelope["manifest"]["release"]
    result = {"promoted": release}
    github_repo = config.get("github_repo") or envelope["manifest"].get("channel", {}).get("github_repo")
    if github_repo:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from .release_channel import publish
        public = base64.b64encode(Ed25519PrivateKey.from_private_bytes(private_key).public_key().public_bytes_raw()).decode()
        publish(Path(config["feed_dir"]) / release, github_repo, public)
        result["published"] = release
    if deploy_after:
        result["deployed"] = deploy(release, port=config.get("hub_port", 9090))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=os.environ.get("JSTACK_RELEASE_CONFIG"))
    commands = parser.add_subparsers(dest="action", required=True)
    initialize = commands.add_parser("init-key")
    initialize.add_argument("--private-key", type=Path, required=True)
    create = commands.add_parser("build")
    create.add_argument("--notes", required=True)
    create.add_argument("--reuse-client", type=Path,
                        help="reuse an exact signed client candidate only when its source inputs are unchanged")
    finish = commands.add_parser("seal", help="resume a fully built candidate without rebuilding")
    finish.add_argument("work", type=Path)
    finish.add_argument("--notes", required=True)
    publish = commands.add_parser("promote")
    publish.add_argument("candidate", type=Path)
    publish.add_argument("--receipts", type=Path, required=True)
    publish.add_argument("--channel", help="publish a candidate on a channel no configured hub follows")
    prove = commands.add_parser("qualify", help="run the acceptance runner over a candidate")
    prove.add_argument("candidate", type=Path)
    prove.add_argument("--receipts", type=Path, required=True)
    state = commands.add_parser("acceptance", help="what this candidate's receipts prove today")
    state.add_argument("candidate", type=Path)
    state.add_argument("--receipts", type=Path, required=True)
    whole = commands.add_parser("ship", help="qualify, promote and deploy one candidate")
    whole.add_argument("candidate", type=Path)
    whole.add_argument("--receipts", type=Path, required=True)
    whole.add_argument("--channel", help="publish a candidate on a channel no configured hub follows")
    whole.add_argument("--deploy", action="store_true",
                       help="after promotion, update this hub and its eligible leaves")
    args = parser.parse_args()
    if args.action == "init-key":
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        key = Ed25519PrivateKey.generate()
        descriptor = os.open(args.private_key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(base64.b64encode(key.private_bytes_raw()).decode() + "\n")
        print(base64.b64encode(key.public_key().public_bytes_raw()).decode())
        return
    if args.config is None:
        parser.error("set JSTACK_RELEASE_CONFIG to the publishing machine's release configuration")
    config = json.loads(args.config.read_text())
    if args.action == "build":
        print(build(config, args.notes, args.reuse_client))
        return
    if args.action == "seal":
        print(seal(args.work, config, args.notes))
        return
    private = base64.b64decode(Path(config["private_key"]).read_text().strip(), validate=True)
    if args.action == "qualify":
        state = qualify(config, args.candidate, args.receipts, private)
        print(json.dumps({name: entry["state"] for name, entry in state.items()}, indent=2))
    elif args.action == "acceptance":
        manifest = candidate_manifest(args.candidate, private)
        # stderr: stdout stays the JSON it always was.
        print(channel_line(manifest, offerable(config)), file=sys.stderr)
        state = acceptance.inspect(args.receipts, manifest)
        print(json.dumps({name: {"state": entry["state"], "detail": entry["detail"]}
                          for name, entry in state.items()}, indent=2))
    elif args.action == "ship":
        print(json.dumps(ship(config, args.candidate, args.receipts, private,
                              deploy_after=args.deploy, channel=args.channel), indent=2))
    else:
        result = promote(args.candidate, args.receipts, Path(config["feed_dir"]), private,
                         local_components=config.get("local_components"),
                         channels=offerable(config), channel=args.channel)
        print(json.dumps({"promoted": result["manifest"]["release"]}))


if __name__ == "__main__":
    main()
