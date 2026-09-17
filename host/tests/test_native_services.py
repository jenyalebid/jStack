"""Compile the actual security-sensitive native sources in the host gate."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc"), reason="requires macOS Swift toolchain")
@pytest.mark.parametrize("filename,flags", [("Network.swift", ["-parse-as-library"]), ("ServiceControl.swift", [])])
def test_native_service_compiles(tmp_path, filename, flags):
    source = Path(__file__).resolve().parents[1] / "macos" / filename
    result = subprocess.run(["swiftc", *flags, "-O", "-o", str(tmp_path / "probe"), str(source)],
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
