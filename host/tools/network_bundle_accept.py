"""Disposable GUI-VM network-helper probes. Never run against a real Mac."""
import json
import os
from pathlib import Path
import subprocess
import time

APP = Path("/Library/PrivilegedHelperTools/jStack Network.app")
POLICY = Path("/Library/Preferences/live.jstack.network.json")
CONFIG = Path("/Users/admin/network-acceptance.conf")
NAME = Path("/var/run/wireguard/jstack-acceptance.name")


def run(*argv, **kwargs):
    return subprocess.run(argv, capture_output=True, check=True, **kwargs).stdout


def until(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.1)
    raise AssertionError("network condition did not become true")


def main():
    assert run("/usr/sbin/sysctl", "-n", "hw.model").decode().strip().startswith("VirtualMac")
    assert os.geteuid() == 0
    assert not POLICY.exists(), "refusing to replace an existing network policy"
    assert not CONFIG.exists(), "refusing to replace an existing network configuration"
    wg = str(APP / "Contents/MacOS/wg")
    executable = str(APP / "Contents/MacOS/JStackNetwork")
    private_key = run(wg, "genkey").decode().strip()
    CONFIG.write_text(f"[Interface]\nPrivateKey = {private_key}\nListenPort = 51987\n")
    CONFIG.chmod(0o600)
    policy = {"owner": 0, "configuration": str(CONFIG), "address": "10.199.76.1/24",
              "subnet": "10.199.76.0/24", "nameFile": str(NAME), "forwarding": False, "active": True}
    POLICY.write_text(json.dumps(policy))
    POLICY.chmod(0o644)
    receipts = {}
    run(executable, "--check")
    receipts["valid_configuration"] = True
    original = CONFIG.read_text()
    CONFIG.chmod(0o644)
    assert subprocess.run([executable, "--check"], capture_output=True).returncode != 0
    receipts["world_readable_key_rejected"] = True
    CONFIG.chmod(0o600)
    CONFIG.write_text(original + "PostUp = /usr/bin/true\n")
    assert subprocess.run([executable, "--check"], capture_output=True).returncode != 0
    receipts["script_directive_rejected"] = True
    CONFIG.write_text(original)
    logs = Path("/Users/admin/network-acceptance.log")
    with logs.open("wb") as stream:
        process = subprocess.Popen([executable], stdout=stream, stderr=stream)
        try:
            until(lambda: NAME.exists() or process.poll() is not None)
            assert process.poll() is None, logs.read_text()
            interface = NAME.read_text().strip()
            until(lambda: b"51987" in run(wg, "show", interface, "listen-port"))
            until(lambda: b"10.199.76.1" in run("/sbin/ifconfig", interface))
            receipts["interface_active"] = True
            peer_private = run(wg, "genkey")
            peer_public = run(wg, "pubkey", input=peer_private).decode().strip()
            CONFIG.write_text(original + f"\n[Peer]\nPublicKey = {peer_public}\nAllowedIPs = 10.199.76.2/32\n")
            until(lambda: peer_public.encode() in run(wg, "show", interface, "peers"))
            receipts["peer_sync"] = True
            CONFIG.write_text(original)
            until(lambda: not run(wg, "show", interface, "peers").strip())
            receipts["peer_removal_sync"] = True
            policy["active"] = False
            POLICY.write_text(json.dumps(policy))
            until(lambda: not NAME.exists())
            assert subprocess.run(["/sbin/ifconfig", interface], capture_output=True).returncode != 0
            receipts["deactivation_removes_interface"] = True
        finally:
            process.terminate()
            process.wait(timeout=15)
    target = Path("/Users/admin/network-acceptance-receipt.json")
    target.write_text(json.dumps(receipts, indent=2) + "\n")
    print(target.read_text())


if __name__ == "__main__":
    main()
