"""Exercise the approved Network installer only in a disposable GUI Mac VM."""
import argparse
import hashlib
import json
import os
import re
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
    parser.add_argument("phase", choices=("prepare", "stage", "activate", "rollback", "uninstall", "verify"))
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
    if args.phase == "prepare":
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
        atomic_json(request_path, request)
        print(json.dumps({"prepared": True, "transaction": request["transaction"]}))
        return
    request = json.loads(request_path.read_text())
    if args.phase == "verify":
        interfaces = re.split(r"(?m)^(?=\S)", run("/sbin/ifconfig", "-a"))
        matches = [item for item in interfaces if "inet 10.199.76.1 " in item]
        assert len(matches) == 1, "expected one active fixture interface"
        name = matches[0].split(":", 1)[0]
        assert re.fullmatch(r"utun[0-9]+", name)
        observation = json.loads(run(str(installed / "Contents/MacOS/JStackHub"), "status"))
        assert observation["network"] == "enabled"
        assert digest(installed / "Contents/_CodeSignature/CodeResources") == request["candidateSeal"]
        result = {"active": True, "signature_identity": True, "transaction": request["transaction"]}
    else:
        if args.phase != "stage":
            request = {"schema": 1, "action": args.phase, "transaction": request["transaction"]}
        result = approve(args.app, request, root)
    receipts = Path.home() / "network-installer-receipts" / args.case
    receipts.mkdir(parents=True, exist_ok=True)
    atomic_json(receipts / (args.phase + ".json"), result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
