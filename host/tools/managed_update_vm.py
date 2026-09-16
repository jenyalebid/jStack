"""Operate a candidate-only disposable Mac as a real self-updating hub.

This changes only an explicitly provisioned update-lab fixture. Enrollment and
network topology are not covered. No production acceptance receipt is emitted.
"""
import argparse
import json
import os
import pwd
from pathlib import Path
import shutil
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["hub", "offer", "queue", "inventory", "enrol", "adopt", "grant", "spawn"])
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--request", default="fixture-self-update")
    parser.add_argument("--target", default="self")
    parser.add_argument("--record", type=Path)
    parser.add_argument("--machine")
    parser.add_argument("--port", type=int)
    parser.add_argument("--parent-address")
    parser.add_argument("--parent-port", type=int)
    parser.add_argument("--agent")
    args = parser.parse_args()
    state = Path.home() / ".local/state/jremote"
    root = state / "updates"
    config = json.loads((root / "config.json").read_text())
    account = pwd.getpwuid(os.getuid())
    if (account.pw_name != "admin" or Path.home() != Path(account.pw_dir)
            or not config.get("candidate_test")
            or not (Path.home() / "update-lab-adoption.json").is_file()):
        raise RuntimeError("operation requires the disposable candidate-test VM fixture")
    for path in reversed(config["runtime_imports"]):
        if not Path(path).resolve().is_relative_to(root):
            raise RuntimeError("runtime is outside the fixture")
        sys.path.insert(0, path)
    from jstack_host import fleet_updates, release_manifest, devices, grants, hostenv
    from jstack_host.store import get_store
    from jstack_host.update_supervisor import atomic_json
    import httpx
    if args.action in {"enrol", "adopt", "grant"}:
        if args.record is None or not args.record.resolve().is_relative_to(Path.home()):
            parser.error("fixture operation requires a record file under the guest account")
        if args.action == "enrol":
            if not all([args.machine, args.port, args.parent_address, args.parent_port]):
                parser.error("enrol requires machine, relay port and parent endpoint")
            if get_store().host_row(args.machine):
                raise RuntimeError("fixture machine already enrolled; refusing credential rotation")
            row, token = devices.mint("Update lab VM peer")
            get_store().upsert_host(args.machine, "Update lab leaf", "127.0.0.1", args.port)
            get_store().bind_host_device(args.machine, row["id"])
            atomic_json(args.record, {"parent_key": hostenv.host_id(), "parent_name": "Update lab VM hub",
                                     "parent_address": args.parent_address, "parent_port": args.parent_port,
                                     "device_id": row["id"], "token": token})
        elif args.action == "adopt":
            job = json.loads((root / "job.json").read_text())
            if job.get("state") not in fleet_updates.TERMINAL:
                raise RuntimeError("cannot change fixture parent during an active update")
            record = json.loads(args.record.read_text())
            if record.get("parent_name") != "Update lab VM hub":
                raise RuntimeError("not a VM hub fixture record")
            backup = state / "parent.previous-update-lab.json"
            if backup.exists():
                raise RuntimeError("fixture parent already moved")
            (state / "parent.json").rename(backup)
            atomic_json(state / "parent.json", record)
            atomic_json(args.record.with_suffix(".grant.json"),
                        {"machine": hostenv.host_id(), "grant": grants.issue("Update lab VM hub")})
        else:
            record = json.loads(args.record.read_text())
            if not get_store().host_row(record["machine"]):
                raise RuntimeError("grant does not name an enrolled fixture")
            grants.remember(record["machine"], record["grant"])
        print(json.dumps({"fixture_action": args.action, "record": str(args.record)}))
        return
    if args.action in {"hub", "offer"}:
        if args.candidate is None:
            parser.error("hub requires --candidate")
        envelope = json.loads((args.candidate / "candidate.json").read_text())
        manifest = release_manifest.verify(envelope, config["public_key"], promoted=False)
        for component in manifest["components"].values():
            release_manifest.check_artifact(args.candidate / component["file"], component)
        if args.action == "hub":
            backup = state / "parent.previous-update-lab.json"
            if backup.exists():
                raise RuntimeError("fixture parent already moved; refusing replacement")
            (state / "parent.json").rename(backup)
            config["managed"] = False
            atomic_json(root / "config.json", config)
        elif config.get("managed") or (state / "parent.json").exists():
            raise RuntimeError("a fixture leaf cannot offer its own release")
        destination = fleet_updates.feed_dir() / manifest["release"]
        destination.mkdir(parents=True, exist_ok=True)
        for component in manifest["components"].values():
            shutil.copy2(args.candidate / component["file"], destination / component["file"])
        atomic_json(destination / "manifest.json", envelope)
        atomic_json(fleet_updates.feed_dir() / "latest.json", envelope)
        print(json.dumps({"fixture_hub": True, "offer": manifest["release"]}))
        return
    headers = {"Authorization": "Bearer " + Path(config["token_path"]).read_text().strip()}
    base = config["local_url"] + "/api/jremote/v1/updates"
    if args.action == "spawn":
        if not args.agent or not args.agent.startswith("update-proof"):
            parser.error("spawn is limited to the update-proof fixture agent")
        result = httpx.post(config["local_url"] + "/api/jremote/v1/sessions/open-new", headers=headers,
                            json={"agent_id": args.agent, "engine": "codex",
                                  "text": "Use the shell tool to run pwd once, then report the path. "
                                          "Do not modify files or make network requests."}, timeout=60)
    elif args.action == "queue":
        result = httpx.post(base + "/queue", headers=headers,
                            json={"target": args.target, "request_id": args.request}, timeout=20)
    else:
        result = httpx.get(base + "/inventory", headers=headers, timeout=20)
    result.raise_for_status()
    print(json.dumps(result.json(), indent=2))


if __name__ == "__main__":
    main()
