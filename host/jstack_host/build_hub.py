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


def build(stack: Path, output: Path, version: str, config: dict | None = None) -> Path:
    if sys.version_info[:2] != (3, 12):
        raise ValueError("this runtime build requires the audited CPython 3.12 framework")
    source = Path(sys.base_prefix)
    if not (source / "Python").is_file():
        raise ValueError("a framework Python build is required")
    app = output / "jStack Hub.app"
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
    if (stack / "host/release-identity.json").exists():
        shutil.copy2(stack / "host/release-identity.json", resources / "release-identity.json")
    command(["xcrun", "clang", "-O2", "-Wall", "-Wextra", "-Werror", "-mmacosx-version-min=13.0",
             "-I" + str(source / "include/python3.12"), str(stack / "host/macos/Runtime.c"),
             str(source / "Python"), "-o", str(macos / "JStackRuntime")])
    shutil.copy2(macos / "JStackRuntime", macos / "JStackCLI")
    for name, relative in (("JStackHub", "host/macos/ServiceControl.swift"),
                           ("JStackHostBar", "host/menubar/JStackHostBar.swift")):
        command(["xcrun", "swiftc", "-O", "-o", str(macos / name), str(stack / relative)], timeout=180)
    definitions = contents / "Library/LaunchAgents"
    definitions.mkdir(parents=True)
    for role in ROLES:
        (definitions / f"live.jstack.hub.{role}.plist").write_bytes(plistlib.dumps(service_plist(role)))
    (contents / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "live.jstack.hub", "CFBundleExecutable": "JStackHub",
        "CFBundleName": "jStack Hub", "CFBundleDisplayName": "jStack Hub",
        "CFBundlePackageType": "APPL", "CFBundleVersion": version,
        "CFBundleShortVersionString": version, "LSUIElement": True,
        "LSMinimumSystemVersion": "13.0",
        "CFBundleURLTypes": [{"CFBundleURLName": "jStack updates", "CFBundleURLSchemes": ["jstack"]}]}))
    binaries = [path for path in contents.rglob("*") if macho(path)]
    for path in binaries:
        relocate(path, source, runtime)
    # Catch escaping links too: a sealed signature must not conceal a runtime
    # dependency on this publisher's machine.
    for path in contents.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(contents.resolve()):
            raise ValueError(f"escaping runtime symlink: {path.relative_to(contents)}")
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
    command([*signing, str(framework)])
    command([*signing, str(app)])
    command(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)])
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--signing-config", type=Path)
    parser.add_argument("--notarize", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.signing_config.read_text()) if args.signing_config else None
    if args.notarize and not config:
        parser.error("--notarize requires --signing-config")
    app = build(args.stack.resolve(), args.output.resolve(), args.version, config)
    if args.notarize:
        archive = args.output.resolve() / "hub-notary.zip"
        command(["/usr/bin/ditto", "-c", "-k", "--keepParent", str(app), str(archive)])
        credentials = json.loads(Path(config["notary_credentials"]).read_text())
        result = json.loads(command(["xcrun", "notarytool", "submit", str(archive),
            "--key", str(Path(credentials["private_key_path"]).expanduser()),
            "--key-id", credentials["key_id"], "--issuer", credentials["issuer_id"],
            "--wait", "--output-format", "json"], timeout=1200))
        (args.output / "notary-result.json").write_text(json.dumps(result, indent=2) + "\n")
        if result.get("status") != "Accepted":
            raise ValueError("Hub notarization was not accepted")
        command(["xcrun", "stapler", "staple", str(app)])
        command(["/usr/sbin/spctl", "--assess", "--type", "execute", str(app)])
        command(["/usr/bin/ditto", "-c", "-k", "--keepParent", str(app),
                 str(args.output.resolve() / "hub-notarized.zip")])
    print(app)


if __name__ == "__main__":
    main()
