"""Observe the registered network daemon on a disposable GUI Mac only."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time

POLICY = Path("/Library/Preferences/live.jstack.network.json")
NAME = Path("/var/run/wireguard/jstack-acceptance.name")
RECEIPT = Path("/Users/admin/network-lifecycle-receipt.json")


def run(*argv):
    return subprocess.check_output(argv, text=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["activate", "reboot", "removed"])
    args = parser.parse_args()
    assert run("/usr/sbin/sysctl", "-n", "hw.model").strip().startswith("VirtualMac")
    assert os.geteuid() == 0
    receipt = json.loads(RECEIPT.read_text()) if RECEIPT.exists() else {}
    if args.phase == "activate":
        policy = json.loads(POLICY.read_text())
        assert policy["configuration"] == "/Users/admin/network-acceptance.conf"
        policy["active"] = True
        temporary = POLICY.with_suffix(".acceptance-tmp")
        temporary.write_text(json.dumps(policy))
        temporary.chmod(0o644)
        temporary.replace(POLICY)
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if NAME.exists() == (args.phase != "removed"):
            break
        time.sleep(.2)
    if args.phase == "removed":
        assert not NAME.exists(), "daemon left its tunnel behind"
        assert subprocess.run(["/bin/launchctl", "print", "system/live.jstack.network"], capture_output=True).returncode != 0
    else:
        job = run("/bin/launchctl", "print", "system/live.jstack.network")
        assert "state = running" in job and "parent bundle identifier = live.jstack.network" in job
        interface = NAME.read_text().strip()
        while time.monotonic() < deadline and "10.199.76.1" not in run("/sbin/ifconfig", interface):
            time.sleep(.2)
        assert "10.199.76.1" in run("/sbin/ifconfig", interface)
        assert run("/Library/PrivilegedHelperTools/jStack Network.app/Contents/MacOS/wg", "show", interface, "listen-port").strip() == "51987"
    receipt[args.phase] = True
    RECEIPT.write_text(json.dumps(receipt, indent=2) + "\n")
    print(RECEIPT.read_text())


if __name__ == "__main__":
    main()
