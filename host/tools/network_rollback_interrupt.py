"""Interrupt an approved native rollback in a disposable GUI VirtualMac.

Run this observer as root, then request rollback through the actual GUI admin
prompt. It never launches the installer or resumes the transaction: recovery
must receive a separate supported administrator approval.
"""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

import psutil


STORE = Path("/Library/PrivilegedHelperTools/.jstack-network")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transaction", required=True)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    assert os.geteuid() == 0
    assert subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.model"], text=True).startswith("VirtualMac")
    assert re.fullmatch(r"[a-f0-9]{32}", args.transaction)
    assert args.receipt.is_absolute() and not args.receipt.exists()
    journal = STORE / "transactions" / args.transaction / "journal.json"
    original = json.loads(journal.read_text())
    assert original["state"] == "active" and original["previousApp"]
    args.receipt.write_text(json.dumps({"passed": False, "transaction": args.transaction}) + "\n")
    args.receipt.chmod(0o600)
    deadline = time.monotonic() + 120
    paused = None
    tracked = None
    killed = False
    try:
        while time.monotonic() < deadline and paused is None:
            state = json.loads(journal.read_text())
            assert state["state"] != "rolled_back", "rollback completed before interruption"
            candidates = [tracked] if tracked is not None else psutil.process_iter(["pid", "name"])
            for process in candidates:
                if tracked is None and process.info["name"] != "Installer":
                    continue
                try:
                    argv = process.cmdline()
                    if len(argv) != 2:
                        continue
                    executable, request = map(Path, argv)
                    if (executable.name != "Installer" or executable.parent.parent != STORE / "invocations"
                            or request != executable.parent / "request.json"):
                        continue
                    approved = json.loads(request.read_text())
                    if approved != {"schema": 1, "action": "rollback", "transaction": args.transaction}:
                        continue
                    if tracked is None:
                        subprocess.run(["/usr/bin/codesign", "--verify", "--strict", "-R",
                                        '=anchor apple generic and certificate leaf[subject.OU] = "MZ95H77RQQ" and identifier "JStackNetworkInstaller"',
                                        str(executable)], check=True, capture_output=True)
                        tracked = process
                    state = json.loads(journal.read_text())
                    if state.get("recoveryPhase") != "stopping":
                        continue
                    for child in process.children(recursive=True):
                        command = child.cmdline()
                        # launchctl asuser can exec sudo and then the native
                        # controller before the observer samples the child.
                        allowed = {"/bin/launchctl", "/usr/bin/sudo",
                                   "/Library/PrivilegedHelperTools/jStack Network.app/Contents/MacOS/JStackHub"}
                        if child.exe() in allowed and command[-2:] == ["unregister", "network"]:
                            process.send_signal(signal.SIGSTOP)
                            paused = process
                            break
                    if paused is not None:
                        break
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            time.sleep(0.001)
        assert paused is not None, "did not observe native unregister; no interruption performed"
        while time.monotonic() < deadline:
            observation = subprocess.run(["/bin/launchctl", "print", "system/live.jstack.network"],
                                         capture_output=True, text=True)
            if observation.returncode and "Could not find service" in observation.stdout + observation.stderr:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("native unregister did not complete")
        state = json.loads(journal.read_text())
        assert state["state"] == "rolling_back" and state["recoveryPhase"] == "stopping"
        paused.send_signal(signal.SIGKILL)
        paused.wait(timeout=10)
        killed = True
        result = {"passed": True, "transaction": args.transaction,
                  "killed_after_native_unregister": True, "durable_phase": "stopping",
                  "candidate_seal": original["request"]["candidateSeal"],
                  "recovery_still_required": True}
        args.receipt.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
    finally:
        if paused is not None and not killed:
            try:
                paused.send_signal(signal.SIGCONT)
            except psutil.NoSuchProcess:
                pass


if __name__ == "__main__":
    main()
