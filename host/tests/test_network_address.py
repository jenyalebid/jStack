import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc"), reason="requires Swift")
def test_native_network_address_relationship(tmp_path):
    cases = [
        ("10.66.0.1/24", "10.66.0.0/24", True),
        ("10.66.0.1/24", "192.168.1.0/24", False),
        ("10.66.0.1/24", "10.66.0.1/24", False),
        ("10.66.0.1/24", "10.66.0.0/16", False),
        ("10.66.0.1/32", "10.66.0.1/32", True),
        ("10.66.0.1/31", "10.66.0.0/31", True),
        ("128.0.0.1/1", "128.0.0.0/1", True),
        ("1.2.3.4/0", "0.0.0.0/0", False),
        ("10.066.0.1/24", "10.66.0.0/24", False),
        ("10.66.0.1/+24", "10.66.0.0/24", False),
        ("256.1.1.1/24", "256.1.1.0/24", False),
        ("::1/128", "::1/128", False),
    ]
    main = tmp_path / "main.swift"
    main.write_text("\n".join(
        f'precondition(networkAddressPair({json.dumps(a)}, {json.dumps(s)}) == {str(expected).lower()})'
        for a, s, expected in cases))
    source = Path(__file__).resolve().parents[1] / "macos/NetworkAddress.swift"
    binary = tmp_path / "probe"
    subprocess.run(["swiftc", str(main), str(source), "-o", str(binary)], check=True, capture_output=True, timeout=60)
    subprocess.run([str(binary)], check=True, capture_output=True, timeout=10)
