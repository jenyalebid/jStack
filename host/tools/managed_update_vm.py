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
    parser.add_argument("action", choices=["hub", "offer", "queue", "inventory", "enrol",
                                           "adopt", "grant", "spawn", "probe", "call", "revoke", "session-proof",
                                           "tamper", "restore-artifact"])
    parser.add_argument("--session")
    parser.add_argument("--after", type=int, default=0)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--request", default="fixture-self-update")
    parser.add_argument("--target", default="self")
    parser.add_argument("--record", type=Path)
    parser.add_argument("--machine")
    parser.add_argument("--port", type=int)
    parser.add_argument("--parent-address")
    parser.add_argument("--parent-port", type=int)
    parser.add_argument("--agent")
    parser.add_argument("--engine", help="pin the session engine; the agent's "
                        "host-side default decides when this is absent")
    parser.add_argument("--path", help="local API path for call, e.g. /updates/queue")
    parser.add_argument("--body", help="JSON body; its absence makes call a GET")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    state = Path.home() / ".local/state/jremote"
    root = state / "updates"
    config = json.loads((root / "config.json").read_text())
    account = pwd.getpwuid(os.getuid())
    if (account.pw_name != "admin" or Path.home() != Path(account.pw_dir)
            or not config.get("candidate_test")):
        raise RuntimeError("operation requires the disposable candidate-test VM fixture")
    # Every action that changes a fixture also needs the adoption record that
    # proves this guest was provisioned as one. `probe` only reads, and a guest
    # that was just installed from a candidate has no adoption record yet —
    # which is exactly the machine a fresh-install journey has to observe.
    if args.action != "probe" and not (Path.home() / "update-lab-adoption.json").is_file():
        raise RuntimeError("operation requires the disposable candidate-test VM fixture")
    for path in reversed(config["runtime_imports"]):
        if not Path(path).resolve().is_relative_to(root):
            raise RuntimeError("runtime is outside the fixture")
        sys.path.insert(0, path)
    from jstack_host import fleet_updates, release_manifest, devices, grants, hostenv
    from jstack_host.store import get_store
    from jstack_host.update_supervisor import atomic_json
    import httpx
    if args.action in {"tamper", "restore-artifact"}:
        if config.get("managed") or (state / "parent.json").exists():
            raise RuntimeError("artifact fault requires the disposable fixture hub")
        feed = fleet_updates.feed_dir()
        envelope = json.loads((feed / "latest.json").read_text())
        manifest = release_manifest.verify(envelope, config["public_key"], promoted=False)
        artifact = feed / manifest["release"] / manifest["components"]["client"]["file"]
        backup = artifact.with_name(artifact.name + ".lab-original")
        if args.action == "tamper":
            if backup.exists():
                raise RuntimeError("artifact already withheld; restore it first")
            release_manifest.check_artifact(artifact, manifest["components"]["client"])
            shutil.copy2(artifact, backup)
            with artifact.open("ab") as stream:
                stream.write(b"lab artifact corruption")
        else:
            release_manifest.check_artifact(backup, manifest["components"]["client"])
            os.replace(backup, artifact)
        print(json.dumps({"fixture_action": args.action, "release": manifest["release"]}))
        return
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
    if args.action == "probe":
        # The product's own observation code, run on the guest: installed and
        # running component versions, the host's live source answer, plugin
        # versions and this updater's loaded source. A file on disk is not an
        # observation of what is running, and neither is a checkout's HEAD.
        from jstack_host.update_macos import MacBackend
        journal = root / "job.json"
        job = json.loads(journal.read_text()) if journal.exists() else {}
        print(json.dumps({"host_id": hostenv.host_id(),
                          "observed": MacBackend(root, config).observe(job),
                          "job": {key: job.get(key) for key in ("id", "state", "detail", "release")},
                          "adopted": (state / "parent.json").exists(),
                          "managed": bool(config.get("managed"))}, indent=2))
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
    if args.action == "session-proof":
        import psutil
        import re
        from jstack_host import board
        if not re.fullmatch(r"[a-f0-9-]{32,40}", args.session or ""):
            parser.error("session-proof requires a session identity")
        result = httpx.get(config["local_url"] + "/api/jremote/v1/sessions/" + args.session,
                           headers=headers, timeout=args.timeout)
        result.raise_for_status()
        payload = result.json()
        if payload.get("error"):
            raise RuntimeError(payload["error"])
        entries = payload.get("messages", [])
        holders = [{"pid": pid, "started": psutil.Process(pid).create_time()}
                   for pid in board.pids_holding(args.session)]
        print(json.dumps({"session": args.session, "holders": holders,
                          "cursor": len(entries), "messages": entries[args.after:]}))
        return
    if args.action == "call":
        # One authenticated local request, answered with its status instead of
        # an exception: a refusal is an observation the caller needs to see.
        if not args.path or not args.path.startswith("/"):
            parser.error("call requires a local API path")
        body = json.loads(args.body) if args.body else None
        answer = httpx.request("POST" if body is not None else "GET",
                               config["local_url"] + "/api/jremote/v1" + args.path,
                               headers=headers, json=body, timeout=args.timeout)
        print(json.dumps({"status": answer.status_code, "body": answer.text[:4000]}))
        return
    if args.action == "revoke":
        row = get_store().host_row(args.machine or "")
        if row is None or not str(row["name"]).startswith("Update lab"):
            raise RuntimeError("only a machine this fixture enrolled may be revoked")
        # This lab has HTTP relays, not a WireGuard mesh; the native device
        # management route correctly refuses to call it a mesh-owning hub.
        # Revoke the fixture credential through the shipped device store and
        # test the updater's response. This is NOT menu/mesh acceptance.
        if not devices.revoke(row["device_id"]):
            raise RuntimeError("fixture credential is missing or already revoked")
        print(json.dumps({"revoked": row["device_id"], "machine": args.machine,
                          "method": "local fixture credential revocation"}))
        return
    if args.action == "spawn":
        if not args.agent or not args.agent.startswith("update-proof"):
            parser.error("spawn is limited to the update-proof fixture agent")
        # No hard-coded engine: the host's `resolve()` is the single fallback
        # point, and it picks the agent's default — the CLI this disposable
        # guest actually has installed. Pinning codex here spawned sessions on
        # guests that never carried a codex binary.
        body = {"agent_id": args.agent,
                "text": "Use the shell tool to run pwd once, then report the path. "
                        "Do not modify files or make network requests."}
        if args.engine:
            body["engine"] = args.engine
        result = httpx.post(config["local_url"] + "/api/jremote/v1/sessions/open-new",
                            headers=headers, json=body, timeout=60)
    elif args.action == "queue":
        result = httpx.post(base + "/queue", headers=headers,
                            json={"target": args.target, "request_id": args.request}, timeout=20)
    else:
        result = httpx.get(base + "/inventory", headers=headers, timeout=20)
    result.raise_for_status()
    print(json.dumps(result.json(), indent=2))


if __name__ == "__main__":
    main()
