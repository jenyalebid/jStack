"""Kill a real signed rollback after its first unregister, then resume it.

Disposable GUI VirtualMac only. No product-module monkeypatches or substituted
service controller: stop the parent while its actual native controller exits.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import time

import psutil


def main():
    assert subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.model"], text=True).startswith("VirtualMac")
    assert subprocess.check_output(["/usr/bin/stat", "-f", "%Su", "/dev/console"], text=True).strip() == os.environ["USER"]
    root = Path.home() / "migration-receipts"
    journal = Path(json.loads((root / "journal-path.json").read_text())["path"])
    value = json.loads((journal / "journal.json").read_text())
    assert value["kind"] == "host" and value["state"] == "migrated"
    assert all(record["enabled"] for record in value["records"])
    runtime = str(Path(value["settings"]["app"]) / "Contents/MacOS/JStackRuntime")
    controller = str(Path(value["settings"]["app"]) / "Contents/MacOS/JStackHub")
    arguments = [runtime, "migrate", "rollback", str(journal)]
    receipt = root / "rollback-interruption.json"
    receipt.write_text(json.dumps({"passed": False, "timestamp": time.time()}))
    with (root / "rollback-interruption.log").open("w") as log:
        task = subprocess.Popen(arguments, stdout=log, stderr=subprocess.STDOUT)
        paused = False
        try:
            deadline = time.monotonic() + 30
            while task.poll() is None and time.monotonic() < deadline:
                for child in psutil.Process(task.pid).children():
                    try:
                        executable = child.exe()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                    phase = json.loads((journal / "journal.json").read_text())["state"]
                    if executable == controller and phase == "rolling_back":
                        os.kill(task.pid, signal.SIGSTOP)
                        paused = True
                        break
                if paused:
                    break
                time.sleep(0.001)
            assert paused, "rollback completed before an interruption could be observed"
            while time.monotonic() < deadline:
                if subprocess.run(["/bin/launchctl", "print", f"gui/{os.getuid()}/live.jstack.hub.menu"],
                                  capture_output=True).returncode != 0:
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("native unregister did not finish")
            interrupted = json.loads((journal / "journal.json").read_text())
            assert interrupted["state"] == "rolling_back" and interrupted["rollback_stopped"] == []
            os.kill(task.pid, signal.SIGKILL)
            assert task.wait(timeout=10) == -signal.SIGKILL
        finally:
            if paused and task.poll() is None:
                os.kill(task.pid, signal.SIGCONT)
    subprocess.run(arguments, check=True, timeout=120)
    restored = json.loads((journal / "journal.json").read_text())
    assert restored["state"] == "rolled_back"
    for record in restored["records"]:
        assert subprocess.run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{record['label']}"],
                              capture_output=True).returncode == 0
    result = {"passed": True, "killed_after_native_unregister": True, "resumed_signed_command": True,
              "original_jobs_loaded": True, "candidate_source": value["identity"], "timestamp": time.time()}
    receipt.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
