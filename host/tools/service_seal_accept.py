"""Negative resource-seal probes confined to a disposable GUI Mac."""
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    model = subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.model"], text=True).strip()
    assert model.startswith("VirtualMac")
    network = sys.argv[1:] == ["network"]
    app = Path("/Library/PrivilegedHelperTools/jStack Network.app" if network else "/Applications/jStack Hub Services.app")
    assert (os.geteuid() == 0) == network
    target = app / "Contents/Resources" / ("services.json" if network else "runtime_entry.py")
    executable = app / "Contents/MacOS" / ("JStackNetwork" if network else "JStackRuntime")
    argv = [str(executable), "--check" if network else "self-test"]
    assert subprocess.run(argv, capture_output=True).returncode == 0
    original = target.read_bytes()
    try:
        target.write_bytes(original + b"\n# unsigned resource mutation\n")
        result = subprocess.run(argv, capture_output=True, text=True)
        assert result.returncode != 0, "unsigned source was accepted"
        assert "signature" in result.stderr, result.stderr
    finally:
        target.write_bytes(original)
    assert subprocess.run(argv, capture_output=True).returncode == 0
    receipt = {"unsigned_resources_rejected": True, "restored_seal_accepted": True}
    if network:
        mode = executable.stat().st_mode & 0o777
        try:
            executable.chmod(mode | 0o020)
            assert subprocess.run(argv, capture_output=True).returncode != 0
        finally:
            executable.chmod(mode)
        assert subprocess.run(argv, capture_output=True).returncode == 0
        receipt["writable_privileged_code_rejected"] = True
    path = Path("/Users/admin") / ("network-seal-receipt.json" if network else "service-seal-receipt.json")
    path.write_text(json.dumps(receipt, indent=2) + "\n")
    print(path.read_text())


if __name__ == "__main__":
    main()
