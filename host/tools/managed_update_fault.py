"""Inject one failure in a disposable VM's candidate-test updater.

Run before queueing a NEW job. Observes the real durable journal and launchd
process; never rewrites update state or installs a fake backend. An interruption
kills only this VM's updater after a bundle moves. A rollback fault temporarily
withholds the staged client bundle while apply is paused. A freeze stops the
updater with an app copy half written and leaves it stopped, for the host to
cut the VM under it. Production is refused.
"""
import argparse
import json
import os
import pwd
from pathlib import Path
import re
import signal
import subprocess
import time

import psutil


def updater_process(config, root):
    if config.get("service_model") == "app":
        label = f"gui/{os.getuid()}/live.jstack.hub.updater"
        result = subprocess.run(["/bin/launchctl", "print", label],
                                check=True, capture_output=True, text=True, timeout=10)
        match = re.search(r"^\s*pid = (\d+)\s*$", result.stdout, re.MULTILINE)
        if not match:
            raise RuntimeError("the fixture updater service has no running process")
        process = psutil.Process(int(match.group(1)))
        if process.uids().real != os.getuid() or "updater" not in process.cmdline():
            raise RuntimeError("the fixture updater service has an unexpected process")
        return process
    candidates = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        cmd = proc.info["cmdline"] or []
        if str(root.parent) in cmd and any(
                part.endswith("update_dispatcher.py") or part == "jstack_host.update_supervisor"
                for part in cmd):
            candidates.append(proc)
    if len(candidates) != 1:
        raise RuntimeError(f"expected one fixture updater, got {len(candidates)}")
    return candidates[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fault", choices=["interruption", "rollback", "freeze"])
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    root = Path.home() / ".local/state/jremote/updates"
    config = json.loads((root / "config.json").read_text())
    account = pwd.getpwuid(os.getuid())
    if (account.pw_name != "admin" or Path.home() != Path(account.pw_dir)
            or not config.get("candidate_test")
            or not (Path.home() / "update-lab-adoption.json").is_file()):
        raise RuntimeError("fault injection requires the disposable VM fixture")
    journal = root / "job.json"
    old = json.loads(journal.read_text()).get("id") if journal.exists() else None
    deadline = time.monotonic() + args.timeout
    withheld = None
    injected = None
    print(json.dumps({"armed": args.fault, "previous_job": old}), flush=True)
    try:
        while time.monotonic() < deadline:
            job = json.loads(journal.read_text()) if journal.exists() else {}
            if job.get("id") == old:
                time.sleep(.05)
                continue
            if injected:
                if job.get("state") in {"rolled_back", "current", "failed", "cancelled"}:
                    print(json.dumps({"fault": args.fault, "job": job["id"],
                                      "state": job["state"], "detail": job.get("detail")}), flush=True)
                    if job["state"] != "rolled_back":
                        raise RuntimeError("fault did not produce rollback")
                    return
            elif job.get("state") == "applying":
                app = job["transaction"]["apps"]["menubar"]
                target = Path(app["target"])
                # The first fault waits until replacement really began. The
                # second must withhold the client before its replacement. The
                # freeze wants the hub copy started and not yet swapped in.
                if args.fault == "interruption" and not Path(app["backup"]).exists():
                    time.sleep(.01)
                    continue
                copies = [str(path) for path in target.parent.glob(target.name + ".incoming-*")
                          if path.is_dir()]
                if args.fault == "freeze" and (not copies or Path(app["backup"]).exists()):
                    time.sleep(.01)
                    continue
                updater = updater_process(config, root)
                updater.send_signal(signal.SIGSTOP)
                if args.fault == "freeze":
                    current = json.loads(journal.read_text())
                    if (current["id"] != job["id"] or current["state"] != "applying"
                            or Path(app["backup"]).exists()):
                        updater.send_signal(signal.SIGCONT)
                        raise RuntimeError("missed the copy window")
                    print(json.dumps({"injected": "freeze", "job": job["id"], "pid": updater.pid,
                                      "copies": copies}), flush=True)
                    return  # stopped on purpose: the VM is about to be cut under it
                try:
                    current = json.loads(journal.read_text())
                    if current["id"] != job["id"] or current["state"] != "applying":
                        raise RuntimeError("missed apply window")
                    if args.fault == "interruption":
                        updater.kill()
                    else:
                        source = Path(job["transaction"]["apps"]["client"]["source"])
                        if not source.resolve().is_relative_to(root / "releases"):
                            raise RuntimeError("staged bundle escaped fixture")
                        withheld = source.with_name(source.name + ".lab-withheld")
                        source.rename(withheld)
                    injected = job["id"]
                    print(json.dumps({"injected": args.fault, "job": injected,
                                      "pid": updater.pid}), flush=True)
                finally:
                    try:
                        if updater.is_running():
                            updater.send_signal(signal.SIGCONT)
                    except psutil.NoSuchProcess:
                        pass
            time.sleep(.02)
        raise TimeoutError("fault scenario timed out")
    finally:
        if withheld and withheld.exists():
            withheld.rename(withheld.with_name(withheld.name.removesuffix(".lab-withheld")))


if __name__ == "__main__":
    main()
