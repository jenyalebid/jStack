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

from .update_macos import command

ROLES = {"host": ("JStackRuntime", "host"),
         "updater": ("JStackRuntime", "updater"),
         "services-handoff": ("JStackRuntime", "services-handoff"),
         "menu": ("JStackHostBar",)}
MAGICS = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}


def service_plist(role: str) -> dict:
    binary, *arguments = ROLES[role]
    return {"Label": f"live.jstack.hub.{role}",
            "BundleProgram": f"Contents/MacOS/{binary}",
            "ProgramArguments": [binary, *arguments],
            "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
            "AssociatedBundleIdentifiers": ["live.jstack.hub"]}


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


def release_identity(source_sha: str, version: str, *, release_id=None, github_repo=None, build_number=None) -> dict:
    from .release_manifest import identifier
    from .release_channel import repository
    if any(value is not None for value in (release_id, github_repo, build_number)):
        if not release_id or not github_repo or type(build_number) is not int or build_number <= 0:
            raise ValueError("release builds require release ID, GitHub origin and positive build number together")
        return {"sha": source_sha, "release": identifier(release_id), "version": version,
                "build": build_number, "github_repo": repository(github_repo)}
    return {"sha": source_sha, "release": f"hub-{version}-{source_sha[:8]}", "version": version}


def build(stack: Path, output: Path, version: str, config: dict | None = None, *, recovery=False, catalog=None,
          release_id=None, github_repo=None, build_number=None) -> Path:
    # Build one immutable git snapshot. A clean-tree check alone does not
    # exclude untracked package files or concurrent changes during pip/build.
    command(["git", "-C", str(stack), "diff", "--quiet", "HEAD", "--", "host"])
    source_sha = command(["git", "-C", str(stack), "rev-parse", "HEAD"]).strip()
    identity = release_identity(source_sha, version, release_id=release_id,
                                github_repo=github_repo, build_number=build_number)
    with tempfile.TemporaryDirectory(prefix="jstack-source-") as temporary:
        root = Path(temporary)
        archive = root / "source.tar"
        snapshot = root / "source"
        snapshot.mkdir()
        command(["git", "-C", str(stack), "archive", "--format=tar", "-o", str(archive), source_sha])
        command(["/usr/bin/tar", "-xf", str(archive), "-C", str(snapshot)])
        return _build(snapshot, output, version, config, recovery=recovery, catalog=catalog, identity=identity)


def _build(stack: Path, output: Path, version: str, config: dict | None, *, recovery, catalog, identity: dict) -> Path:
    if sys.version_info[:2] != (3, 12):
        raise ValueError("this runtime build requires the audited CPython 3.12 framework")
    source = Path(sys.base_prefix)
    if not (source / "Python").is_file():
        raise ValueError("a framework Python build is required")
    if catalog is not None and not recovery:
        raise ValueError("optional local capabilities belong to the stable services bundle")
    app_name = "jStack Hub Services" if recovery else "jStack Hub"
    bundle_id = "live.jstack.hub.services" if recovery else "live.jstack.hub"
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
        "CFBundleVersion": sys.version.split()[0]}))
    (framework / "Versions/Current").symlink_to("3.12")
    (framework / "Python").symlink_to("Versions/Current/Python")
    (framework / "Resources").symlink_to("Versions/Current/Resources")
    packages = resources / "packages"
    with tempfile.TemporaryDirectory(prefix="jstack-package-") as temporary:
        package_source = Path(temporary) / "host"
        shutil.copytree(stack / "host", package_source,
                        ignore=shutil.ignore_patterns(".venv", "build", "*.egg-info", "__pycache__"))
        command([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-compile",
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
    from .sourcestamp import fingerprint
    (packages / "release-identity.json").write_text(json.dumps({
        **identity,
        "package_sha256": fingerprint(packages / "jstack_host")}) + "\n")
    command(["xcrun", "clang", "-O2", "-Wall", "-Wextra", "-Werror", "-mmacosx-version-min=13.0",
             "-framework", "Security", "-framework", "CoreFoundation",
             "-I" + str(source / "include/python3.12"), str(stack / "host/macos/Runtime.c"),
             str(source / "Python"), "-o", str(macos / "JStackRuntime")])
    shutil.copy2(macos / "JStackRuntime", macos / "JStackCLI")
    command(["xcrun", "clang", "-O2", "-Wall", "-Wextra", "-Werror", "-mmacosx-version-min=13.0",
             "-framework", "Security", "-framework", "CoreFoundation",
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
    for role in (["updater"] if recovery else ["host", "menu", "services-handoff"]):
        definition = service_plist(role)
        definition["AssociatedBundleIdentifiers"] = [bundle_id]
        (definitions / f"live.jstack.hub.{role}.plist").write_bytes(plistlib.dumps(definition))
        services[role] = f"live.jstack.hub.{role}.plist"
    if catalog is not None:
        from .service_catalog import definitions as catalog_definitions
        jobs, manifest = catalog_definitions(catalog)
        if "updater" in manifest:
            raise ValueError("updater is a reserved capability identifier")
        for name, definition in jobs.items():
            definition["AssociatedBundleIdentifiers"] = [bundle_id]
            (definitions / name).write_bytes(plistlib.dumps(definition))
        services.update({slug: item["plist"] for slug, item in manifest.items()})
        (resources / "automation-catalog.json").write_text(json.dumps(manifest, indent=2) + "\n")
    elif recovery:
        (resources / "automation-catalog.json").write_text("{}\n")
    (resources / "services.json").write_text(json.dumps(services, indent=2) + "\n")
    (contents / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": bundle_id, "CFBundleExecutable": "JStackHub",
        "CFBundleName": app_name, "CFBundleDisplayName": app_name,
        "CFBundlePackageType": "APPL", "CFBundleVersion": str(identity.get("build", version)),
        "CFBundleShortVersionString": version, "LSUIElement": True,
        "LSMinimumSystemVersion": "13.0",
        "CFBundleURLTypes": [] if recovery else [{"CFBundleURLName": "jStack updates", "CFBundleURLSchemes": ["jstack"]}]}))
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
    parser.add_argument("--build-number", type=int)
    parser.add_argument("--signing-config", type=Path)
    parser.add_argument("--notarize", action="store_true")
    parser.add_argument("--recovery", action="store_true", help="build the separately installed, versioned updater")
    parser.add_argument("--catalog", type=Path, help="private optional capability definitions; never publish this variant")
    args = parser.parse_args()
    config = json.loads(args.signing_config.read_text()) if args.signing_config else None
    if args.notarize and not config:
        parser.error("--notarize requires --signing-config")
    catalog = json.loads(args.catalog.read_text()) if args.catalog else None
    app = build(args.stack.resolve(), args.output.resolve(), args.version, config, recovery=args.recovery, catalog=catalog,
                release_id=args.release_id, github_repo=args.github_repo, build_number=args.build_number)
    if args.notarize:
        notarize(app, args.output.resolve(), config)
    print(app)


if __name__ == "__main__":
    main()
