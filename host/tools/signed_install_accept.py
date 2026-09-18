"""Fresh signed-installer evidence, only on a disposable GUI VirtualMac.

Copy the notarized jStack Hub app to /Applications before the install phase —
the Hub is the single user-level owner (host, menu and updater are all its
roles); the root-level Network app has its own admin flow and accept tool.
Reboot the guest through its GUI before the reboot phase. Receipts contain
source identities and outcomes, never credentials or session contents.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time


def run(*args):
    result = subprocess.run(args, text=True, capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError(f"{Path(args[0]).name} failed: {result.stderr[-2000:]}")
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["install", "reboot", "off-repair", "denied-repair", "denied-reboot"])
    args = parser.parse_args()
    if not run("/usr/sbin/sysctl", "-n", "hw.model").strip().startswith("VirtualMac"):
        raise SystemExit("REFUSED: installer acceptance requires a disposable VirtualMac")
    if run("/usr/bin/stat", "-f", "%Su", "/dev/console").strip() != os.environ["USER"]:
        raise SystemExit("REFUSED: installer acceptance requires the logged-in GUI user")
    gatekeeper = run("/usr/sbin/spctl", "--status", "--verbose").splitlines()
    if set(gatekeeper) != {"assessments enabled", "developer id enabled"}:
        raise SystemExit("REFUSED: installer acceptance requires Gatekeeper and Developer ID policy enabled before installation")
    app = Path("/Applications/jStack Hub.app")
    runtime = str(app / "Contents/MacOS/JStackRuntime")
    controller = str(app / "Contents/MacOS/JStackHub")
    state = Path.home() / ".local/state/signed-host"
    settings = Path.home() / ".local/state/jremote/service-settings.json"
    receipts = Path.home() / "signed-install-receipts"
    receipts.mkdir(exist_ok=True)
    result = {"phase": args.phase, "sources": {}, "gatekeeper": gatekeeper}
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(app))
    run("/usr/sbin/spctl", "--assess", "--type", "execute", str(app))
    result["sources"][app.name] = json.loads((app / "Contents/Resources/packages/release-identity.json").read_text())
    arguments = (runtime, "install", "--app", str(app),
                 "--state-dir", str(state), "--port", "9391", "--bind", "127.0.0.1")
    if args.phase == "install":
        assert not settings.exists() and not state.exists(), "fixture is not fresh"
        result["installer"] = json.loads(run(*arguments))
        assert result["installer"]["state"] == "installed"
        run(runtime, "verify-install")
    elif args.phase == "reboot":
        boot_time = run("/usr/sbin/sysctl", "-n", "kern.boottime")
        boot_seconds = int(re.search(r"sec = (\d+)", boot_time)[1])
        assert boot_seconds > (receipts / "install.json").stat().st_mtime, "guest has not rebooted since installation"
        run(runtime, "verify-install")
        result["boot_time"] = boot_time.strip()
        result["verified_after_reboot"] = True
    elif args.phase in ("denied-repair", "denied-reboot"):
        if args.phase == "denied-reboot":
            boot_time = run("/usr/sbin/sysctl", "-n", "kern.boottime")
            assert int(re.search(r"sec = (\d+)", boot_time)[1]) > (receipts / "denied-repair.json").stat().st_mtime
            result["boot_time"] = boot_time.strip()
        assert set(json.loads(run(controller, "status")).values()) == {"requires_approval"}
        cli = str(app / "Contents/MacOS/JStackCLI")
        repair = subprocess.run([cli, "install"], text=True, capture_output=True, timeout=30)
        assert repair.returncode == 1 and "requires_approval" in repair.stdout
        assert json.loads(run(cli, "updates", "enable"))["status"] == "requires_approval"
        result["installer"] = json.loads(run(*arguments))
        assert result["installer"]["state"] == "approval_required"
        assert result["installer"]["status"] == "requires_approval"
        result["denied_approval_preserved"] = True
    else:
        for role in ("host", "menu"):
            run(controller, "unregister", role)
            deadline = time.monotonic() + 30
            label = f"gui/{os.getuid()}/live.jstack.hub.{role}"
            while subprocess.run(["/bin/launchctl", "print", label], capture_output=True).returncode == 0:
                assert time.monotonic() < deadline, "stopped service did not leave launchd"
                time.sleep(0.25)
        result["installer"] = json.loads(run(*arguments))
        assert result["installer"]["services"]["host"] == "not_registered"
        assert result["installer"]["services"]["menu"] == "not_registered"
        for role in ("host", "menu"):
            assert subprocess.run(["/bin/launchctl", "print", f"gui/{os.getuid()}/live.jstack.hub.{role}"],
                                  capture_output=True).returncode != 0
        result["off_preserved"] = True
    if args.phase != "off-repair":
        before = {name: hashlib.sha256((state / name).read_bytes()).digest()
                  for name in ("host-id", "internal-token")}
        settings_before = settings.read_bytes()
        result["repeat"] = json.loads(run(*arguments))
        assert all(hashlib.sha256((state / name).read_bytes()).digest() == value for name, value in before.items())
        assert settings.read_bytes() == settings_before
        result["identity_and_settings_preserved"] = True
    result["passed"] = True
    (receipts / (args.phase + ".json")).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"phase": args.phase, "passed": True}))


if __name__ == "__main__":
    main()
