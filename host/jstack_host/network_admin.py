"""One-shot, approved bootstrap of the signed Network transaction installer.

The administrator shell runs only fixed system tools and a protected copy of
the verified native installer. It never executes user-writable Python as root.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
from pathlib import Path
import shlex
import stat
import subprocess
import tempfile
import uuid

from . import app_services
from .update_macos import command
from .update_supervisor import atomic_json

ROOT = Path("/Library/PrivilegedHelperTools/.jstack-network")
INSTALLER_ID = "JStackNetworkInstaller"


def validate_request(request: dict) -> None:
    """Reject malformed plans before displaying an administrator prompt."""
    if not isinstance(request, dict) or request.get("schema") != 1:
        raise ValueError("unsupported Network request schema")
    if not re.fullmatch(r"[a-f0-9]{32}", str(request.get("transaction", ""))):
        raise ValueError("invalid Network transaction identity")
    action = request.get("action")
    common = {"schema", "action", "transaction"}
    if action in {"activate", "rollback", "uninstall"}:
        if set(request) != common:
            raise ValueError("recovery must use the protected staged request")
        return
    if action != "stage" or set(request) != common | {"candidate", "candidateSeal", "candidateBinary", "policy", "legacy"}:
        raise ValueError("invalid Network staging request")
    if not isinstance(request["candidate"], str) or not Path(request["candidate"]).is_absolute():
        raise ValueError("candidate must be absolute")
    for key in ("candidateSeal", "candidateBinary"):
        if not re.fullmatch(r"[a-f0-9]{64}", str(request[key])):
            raise ValueError("candidate requires exact signature and executable hashes")
    policy = request["policy"]
    if not isinstance(policy, dict) or set(policy) != {"owner", "configuration", "address", "subnet", "nameFile", "forwarding", "active"}:
        raise ValueError("invalid Network policy fields")
    if type(policy["owner"]) is not int or policy["owner"] <= 0:
        raise ValueError("Network policy requires a non-root owner")
    if any(type(policy[key]) is not bool for key in ("forwarding", "active")):
        raise ValueError("Network policy choices must be boolean")
    address = ipaddress.IPv4Interface(policy["address"])
    subnet = ipaddress.IPv4Network(policy["subnet"], strict=True)
    if address.network != subnet or subnet.prefixlen == 0:
        raise ValueError("Network address and subnet differ")
    if not Path(policy["configuration"]).is_absolute() or not re.fullmatch(r"/var/run/wireguard/[a-zA-Z0-9.-]+", policy["nameFile"]):
        raise ValueError("invalid Network data paths")
    if not isinstance(request["legacy"], list):
        raise ValueError("legacy review must be an explicit list")
    labels = set()
    for job in request["legacy"]:
        if not isinstance(job, dict) or set(job) != {"label", "sha256", "loaded", "disabled", "sources"}:
            raise ValueError("invalid legacy job review")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", job["label"]) or job["label"] == "live.jstack.network" or job["label"] in labels:
            raise ValueError("invalid or repeated legacy label")
        labels.add(job["label"])
        if not re.fullmatch(r"[a-f0-9]{64}", job["sha256"]) or any(type(job[key]) is not bool for key in ("loaded", "disabled")):
            raise ValueError("invalid legacy definition or lifecycle")
        if not isinstance(job["sources"], list) or not job["sources"]:
            raise ValueError("legacy executable provenance is required")
        for source in job["sources"]:
            if not isinstance(source, dict) or set(source) != {"path", "sha256", "owner"}:
                raise ValueError("invalid legacy executable provenance")
            if not Path(source["path"]).is_absolute() or not re.fullmatch(r"[a-f0-9]{64}", source["sha256"]) or type(source["owner"]) is not int or source["owner"] < 0:
                raise ValueError("invalid legacy executable pin")
        if policy["active"] and (not job["loaded"] or job["disabled"]):
            raise ValueError("migration cannot enable an OFF legacy network")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def protected_ancestry(path: Path):
    for item in (path, *path.parents):
        if not item.exists() and not item.is_symlink():
            continue
        info = item.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
            raise ValueError("unprotected administrator staging ancestry")


def bootstrap_script(app: Path, request: Path, invocation: str) -> str:
    if uuid.UUID(invocation).hex != invocation:
        raise ValueError("invalid administrator invocation identifier")
    protected_ancestry(ROOT / "invocations")
    app_services.verify(app, "live.jstack.network")
    command(["/usr/sbin/spctl", "--assess", "--type", "execute", str(app)])
    installer = app / "Contents/MacOS/JStackNetworkInstaller"
    requirement = '=anchor apple generic and certificate leaf[subject.OU] = "MZ95H77RQQ" and identifier "' + INSTALLER_ID + '"'
    command(["/usr/bin/codesign", "--verify", "--strict", "-R", requirement, str(installer)])
    target = ROOT / "invocations" / invocation
    executable, copied_request = target / "Installer", target / "request.json"
    q = lambda value: shlex.quote(str(value))
    # Recheck exact hashes AFTER copying beneath protected root ancestry and
    # before execution. The source may change while the OS approval is open.
    lines = ["set -eu", "umask 077",
             "/usr/bin/install -d -o root -g wheel -m 700 " + q(ROOT / "invocations"),
             "/bin/mkdir -m 700 " + q(target),
             "/usr/bin/install -o root -g wheel -m 755 " + q(installer) + " " + q(executable),
             "/usr/bin/install -o root -g wheel -m 600 " + q(request) + " " + q(copied_request)]
    for source, destination in ((installer, executable), (request, copied_request)):
        lines.append('test "$(/usr/bin/shasum -a 256 ' + q(destination) +
                     ' | /usr/bin/cut -d " " -f 1)" = ' + q(digest(source)))
    lines += ["/usr/bin/codesign --verify --strict -R " + q(requirement) + " " + q(executable),
              q(executable) + " " + q(copied_request)]
    return "\n".join(lines)


def approve(app: Path, request: dict, private_storage: Path) -> dict:
    if os.geteuid() == 0:
        raise PermissionError("request OS approval as the logged-in user")
    validate_request(request)
    if not private_storage.is_absolute():
        raise ValueError("private request storage must be absolute")
    private_storage.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = private_storage.lstat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077 or stat.S_ISLNK(info.st_mode):
        raise ValueError("unsafe private request storage")
    folder = Path(tempfile.mkdtemp(prefix="network-request-", dir=private_storage))
    path = folder / "request.json"
    atomic_json(path, request)
    script = bootstrap_script(app, path, uuid.uuid4().hex)
    # AppleScript string escaping is separate from shell argument quoting.
    literal = '"' + script.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    source = "do shell script " + literal + ' with administrator privileges with prompt "Install or recover the reviewed jStack Network service"'
    result = subprocess.run(["/usr/bin/osascript", "-"], input=source, capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise ValueError("administrator approval or protected Network transaction failed: " + result.stderr[-1000:])
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--private-storage", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(approve(args.app, json.loads(args.request.read_text()), args.private_storage)))


if __name__ == "__main__":
    main()
