"""Exercise the approved Network installer only in a disposable GUI Mac VM."""
import argparse
import hashlib
import json
import os
import plistlib
import re
import shlex
from pathlib import Path
import subprocess
import uuid

from jstack_host.network_admin import approve
from jstack_host.update_supervisor import atomic_json


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "prepare-legacy", "legacy", "stage", "activate", "rollback", "uninstall", "verify", "verify-legacy"))
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--private-storage", required=True, type=Path)
    parser.add_argument("--case", default="initial")
    args = parser.parse_args()
    assert re.fullmatch(r"[a-zA-Z0-9-]+", args.case), "invalid fixture case"
    assert run("/usr/sbin/sysctl", "-n", "hw.model").startswith("VirtualMac"), "VM only"
    assert os.getuid() != 0, "use the actual administrator prompt"
    assert args.private_storage.is_absolute(), "private storage must be explicit and absolute"
    root = args.private_storage / args.case
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    request_path = root / "stage.json"
    installed = Path("/Library/PrivilegedHelperTools/jStack Network.app")
    label = "live.jstack.network.acceptance.legacy"
    original = Path("/Library/LaunchDaemons") / (label + ".plist")
    if args.phase in {"prepare", "prepare-legacy"}:
        assert not request_path.exists() and not installed.exists(), "fresh fixture already exists"
        configuration = root / "network.conf"
        assert not configuration.exists()
        private = run(str(args.app / "Contents/MacOS/wg"), "genkey")
        with configuration.open("x") as stream:
            configuration.chmod(0o600)
            stream.write(f"[Interface]\nPrivateKey = {private}\nListenPort = 51987\n")
        request = {"schema": 1, "action": "stage", "transaction": uuid.uuid4().hex,
                   "candidate": str(args.app),
                   "candidateSeal": digest(args.app / "Contents/_CodeSignature/CodeResources"),
                   "candidateBinary": digest(args.app / "Contents/MacOS/JStackHub"),
                   "policy": {"owner": os.getuid(), "configuration": str(configuration),
                              "address": "10.199.76.1/24", "subnet": "10.199.76.0/24",
                              "nameFile": "/var/run/wireguard/jstack-installer-lab.name",
                              "forwarding": False, "active": True}, "legacy": []}
        if args.phase == "prepare-legacy":
            assert not original.exists(), "legacy fixture already installed"
            wrapper = root / "legacy-network.sh"
            go = args.app / "Contents/MacOS/wireguard-go"
            wg = args.app / "Contents/MacOS/wg"
            name = request["policy"]["nameFile"]
            wrapper.write_text("\n".join([
                "#!/bin/bash", "set -eu", "umask 077",
                "mkdir -p /var/run/wireguard",
                "WG_TUN_NAME_FILE=" + shlex.quote(name) + " " + shlex.quote(str(go)) + " -f utun &",
                "child=$!", "trap 'kill \"$child\" 2>/dev/null || true' EXIT",
                "for attempt in {1..100}; do test ! -s " + shlex.quote(name) + " || break; sleep 0.1; done",
                "interface=$(cat " + shlex.quote(name) + ")",
                shlex.quote(str(wg)) + " setconf \"$interface\" " + shlex.quote(str(configuration)),
                '/sbin/ifconfig "$interface" inet 10.199.76.1 10.199.76.1 netmask 255.255.255.0',
                '/sbin/ifconfig "$interface" up', 'wait "$child"', ""]))
            wrapper.chmod(0o700)
            plist = root / "legacy.plist"
            plist.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": ["/bin/bash", str(wrapper)],
                "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
                "StandardOutPath": "/var/log/jstack-network-legacy-lab.log",
                "StandardErrorPath": "/var/log/jstack-network-legacy-lab.log"}))
            plist.chmod(0o600)
            request["legacy"] = [{"label": label, "sha256": digest(plist), "loaded": True, "disabled": False,
                "sources": [{"path": str(item), "sha256": digest(item), "owner": item.stat().st_uid}
                            for item in (Path("/bin/bash"), wrapper, go, wg)]}]
        atomic_json(request_path, request)
        print(json.dumps({"prepared": True, "transaction": request["transaction"]}))
        return
    request = json.loads(request_path.read_text())
    receipts = Path.home() / "network-installer-receipts" / args.case
    receipts.mkdir(parents=True, exist_ok=True)
    receipt = receipts / (args.phase + ".json")
    atomic_json(receipt, {"phase": args.phase, "passed": False, "transaction": request["transaction"]})
    if args.phase == "legacy":
        assert request["legacy"] and not original.exists(), "refusing to replace a legacy definition"
        # This deliberately creates the old user-writable-code fixture. The
        # product installer never uses this legacy bootstrap path.
        script = "set -eu\n/usr/bin/install -o root -g wheel -m 644 " + shlex.quote(str(root / "legacy.plist")) + " " + shlex.quote(str(original))
        script += "\n/bin/launchctl bootstrap system " + shlex.quote(str(original))
        literal = '"' + script.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
        run("/usr/bin/osascript", "-e", "do shell script " + literal + ' with administrator privileges with prompt "Create the disposable legacy Network test fixture"')
        result = {"legacy_created": True, "transaction": request["transaction"]}
    elif args.phase in {"verify", "verify-legacy"}:
        interfaces = re.split(r"(?m)^(?=\S)", run("/sbin/ifconfig", "-a"))
        matches = [item for item in interfaces if "inet 10.199.76.1 " in item]
        assert len(matches) == 1, "expected one active fixture interface"
        name = matches[0].split(":", 1)[0]
        assert re.fullmatch(r"utun[0-9]+", name)
        if args.phase == "verify":
            observation = json.loads(run(str(installed / "Contents/MacOS/JStackHub"), "status"))
            assert observation["network"] == "enabled"
            run("/usr/bin/codesign", "--verify", "--deep", "--strict", "-R",
                '=anchor apple generic and certificate leaf[subject.OU] = "MZ95H77RQQ" and identifier "live.jstack.network"', str(installed))
            assert digest(installed / "Contents/_CodeSignature/CodeResources") == request["candidateSeal"]
            assert digest(installed / "Contents/MacOS/JStackHub") == request["candidateBinary"]
            assert not request["legacy"] or not original.exists(), "legacy persistence remains installed"
            result = {"active": True, "signature_identity": True, "transaction": request["transaction"]}
        else:
            assert request["legacy"] and digest(original) == request["legacy"][0]["sha256"]
            run("/bin/launchctl", "print", "system/" + label)
            absent = subprocess.run(["/bin/launchctl", "print", "system/live.jstack.network"], capture_output=True, text=True)
            assert absent.returncode != 0 and "Could not find service" in absent.stdout + absent.stderr
            result = {"legacy_active": True, "exact_definition": True, "transaction": request["transaction"]}
    else:
        if args.phase != "stage":
            request = {"schema": 1, "action": args.phase, "transaction": request["transaction"]}
        result = approve(args.app, request, root)
    result["passed"] = True
    atomic_json(receipt, result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
