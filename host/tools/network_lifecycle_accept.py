"""Observe the registered network daemon on a disposable GUI Mac only."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import subprocess
import time

POLICY = Path("/Library/Preferences/live.jstack.network.json")
NAME = Path("/var/run/wireguard/jstack-acceptance.name")
RECEIPT = Path("/Users/admin/network-lifecycle-receipt.json")


def run(*argv):
    return subprocess.check_output(argv, text=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["activate", "reboot", "crash", "removed"])
    args = parser.parse_args()
    assert run("/usr/sbin/sysctl", "-n", "hw.model").strip().startswith("VirtualMac")
    assert os.geteuid() == 0
    receipt = json.loads(RECEIPT.read_text()) if RECEIPT.exists() else {}
    version = plistlib.loads(Path("/Library/PrivilegedHelperTools/jStack Network.app/Contents/Info.plist").read_bytes())["CFBundleVersion"]
    if receipt.get("version") != version:
        receipt = {"version": version}
    old_pid = None
    if args.phase == "crash":
        job = run("/bin/launchctl", "print", "system/live.jstack.network")
        assert "parent bundle identifier = live.jstack.network" in job
        old_pid = int(re.search(r"\n\s*pid = (\d+)", job)[1])
        os.kill(old_pid, signal.SIGKILL)
    if args.phase == "activate":
        policy = json.loads(POLICY.read_text())
        assert policy["configuration"] == "/Users/admin/network-acceptance.conf"
        policy["active"] = True
        temporary = POLICY.with_suffix(".acceptance-tmp")
        temporary.write_text(json.dumps(policy))
        temporary.chmod(0o644)
        temporary.replace(POLICY)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if NAME.exists() == (args.phase != "removed"):
            break
        time.sleep(.2)
    if args.phase == "removed":
        while time.monotonic() < deadline and subprocess.run(
                ["/bin/launchctl", "print", "system/live.jstack.network"], capture_output=True).returncode == 0:
            time.sleep(.2)
        assert not NAME.exists(), "daemon left its tunnel behind"
        assert subprocess.run(["/bin/launchctl", "print", "system/live.jstack.network"], capture_output=True).returncode != 0
    else:
        if old_pid is not None:
            while time.monotonic() < deadline:
                job = run("/bin/launchctl", "print", "system/live.jstack.network")
                match = re.search(r"\n\s*pid = (\d+)", job)
                if match and int(match[1]) != old_pid:
                    break
                time.sleep(.2)
            assert match and int(match[1]) != old_pid, "launchd did not restart the daemon"
            try:
                os.killpg(old_pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError("old daemon process group survived its crash")
        job = run("/bin/launchctl", "print", "system/live.jstack.network")
        assert "state = running" in job and "parent bundle identifier = live.jstack.network" in job
        interface = ""
        while time.monotonic() < deadline:
            interface = NAME.read_text().strip() if NAME.exists() else ""
            probe = subprocess.run(["/sbin/ifconfig", interface], capture_output=True, text=True)
            if probe.returncode == 0 and "10.199.76.1" in probe.stdout:
                break
            time.sleep(.2)
        assert "10.199.76.1" in run("/sbin/ifconfig", interface)
        assert run("/Library/PrivilegedHelperTools/jStack Network.app/Contents/MacOS/wg", "show", interface, "listen-port").strip() == "51987"
    receipt[args.phase] = True
    RECEIPT.write_text(json.dumps(receipt, indent=2) + "\n")
    print(RECEIPT.read_text())


if __name__ == "__main__":
    main()
