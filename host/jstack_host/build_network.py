"""Build the restricted native privileged helper; never install it here."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import tempfile

from .build_hub import notarize, sign
from .bundle_tools import bundle
from .update_macos import command

WG_COMMIT = "49ce333da02056ae7b22ee2aeb6afe8aaed79b19"
GO_COMMIT = "f333402bd9cbe0f3eeb02507bd14e23d7d639280"


def service_plist():
    return {"Label": "live.jstack.network", "BundleProgram": "Contents/MacOS/JStackNetwork",
            "ProgramArguments": ["JStackNetwork"], "RunAtLoad": True, "KeepAlive": True,
            "ThrottleInterval": 30, "ExitTimeOut": 15,
            "AssociatedBundleIdentifiers": ["live.jstack.network"],
            "StandardOutPath": "/var/log/live.jstack.network.log",
            "StandardErrorPath": "/var/log/live.jstack.network.log"}


def build(stack: Path, source: Path, output: Path, version: str, config: dict | None, *, go_source: Path | None = None):
    command(["git", "-C", str(stack), "diff", "--quiet", "HEAD", "--", "host"])
    source_sha = command(["git", "-C", str(stack), "rev-parse", "HEAD"]).strip()
    if command(["git", "-C", str(source), "rev-parse", "HEAD"]).strip() != WG_COMMIT:
        raise ValueError("unexpected WireGuard tools source revision")
    command(["git", "-C", str(source), "diff", "--quiet", "HEAD"])
    if go_source is None or command(["git", "-C", str(go_source), "rev-parse", "HEAD"]).strip() != GO_COMMIT:
        raise ValueError("unexpected WireGuard Go source revision")
    command(["git", "-C", str(go_source), "diff", "--quiet", "HEAD"])
    app = output / "jStack Network.app"
    app.mkdir(parents=True, exist_ok=False)
    contents = app / "Contents"
    macos, resources = contents / "MacOS", contents / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir()
    for name, filename, flags in (("JStackHub", "ServiceControl.swift", []),
                                   ("JStackNetworkInstaller", "NetworkInstall.swift", ["-parse-as-library"]),
                                   ("JStackNetwork", "Network.swift", ["-parse-as-library"])):
        with tempfile.TemporaryDirectory(prefix="jstack-native-") as temporary:
            native = Path(temporary) / filename
            native.write_text(command(["git", "-C", str(stack), "show", f"{source_sha}:host/macos/{filename}"]))
            sources = [str(native)]
            if filename in {"Network.swift", "NetworkInstall.swift"}:
                protection = Path(temporary) / "ProtectedPaths.swift"
                protection.write_text(command(["git", "-C", str(stack), "show", f"{source_sha}:host/macos/ProtectedPaths.swift"]))
                sources.append(str(protection))
            if filename == "Network.swift":
                commands = Path(temporary) / "NetworkCommand.swift"
                commands.write_text(command(["git", "-C", str(stack), "show", f"{source_sha}:host/macos/NetworkCommand.swift"]))
                sources.append(str(commands))
            command(["xcrun", "swiftc", *flags, "-O", "-o", str(macos / name), *sources], timeout=180)
    # Distribute the exact corresponding GPL source, not an external URL
    # that can disappear. Build only files emitted by git archive.
    archive = resources / "wireguard-tools-source.tar"
    command(["git", "-C", str(source), "archive", "--format=tar", "-o", str(archive), WG_COMMIT])
    with tempfile.TemporaryDirectory(prefix="jstack-wireguard-") as temporary:
        root = Path(temporary)
        command(["/usr/bin/tar", "-xf", str(archive), "-C", str(root)])
        command(["/usr/bin/make", "-C", str(root / "src"), "-j4", "wg"], timeout=180)
        bundle(root / "src/wg", macos / "wg", contents / "Frameworks", resources / "Licenses")
    go = shutil.which("go")
    if not go:
        raise ValueError("Go compiler is required as a build input")
    go_archive = resources / "wireguard-go-source.tar"
    command(["git", "-C", str(go_source), "archive", "--format=tar", "-o", str(go_archive), GO_COMMIT])
    with tempfile.TemporaryDirectory(prefix="jstack-wireguard-go-") as temporary:
        root = Path(temporary)
        command(["/usr/bin/tar", "-xf", str(go_archive), "-C", str(root)])
        command([go, "build", "-trimpath", "-mod=readonly", "-o", str(root / "wireguard-go"), "."],
                cwd=root, env={**os.environ, "CGO_ENABLED": "0", "GOWORK": "off"}, timeout=300)
        command([go, "mod", "verify"], cwd=root, timeout=120)
        bundle(root / "wireguard-go", macos / "wireguard-go", contents / "Frameworks", resources / "Licenses")
    definitions = contents / "Library/LaunchDaemons"
    definitions.mkdir(parents=True)
    (definitions / "live.jstack.network.plist").write_bytes(plistlib.dumps(service_plist()))
    (resources / "services.json").write_text(json.dumps({"network": "live.jstack.network.plist"}) + "\n")
    (resources / "build-inputs.json").write_text(json.dumps({"wireguard_tools": WG_COMMIT, "wireguard_go": GO_COMMIT,
        "go_compiler": command([go, "version"]).strip(),
        "source": source_sha}) + "\n")
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
    parser.add_argument("--wireguard-go-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--signing-config", type=Path)
    parser.add_argument("--notarize", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.signing_config.read_text()) if args.signing_config else None
    if args.notarize and not config:
        parser.error("--notarize requires --signing-config")
    app = build(args.stack.resolve(), args.wireguard_source.resolve(), args.output.resolve(), args.version, config,
                go_source=args.wireguard_go_source.resolve())
    if args.notarize:
        notarize(app, args.output.resolve(), config)
    print(app)


if __name__ == "__main__":
    main()
