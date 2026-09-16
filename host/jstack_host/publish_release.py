"""Build exact source snapshots, then promote only an evidenced candidate."""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from . import release_manifest as releases
from .update_macos import command, safe_tar
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


def sign_menu(stack: Path, output: Path, version: str, config: dict) -> Path:
    password_path = config.get("sign_keychain_password_file")
    if password_path:
        command(["/usr/bin/security", "unlock-keychain", "-p", Path(password_path).read_text().strip(),
                 config["sign_keychain"]])
        command(["/usr/bin/security", "set-keychain-settings", config["sign_keychain"]])
        state = subprocess.run(["/usr/bin/security", "show-keychain-info", config["sign_keychain"]],
                               capture_output=True, text=True, timeout=15)
        if state.returncode or "no-timeout" not in state.stdout + state.stderr:
            raise releases.ReleaseError("signing keychain is not unlocked with no timeout")
    app = output / "JStack Host.app"
    binary = app / "Contents/MacOS/JStackHostBar"
    binary.parent.mkdir(parents=True)
    command(["xcrun", "swiftc", "-O", "-o", str(binary),
             str(stack / "host/menubar/JStackHostBar.swift")], timeout=180)
    info = {"CFBundleExecutable": "JStackHostBar", "CFBundleIdentifier": "com.jremote.menubar",
            "CFBundleName": "JStack Host", "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": version, "CFBundleVersion": version,
            "LSUIElement": True, "NSHighResolutionCapable": True,
            "CFBundleURLTypes": [{"CFBundleURLName": "jStack updates", "CFBundleURLSchemes": ["jstack"]}]}
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    command(["/usr/bin/codesign", "--force", "--timestamp", "--options", "runtime",
             "--keychain", config["sign_keychain"], "--sign", config["sign_identity"], str(app)])
    archive = output / "menubar.zip"
    command(["/usr/bin/ditto", "-c", "-k", "--keepParent", str(app), str(archive)])
    credentials = json.loads(Path(config["notary_credentials"]).read_text())
    # Same Developer ID / notarytool / stapler pipeline as the client release.
    notary = command(["xcrun", "notarytool", "submit", str(archive), "--key", str(Path(credentials["private_key_path"]).expanduser()),
                      "--key-id", credentials["key_id"], "--issuer", credentials["issuer_id"],
                      "--wait", "--output-format", "json"], timeout=1200)
    if json.loads(notary).get("status") != "Accepted":
        raise releases.ReleaseError("menu bar notarization was not accepted")
    command(["xcrun", "stapler", "staple", str(app)])
    command(["/usr/sbin/spctl", "--assess", "--type", "execute", str(app)])
    # Re-archive the stapled bundle, not the unstapled notary upload.
    final = output / "menubar-notarized.zip"
    command(["/usr/bin/ditto", "-c", "-k", "--keepParent", str(app), str(final)])
    return final


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
    release_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + stack_sha[:8]
    output = work / release_id
    output.mkdir()
    version = json.loads((stack / "plugins/jstack/.claude-plugin/plugin.json").read_text())["version"]
    from .sourcestamp import fingerprint
    (stack / "host/release-identity.json").write_text(json.dumps({
        "release": release_id, "sha": stack_sha,
        "package_sha256": fingerprint(stack / "host/jstack_host")}))
    archive = output / "stack.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for path in sorted(stack.iterdir()):
            if path.name != ".git":
                bundle.add(path, arcname=path.name)
    print("Building, signing and notarizing menu bar", flush=True)
    menu = sign_menu(stack, output, version, config)
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
    with log_path.open("w") as log:
        process = subprocess.run(["bash", str(app_script), "--no-bump", "--candidate-dir", str(app_output),
                                  "--notes", notes], timeout=2400, stdout=log, stderr=subprocess.STDOUT,
                                 env={**os.environ, "JSTACK_CHECKOUT": str(stack)})
    if process.returncode:
        raise releases.ReleaseError(f"client build failed; see {log_path}\n{log_path.read_text()[-4000:]}")
    return seal(work, config, notes)


def seal(work: Path, config: dict, notes: str) -> Path:
    """Resume after completed signing without building different bytes."""
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
    manifest = {"schema": 1, "release": release_id, "notes": notes,
                "sources": {"stack": stack_sha, "client": client_sha},
                "client_packages": dependencies,
                "components": {"stack": component(archive, version),
                               "menubar": component(menu, version),
                               "client": component(destination, str(app_manifest["build"]))},
                "compatibility": {"protocol": 1, "rollback": True, "platform": "macos",
                                  "architecture": "arm64", "minimum_os": "26.0"},
                "mobile": {"source": client_sha, "status": "not_distributed"},
                "receipts": {}}
    private_key = base64.b64decode(Path(config["private_key"]).read_text().strip(), validate=True)
    atomic_json(output / "candidate.json", releases.sign(manifest, private_key, promoted=False))
    return output


def promote(candidate: Path, receipts_dir: Path, feed: Path, private_key: bytes) -> dict:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.from_private_bytes(private_key)
    public = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    envelope = json.loads((candidate / "candidate.json").read_text())
    manifest = copy.deepcopy(releases.verify(envelope, public, promoted=False))
    for name in releases.RECEIPTS:
        receipt = json.loads((receipts_dir / (name + ".json")).read_text())
        evidence = receipts_dir / (name + ".log")
        if releases.digest(evidence) != receipt.get("evidence_sha256"):
            raise releases.ReleaseError(f"acceptance evidence missing or changed: {name}")
        manifest["receipts"][name] = receipt
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
    # Single publication point. Every referenced artifact and evidence receipt
    # already exists, and the prior release directory remains untouched.
    atomic_json(feed / "latest.json", envelope)
    return envelope


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
    elif args.action == "seal":
        print(seal(args.work, config, args.notes))
    else:
        private = base64.b64decode(Path(config["private_key"]).read_text().strip(), validate=True)
        result = promote(args.candidate, args.receipts, Path(config["feed_dir"]), private)
        print(json.dumps({"promoted": result["manifest"]["release"]}))


if __name__ == "__main__":
    main()
