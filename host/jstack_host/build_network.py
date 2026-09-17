"""Build the restricted native privileged helper; never install it here."""
import argparse
import json
from pathlib import Path
import plistlib
import shutil
import tempfile

from .build_hub import notarize, sign
from .bundle_tools import bundle
from .update_macos import command

WG_COMMIT = "49ce333da02056ae7b22ee2aeb6afe8aaed79b19"


def service_plist():
    return {"Label": "live.jstack.network", "BundleProgram": "Contents/MacOS/JStackNetwork",
            "ProgramArguments": ["JStackNetwork"], "RunAtLoad": True, "KeepAlive": True,
            "ThrottleInterval": 30, "AssociatedBundleIdentifiers": ["live.jstack.network"],
            "StandardOutPath": "/var/log/live.jstack.network.log",
            "StandardErrorPath": "/var/log/live.jstack.network.log"}


def build(stack: Path, source: Path, output: Path, version: str, config: dict | None):
    if command(["git", "-C", str(source), "rev-parse", "HEAD"]).strip() != WG_COMMIT:
        raise ValueError("unexpected WireGuard tools source revision")
    command(["git", "-C", str(source), "diff", "--quiet", "HEAD"])
    app = output / "jStack Network.app"
    app.mkdir(parents=True, exist_ok=False)
    contents = app / "Contents"
    macos, resources = contents / "MacOS", contents / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir()
    for name, filename, flags in (("JStackHub", "ServiceControl.swift", []),
                                   ("JStackNetwork", "Network.swift", ["-parse-as-library"])):
        command(["xcrun", "swiftc", *flags, "-O", "-o", str(macos / name),
                 str(stack / "host/macos" / filename)], timeout=180)
    # Distribute the exact corresponding GPL source, not an external URL
    # that can disappear. Build only files emitted by git archive.
    archive = resources / "wireguard-tools-source.tar"
    command(["git", "-C", str(source), "archive", "--format=tar", "-o", str(archive), WG_COMMIT])
    with tempfile.TemporaryDirectory(prefix="jstack-wireguard-") as temporary:
        root = Path(temporary)
        command(["/usr/bin/tar", "-xf", str(archive), "-C", str(root)])
        command(["/usr/bin/make", "-C", str(root / "src"), "-j4", "wg"], timeout=180)
        bundle(root / "src/wg", macos / "wg", contents / "Frameworks", resources / "Licenses")
    go = shutil.which("wireguard-go")
    if not go:
        raise ValueError("wireguard-go is required as a build input")
    bundle(Path(go), macos / "wireguard-go", contents / "Frameworks", resources / "Licenses")
    definitions = contents / "Library/LaunchDaemons"
    definitions.mkdir(parents=True)
    (definitions / "live.jstack.network.plist").write_bytes(plistlib.dumps(service_plist()))
    (resources / "services.json").write_text(json.dumps({"network": "live.jstack.network.plist"}) + "\n")
    (resources / "build-inputs.json").write_text(json.dumps({"wireguard_tools": WG_COMMIT,
        "source": command(["git", "-C", str(stack), "rev-parse", "HEAD"]).strip()}) + "\n")
    (contents / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "live.jstack.network", "CFBundleExecutable": "JStackHub",
        "CFBundleName": "jStack Network", "CFBundleDisplayName": "jStack Network",
        "CFBundlePackageType": "APPL", "CFBundleVersion": version,
        "CFBundleShortVersionString": version, "LSUIElement": True, "LSMinimumSystemVersion": "13.0"}))
    sign(app, config)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", type=Path, required=True)
    parser.add_argument("--wireguard-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--signing-config", type=Path)
    parser.add_argument("--notarize", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.signing_config.read_text()) if args.signing_config else None
    if args.notarize and not config:
        parser.error("--notarize requires --signing-config")
    app = build(args.stack.resolve(), args.wireguard_source.resolve(), args.output.resolve(), args.version, config)
    if args.notarize:
        notarize(app, args.output.resolve(), config)
    print(app)


if __name__ == "__main__":
    main()
