"""Build a self-contained app candidate; never install or register it here.

The Python framework is an explicit build input. Dependencies are resolved
into the candidate only, never copied from the operator's site-packages.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
from typing import NamedTuple

from .update_macos import command

ROLES = {"host": ("JStackRuntime", "host"),
         "updater": ("JStackRuntime", "updater"),
         "menu": ("JStackHostBar",)}
MAGICS = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}


def service_plist(role: str) -> dict:
    binary, *arguments = ROLES[role]
    return {"Label": f"live.jstack.hub.{role}",
            "BundleProgram": f"Contents/MacOS/{binary}",
            "ProgramArguments": [binary, *arguments],
            "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
            "AssociatedBundleIdentifiers": ["live.jstack.hub"]}


def bundle_version(identity: dict, version: str) -> str:
    """The menubar's CFBundleVersion — and the only thing a manifest may record
    as that component's version.

    The updater stages a release by comparing the built bundle's
    CFBundleVersion against the version the manifest claims for it, so these
    two cannot be derived independently. They were, and when the build counter
    was removed the seam split: the plist started carrying the release date
    while the manifest kept falling back to the short version, so every
    candidate died in staging with "menubar bundle version differs from
    release" — after signing and notarisation, on the installing machine.

    The date the release was cut, digits only: macOS wants a monotonic
    CFBundleVersion and this is the honest one. A dev build has no date and
    falls back to the version, exactly as it did before.
    """
    return str(identity.get("date") or version).replace("-", "")


def hub_info(version: str, identity: dict) -> dict:
    """The one bundle the user grants to: its name, identity and purposes."""
    return {
        "CFBundleIdentifier": "live.jstack.hub", "CFBundleExecutable": "JStackHub",
        "CFBundleName": "jStack Hub", "CFBundleDisplayName": "jStack Hub",
        "CFBundlePackageType": "APPL",
        "CFBundleVersion": bundle_version(identity, version),
        "CFBundleShortVersionString": version, "LSUIElement": True,
        "LSMinimumSystemVersion": "13.0",
        "NSLocalNetworkUsageDescription":
            "jStack Hub serves this machine's dashboard and reaches your paired machines on the local network.",
        "NSAppleEventsUsageDescription":
            "jStack Hub automations control local applications only when a job you configured requires it.",
        "NSDesktopFolderUsageDescription":
            "jStack Hub automations read and organize files here only when a job you configured requires it.",
        "NSDocumentsFolderUsageDescription":
            "jStack Hub automations read and organize files here only when a job you configured requires it.",
        "NSDownloadsFolderUsageDescription":
            "jStack Hub automations read and organize files here only when a job you configured requires it.",
        "CFBundleURLTypes": [{"CFBundleURLName": "jStack updates", "CFBundleURLSchemes": ["jstack"]}]}


def macho(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as stream:
        return stream.read(4) in MAGICS


def relocate(path: Path, source: Path, target: Path):
    """Every non-system absolute dependency must resolve inside this app."""
    lines = command(["/usr/bin/otool", "-L", str(path)]).splitlines()
    changes = []
    for line in lines:
        if not line.startswith("\t"):
            continue
        dependency = line.strip().split(" (", 1)[0]
        if dependency.startswith(str(source) + "/"):
            destination = target / Path(dependency).relative_to(source)
            if not destination.exists():
                raise ValueError(f"missing bundled library: {dependency}")
            changes.extend(["-change", dependency,
                            "@loader_path/" + os.path.relpath(destination, path.parent)])
        elif dependency.startswith("/") and not dependency.startswith(("/usr/lib/", "/System/Library/")):
            raise ValueError(f"unbundled dependency: {dependency}")
    if changes:
        command(["/usr/bin/install_name_tool", *changes, str(path)])


def release_identity(source_sha: str, version: str, *, release_id=None, github_repo=None,
                     date=None, channel=None, origin=None) -> dict:
    """A Hub release is its source hash and the day it was cut — never a counter.

    The Hub is not App Store distributed, so nothing requires a monotonically
    rising CFBundleVersion, and a counter here was actively harmful: it read as
    comparable to jRemote's own build numbers while counting something else
    entirely. Ordering is not the identity's job either — whichever hash is
    promoted is current, by definition, so rollback is promoting another hash
    rather than out-numbering the last one.

    Two things the bundle has to answer for itself, because the things that
    read them have nothing else to ask. `channel` is the ref these bytes were
    built from: `install_signed.provision` writes the hub's config out of this
    file, and with no ref in it every branch install was provisioned to follow
    stable. `origin` is the marker that says a machine built this for itself,
    which is what lets the installer's bundle gate ask the pinned key instead
    of a signing team no such machine holds. Both are sealed by the signature
    over the bundle, which is the only reason either can be believed.
    """
    from .release_manifest import SOURCE_BUILD, identifier
    from .build_source import channel_ref, repository
    if any(value is not None for value in (release_id, github_repo, date)):
        if not release_id or not github_repo or not _is_date(date):
            raise ValueError("release builds require release ID, GitHub origin and an ISO date together")
        identity = {"sha": source_sha, "release": identifier(release_id), "version": version,
                    "date": date, "github_repo": repository(github_repo)}
        if channel is not None:
            # `channel_ref` reads an empty ref as `stable`, which is right for a
            # config written before refs existed and wrong for a caller naming
            # one: silently following main is the defect this field exists to
            # close, not an acceptable default for a build that asked.
            if not channel:
                raise ValueError("a build that records a release line must name one")
            identity["channel"] = channel_ref({"channel": channel})
        if origin is not None:
            if not isinstance(origin, dict) or origin.get("kind") != SOURCE_BUILD:
                raise ValueError("a bundle records no origin but the one its manifest records")
            identity["origin"] = origin
        return identity
    if channel is not None or origin is not None:
        raise ValueError("a dev build follows no release line and has no release origin")
    return {"sha": source_sha, "release": f"hub-{version}-{source_sha[:8]}", "version": version}


def _is_date(value) -> bool:
    """An ISO day, and a real one — `2026-02-31` is a typo, not a release date."""
    from datetime import date as _date
    if type(value) is not str:
        return False
    try:
        return _date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


#: The mesh tooling, which is not importable Python and so is not something
#: `pip install` will carry. `hostenv.peer_script()` resolves
#: `package_root()/scripts/wireguard/wg_peer.py`, and in a shipped app
#: `package_root()` is `Contents/Resources/packages` — the directory pip
#: writes. pip installs the `jstack_host` package and nothing else in `host/`,
#: so every signed Hub shipped without `wg_peer.py`: `tunnel.can_pair()` was
#: False on a real hub holding a real peer table, which is device pairing,
#: machine adoption and every leaf bundle refusing at once. The refusal even
#: named `wg0.conf`, the half that was present. Staged here, beside the
#: package, because that is where the readers already look.
MESH_TOOLS = "scripts"

#: Where the installer puts the audited CPython 3.12 this Hub is built around.
FRAMEWORK_PYTHON = "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
RUNTIME = "3.12"

#: One question, asked of a candidate in its own process: which framework it
#: belongs to, what it calls itself, and whether it carries pip.
PROBE = ("import importlib.util, json, sys;"
         "print(json.dumps({'prefix': sys.base_prefix, 'version': sys.version.split()[0],"
         " 'pip': importlib.util.find_spec('pip') is not None}))")


class Interpreter(NamedTuple):
    """The CPython a Hub is built *around*: its binary, its framework, its version."""

    executable: str
    framework: Path
    version: str


def build_interpreter() -> Interpreter:
    """Locate that CPython. Never assume the running process is it.

    Two assumptions lived here as one, and neither holds. `jstack-host` runs
    from the checkout venv, which the installer builds on whatever python3 the
    Mac already had — 3.14 from Homebrew on a stock machine; the sealed Hub
    runs JStackPython, which is 3.12 but ships the host package and its
    dependencies and never pip. So `updates build` died on the version guard
    from the CLI and on "No module named pip" from the service, install worked
    because the installer builds under a venv it made on the framework, and no
    machine could move itself forward — which also left rollback, switch the
    branch and rebuild, with nothing to rebuild with.

    Everything the build takes from an interpreter comes from this one answer:
    the `Python` binary and `lib` copied into the bundle, the ABI its wheels
    must match, and the version stamped on the nested framework. The framework
    interpreter is the installer's own hard requirement, so a Mac that could
    install can always build.
    """
    refused: list[tuple[str, str]] = []
    for candidate in (os.environ.get("JSTACK_BUILD_PYTHON"), FRAMEWORK_PYTHON, sys.executable):
        if not candidate or any(candidate == name for name, _ in refused):
            continue
        found = subprocess.run([candidate, "-c", PROBE], capture_output=True,
                               text=True, timeout=120)
        if found.returncode != 0:
            refused.append((candidate, "did not run"))
            continue
        try:
            answer = json.loads(found.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            refused.append((candidate, "answered nothing"))
            continue
        framework = Path(answer["prefix"])
        if not str(answer["version"]).startswith(RUNTIME + "."):
            refused.append((candidate, "is " + str(answer["version"])))
        elif not (framework / "Python").is_file():
            refused.append((candidate, "is not a framework build"))
        elif not answer["pip"]:
            refused.append((candidate, "has no pip"))
        else:
            return Interpreter(candidate, framework, str(answer["version"]))
    raise RuntimeError(
        f"this Hub is built around the audited CPython {RUNTIME} framework and nothing here "
        "is one — " + "; ".join(f"{name} {why}" for name, why in refused)
        + f". Install python.org's macOS {RUNTIME} package, or point JSTACK_BUILD_PYTHON at "
          "a framework interpreter that has pip.")


def stage_mesh_tools(stack: Path, packages: Path) -> Path:
    """Put the mesh scripts where `hostenv.peer_script()` reads them.

    Executable bits are the payload here, not a detail: `wg_peer.py` is spawned
    and the leaf installers are handed to joining machines to run. `copytree`
    preserves mode, and the absence check below is what turns a renamed or
    dropped script into a failed build rather than a Hub that installs, signs,
    notarizes and then cannot mint a peer.
    """
    source = stack / "host" / MESH_TOOLS
    staged = packages / MESH_TOOLS
    shutil.copytree(source, staged, ignore=shutil.ignore_patterns("__pycache__"))
    required = ("wireguard/wg_peer.py", "wireguard/install_hub.sh",
                "wireguard/install_leaf.sh", "wireguard/wg_up.sh",
                "wireguard/wg_leaf_watch.sh", "wireguard/wg_sync.sh")
    for name in required:
        path = staged / name
        if not path.is_file():
            raise ValueError(f"the mesh tooling is missing {name} — this Hub could not pair")
        if not os.access(path, os.X_OK):
            raise ValueError(f"{name} is staged without its executable bit")
    return staged


def build(stack: Path, output: Path, version: str, config: dict | None = None, *, catalog=None,
          release_id=None, github_repo=None, date=None, trust_key=None,
          channel=None, origin=None) -> Path:
    # Build one immutable git snapshot. A clean-tree check alone does not
    # exclude untracked package files or concurrent changes during pip/build.
    command(["git", "-C", str(stack), "diff", "--quiet", "HEAD", "--", "host"])
    source_sha = command(["git", "-C", str(stack), "rev-parse", "HEAD"]).strip()
    identity = release_identity(source_sha, version, release_id=release_id,
                                github_repo=github_repo, date=date,
                                channel=channel, origin=origin)
    with tempfile.TemporaryDirectory(prefix="jstack-source-") as temporary:
        root = Path(temporary)
        archive = root / "source.tar"
        snapshot = root / "source"
        snapshot.mkdir()
        command(["git", "-C", str(stack), "archive", "--format=tar", "-o", str(archive), source_sha])
        command(["/usr/bin/tar", "-xf", str(archive), "-C", str(snapshot)])
        return _build(snapshot, output, version, config, catalog=catalog, identity=identity,
                      trust_key=trust_key)


def _build(stack: Path, output: Path, version: str, config: dict | None, *, catalog,
           identity: dict, trust_key=None) -> Path:
    interpreter = build_interpreter()
    source = interpreter.framework
    info = hub_info(version, identity)
    app_name = info["CFBundleName"]
    bundle_id = info["CFBundleIdentifier"]
    app = output / f"{app_name}.app"
    app.mkdir(parents=True, exist_ok=False)
    contents = app / "Contents"
    macos, resources = contents / "MacOS", contents / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir()
    framework = contents / "Frameworks/Python.framework"
    runtime = framework / "Versions/3.12"
    runtime.mkdir(parents=True)
    shutil.copy2(source / "Python", runtime / "Python")
    shutil.copytree(source / "lib", runtime / "lib", symlinks=True,
                    ignore=shutil.ignore_patterns("site-packages", "__pycache__", "*.pyc", "*.a", "pkgconfig"))
    # Framework metadata and conventional links make the nested code object
    # independently verifiable by codesign and Gatekeeper.
    (runtime / "Resources").mkdir()
    (runtime / "Resources/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "live.jstack.python", "CFBundleExecutable": "Python",
        "CFBundleName": "jStack Python", "CFBundlePackageType": "FMWK",
        "CFBundleVersion": interpreter.version}))
    (framework / "Versions/Current").symlink_to("3.12")
    (framework / "Python").symlink_to("Versions/Current/Python")
    (framework / "Resources").symlink_to("Versions/Current/Resources")
    packages = resources / "packages"
    with tempfile.TemporaryDirectory(prefix="jstack-package-") as temporary:
        package_source = Path(temporary) / "host"
        shutil.copytree(stack / "host", package_source,
                        ignore=shutil.ignore_patterns(".venv", "build", "*.egg-info", "__pycache__"))
        command([interpreter.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-compile",
                 "--target", str(packages), "--report", str(resources / "dependency-resolution.json"),
                 str(package_source)], timeout=900)
    report_path = resources / "dependency-resolution.json"
    report = json.loads(report_path.read_text())
    for item in report.get("install", []):
        if item.get("download_info", {}).get("url", "").startswith("file:"):
            item["download_info"] = {"url": "source:jstack-host"}
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    for metadata in packages.glob("jstack_host-*.dist-info/direct_url.json"):
        metadata.write_text(json.dumps({"url": "source:jstack-host", "dir_info": {}}) + "\n")
    shutil.copy2(stack / "host/macos/runtime_entry.py", resources / "runtime_entry.py")
    stage_mesh_tools(stack, packages)
    if trust_key is not None:
        # The key this Hub will verify its own future builds against. Written
        # before `sign()` seals the bundle, because a file added afterwards
        # invalidates the signature that makes it trustworthy at all.
        (packages / "jstack_host/release-trust.json").write_text(json.dumps(
            {"algorithm": "Ed25519", "public_key": trust_key}, indent=2) + "\n")
    from .sourcestamp import fingerprint
    (packages / "release-identity.json").write_text(json.dumps({
        **identity,
        "package_sha256": fingerprint(packages / "jstack_host")}) + "\n")
    # The runtime gate asks who signed the bundle it is about to import from.
    # A Hub built on the Mac that will run it is signed ad-hoc — that Mac holds
    # no Developer ID — so asking for the publisher's team makes every such
    # build unlaunchable, which is what it did. The answer is chosen here, at
    # compile time, so the seal covers the choice: a bundle that relaxed its
    # own requirement would no longer verify. A published Hub is unchanged.
    origin = ["-DJSTACK_SOURCE_BUILD"] if isinstance(identity.get("origin"), dict) else []
    command(["xcrun", "clang", "-O2", "-Wall", "-Wextra", "-Werror", "-mmacosx-version-min=13.0",
             "-framework", "Security", "-framework", "CoreFoundation", *origin,
             "-I" + str(source / "include/python3.12"), str(stack / "host/macos/Runtime.c"),
             str(source / "Python"), "-o", str(macos / "JStackRuntime")])
    shutil.copy2(macos / "JStackRuntime", macos / "JStackCLI")
    command(["xcrun", "clang", "-O2", "-Wall", "-Wextra", "-Werror", "-mmacosx-version-min=13.0",
             "-framework", "Security", "-framework", "CoreFoundation", *origin,
             "-DJSTACK_PYTHON", "-I" + str(source / "include/python3.12"),
             str(stack / "host/macos/Runtime.c"), str(source / "Python"), "-o", str(macos / "JStackPython")])
    for name, relative in (("JStackHub", "host/macos/ServiceControl.swift"),
                           ("JStackHostBar", "host/menubar/JStackHostBar.swift")):
        command(["xcrun", "swiftc", "-O", "-o", str(macos / name), str(stack / relative)], timeout=180)
    from .bundle_tools import bundle
    tmux = shutil.which("tmux")
    if not tmux:
        raise ValueError("tmux is required as a build input")
    bundle(Path(tmux), macos / "tmux", contents / "Frameworks/Tools", resources / "Licenses")
    python_license = source / "Resources/English.lproj/Documentation/license.html"
    if not python_license.is_file():
        raise ValueError("Python distribution license notice is required")
    shutil.copy2(python_license, resources / "Licenses/Python-license.html")
    definitions = contents / "Library/LaunchAgents"
    definitions.mkdir(parents=True)
    services = {}
    for role in ("host", "menu", "updater"):
        definition = service_plist(role)
        definition["AssociatedBundleIdentifiers"] = [bundle_id]
        (definitions / f"live.jstack.hub.{role}.plist").write_bytes(plistlib.dumps(definition))
        services[role] = f"live.jstack.hub.{role}.plist"
    if catalog is not None:
        from .service_catalog import definitions as catalog_definitions
        jobs, manifest = catalog_definitions(catalog)
        for reserved in ("host", "menu", "updater"):
            if reserved in manifest:
                raise ValueError(f"{reserved} is a reserved capability identifier")
        for name, definition in jobs.items():
            definition["AssociatedBundleIdentifiers"] = [bundle_id]
            (definitions / name).write_bytes(plistlib.dumps(definition))
        services.update({slug: item["plist"] for slug, item in manifest.items()})
        (resources / "automation-catalog.json").write_text(json.dumps(manifest, indent=2) + "\n")
    else:
        (resources / "automation-catalog.json").write_text("{}\n")
    (resources / "services.json").write_text(json.dumps(services, indent=2) + "\n")
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    binaries = [path for path in contents.rglob("*") if macho(path)]
    for path in binaries:
        relocate(path, source, runtime)
    # Catch escaping links too: a sealed signature must not conceal a runtime
    # dependency on this publisher's machine.
    for path in contents.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(contents.resolve()):
            raise ValueError(f"escaping runtime symlink: {path.relative_to(contents)}")
    sign(app, config)
    return app


def sign(app: Path, config: dict | None):
    binaries = [path for path in app.rglob("*") if macho(path)]
    signing = ["/usr/bin/codesign", "--force", "--options", "runtime"]
    if config:
        if config.get("sign_keychain_password_file"):
            command(["/usr/bin/security", "unlock-keychain", "-p",
                     Path(config["sign_keychain_password_file"]).read_text().strip(), config["sign_keychain"]])
            command(["/usr/bin/security", "set-keychain-settings", config["sign_keychain"]])
        signing += ["--timestamp", "--keychain", config["sign_keychain"], "--sign", config["sign_identity"]]
    else:
        signing += ["--sign", "-"]
    for path in sorted(binaries, key=lambda p: len(p.parts), reverse=True):
        command([*signing, str(path)])
    for framework in sorted(app.rglob("*.framework"), key=lambda p: len(p.parts), reverse=True):
        command([*signing, str(framework)])
    command([*signing, str(app)])
    command(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)])


def notarize(app: Path, output: Path, config: dict):
    archive = output / "hub-notary.zip"
    command(["/usr/bin/ditto", "-c", "-k", "--keepParent", str(app), str(archive)])
    credentials = json.loads(Path(config["notary_credentials"]).read_text())
    result = json.loads(command(["xcrun", "notarytool", "submit", str(archive),
        "--key", str(Path(credentials["private_key_path"]).expanduser()),
        "--key-id", credentials["key_id"], "--issuer", credentials["issuer_id"],
        "--wait", "--output-format", "json"], timeout=1200))
    (output / "notary-result.json").write_text(json.dumps(result, indent=2) + "\n")
    if result.get("status") != "Accepted":
        raise ValueError("notarization was not accepted")
    command(["xcrun", "stapler", "staple", str(app)])
    command(["/usr/sbin/spctl", "--assess", "--type", "execute", str(app)])
    command(["/usr/bin/ditto", "-c", "-k", "--keepParent", str(app), str(output / "hub-notarized.zip")])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--release-id")
    parser.add_argument("--github-repo")
    parser.add_argument("--date", help="ISO day this release is cut, e.g. 2026-09-21")
    parser.add_argument("--channel", help="the ref these bytes are built from; absent means stable")
    parser.add_argument("--trust-key", help="base64 Ed25519 public key this Hub will verify its updates against")
    parser.add_argument("--signing-config", type=Path)
    parser.add_argument("--notarize", action="store_true")
    parser.add_argument("--catalog", type=Path, help="private optional capability definitions; never publish this variant")
    args = parser.parse_args()
    config = json.loads(args.signing_config.read_text()) if args.signing_config else None
    if args.notarize and not config:
        parser.error("--notarize requires --signing-config")
    catalog = json.loads(args.catalog.read_text()) if args.catalog else None
    app = build(args.stack.resolve(), args.output.resolve(), args.version, config, catalog=catalog,
                release_id=args.release_id, github_repo=args.github_repo, date=args.date,
                trust_key=args.trust_key, channel=args.channel)
    if args.notarize:
        notarize(app, args.output.resolve(), config)
    print(app)


if __name__ == "__main__":
    main()
