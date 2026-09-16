"""Disposable-Mac fixture controller; never production acceptance evidence.

Runs an isolated real hub and provisions test adoption records. Network setup
and enrollment are deliberately NOT claimed by this fixture. All update APIs,
signatures, supervisor processes, installers and recovery run unchanged.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=19090)
    parser.add_argument("--hub-address", default="192.168.64.1")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("serve")
    expose = commands.add_parser("offer")
    expose.add_argument("candidate", type=Path)
    register = commands.add_parser("register")
    register.add_argument("vm")
    register.add_argument("--vm-tool", type=Path, required=True)
    register.add_argument("--bootstrap", type=Path, required=True)
    register.add_argument("--relay-port", type=int,
                          help="pre-existing loopback SSH relay port for hub-to-VM HTTP")
    commands.add_parser("inventory")
    capture = commands.add_parser("record")
    capture.add_argument("output", type=Path)
    queue = commands.add_parser("queue")
    queue.add_argument("target")
    queue.add_argument("--request", required=True)
    revoke = commands.add_parser("revoke")
    revoke.add_argument("target")
    args = parser.parse_args()
    root = args.root.resolve()
    if "updates-lab" not in root.name or args.port == 9090:
        parser.error("use an explicitly named updates-lab directory and a non-production port")
    marker = root / "DISPOSABLE_UPDATE_LAB"
    if root.exists() and not marker.exists():
        parser.error("existing directory is not this disposable fixture")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker.touch()
    agents = root / "agents"
    agents.mkdir(exist_ok=True)
    os.environ.update(JREMOTE_HOST_PROFILE="default", JREMOTE_STATE_DIR=str(root / "hub"),
                      JREMOTE_INSTANCE_ROOT=str(agents), JREMOTE_HOST_NAME="Update lab hub",
                      JREMOTE_RELEASES_DIR=str(root / "releases/mac"),
                      JREMOTE_TMUX_SOCK="updates-lab-" + str(args.port))
    from jstack_host import devices, hostenv, fleet_updates, grants
    from jstack_host.store import get_store
    from jstack_host.update_supervisor import atomic_json
    public_path = Path(__file__).resolve().parents[1] / "jstack_host/release-trust.json"
    public = json.loads(public_path.read_text())["public_key"]
    atomic_json(root / "hub/updates/config.json", {"public_key": public, "candidate_test": True})
    token = devices.internal_token()
    if args.action == "serve":
        os.execv(sys.executable, [sys.executable, "-m", "jstack_host.server", "--host", "0.0.0.0",
                                 "--port", str(args.port)])
    elif args.action == "offer":
        from jstack_host import release_manifest
        envelope = json.loads((args.candidate / "candidate.json").read_text())
        manifest = release_manifest.verify(envelope, public, promoted=False)
        directory = fleet_updates.feed_dir() / manifest["release"]
        directory.mkdir(parents=True, exist_ok=True)
        for component in manifest["components"].values():
            release_manifest.check_artifact(args.candidate / component["file"], component)
            shutil.copy2(args.candidate / component["file"], directory / component["file"])
        atomic_json(directory / "manifest.json", envelope)
        atomic_json(fleet_updates.feed_dir() / "latest.json", envelope)
        print(json.dumps({"lab_offer": manifest["release"], "production_promoted": False}))
    elif args.action == "register":
        def vm(*argv):
            return subprocess.run([str(args.vm_tool), *argv], check=True, capture_output=True, text=True).stdout
        def ssh(command):
            return vm("ssh", args.vm, command)
        if ssh("id -un").strip() != "admin":
            raise RuntimeError("registration requires the disposable VM account")
        guest_home = Path(ssh('printf "%s" "$HOME"').strip())
        if not guest_home.is_absolute() or guest_home == Path("/"):
            raise RuntimeError("guest did not report a valid home directory")
        def guest(relative):
            return str(guest_home / relative)
        def quoted(relative):
            return shlex.quote(guest(relative))
        machine = ssh("/bin/cat " + quoted(".local/state/jremote/host-id")).strip()
        address = vm("ip", args.vm).strip()
        if get_store().host_row(machine):
            raise RuntimeError("fixture already registered; refusing to rotate a running adoption")
        row, credential = devices.mint("Update lab " + args.vm)
        get_store().upsert_host(machine, args.vm,
                               "127.0.0.1" if args.relay_port else address,
                               port=args.relay_port or 9090)
        get_store().bind_host_device(machine, row["id"])
        record = {"device_id": row["id"], "token": credential, "parent_key": hostenv.host_id(),
                  "parent_name": "Update lab hub", "parent_address": args.hub_address,
                  "parent_port": args.port, "parent_url": f"http://{args.hub_address}:{args.port}"}
        adoption = root / (args.vm + "-adoption.json")
        atomic_json(adoption, record)
        vm("cp", args.vm, str(adoption), guest("update-lab-adoption.json"))
        ssh("mkdir -p " + quoted("update-bootstrap"))
        vm("cp", args.vm, str(args.bootstrap), guest("update-bootstrap/jstack_host"))
        ssh(quoted("jStack/host/.venv/bin/python3") + " -m pip install 'cryptography>=43'")
        vm("cp", args.vm, str(Path(__file__)), guest("update-lab.py"))
        response = ssh("PYTHONPATH=" + quoted("update-bootstrap") + " "
                       + quoted("jStack/host/.venv/bin/python3") + " "
                       + quoted("update-lab.py") + " --leaf-fixture")
        grant = json.loads(response)["grant"]
        grants.remember(machine, grant)
        print(json.dumps({"machine": machine, "vm": args.vm, "address": address, "fixture_adoption": True}))
    else:
        import httpx
        base = f"http://127.0.0.1:{args.port}/api/jremote/v1/updates"
        headers = {"Authorization": "Bearer " + token}
        if args.action in {"inventory", "record"}:
            result = httpx.get(base + "/inventory", headers=headers, timeout=20)
        elif args.action == "revoke":
            row = get_store().host_row(args.target)
            if row is None or not row["name"].startswith("updates-"):
                raise RuntimeError("only a named disposable updates VM may be revoked")
            known = {json.loads(path.read_text())["device_id"]
                     for path in root.glob("*-adoption.json")}
            if row["device_id"] not in known:
                raise RuntimeError("adoption was not created by this fixture")
            result = httpx.post(f"http://127.0.0.1:{args.port}/api/jremote/v1/devices/"
                                + row["device_id"] + "/revoke", headers=headers, timeout=20)
        else:
            result = httpx.post(base + "/queue", headers=headers,
                                json={"target": args.target, "request_id": args.request}, timeout=20)
        result.raise_for_status()
        if args.action == "record":
            with fleet_updates.FleetStore().connection() as db:
                jobs = [fleet_updates.public_job(dict(row)) for row in db.execute(
                    "SELECT * FROM jobs ORDER BY created")]
            import time
            atomic_json(args.output, {"recorded_at": time.time(), "fixture_only": True,
                                     "production_acceptance": False,
                                     "inventory": result.json(), "job_history": jobs})
            print(str(args.output))
        else:
            print(json.dumps(result.json(), indent=2))


def leaf_fixture():
    account = pwd.getpwuid(os.getuid())
    if account.pw_name != "admin" or Path.home() != Path(account.pw_dir):
        raise RuntimeError("fixture bootstrap is restricted to the disposable VM account")
    from jstack_host import grants, hostenv, install_updater
    from jstack_host.update_supervisor import atomic_json
    record = json.loads((Path.home() / "update-lab-adoption.json").read_text())
    state = hostenv.state_dir()
    if (state / "parent.json").exists():
        raise RuntimeError("fixture refuses to replace an existing adoption")
    atomic_json(state / "parent.json", record)
    public = json.loads((Path(install_updater.__file__).parent / "release-trust.json").read_text())["public_key"]
    install_updater.bootstrap(public, candidate_test=True)
    print(json.dumps({"grant": grants.issue("Update lab hub")}))


if __name__ == "__main__":
    if sys.argv[1:] == ["--leaf-fixture"]:
        leaf_fixture()
    else:
        main()
