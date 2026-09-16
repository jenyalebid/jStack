"""The signed Mac-app installer stays idempotent under unattended setup."""

from __future__ import annotations

import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys

import pytest


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS app installer")


def test_yes_does_not_redownload_the_build_already_installed(tmp_path: Path):
    """The offline joiner uses --yes; it must not turn that into --force."""
    repo = Path(__file__).resolve().parents[2]
    installer = repo / "app" / "install.sh"
    prefix = tmp_path / "Applications"
    contents = prefix / "jRemote.app" / "Contents"
    contents.mkdir(parents=True)
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump({"CFBundleVersion": "67"}, handle)

    calls = tmp_path / "curl.calls"
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    curl = stub_dir / "curl"
    curl.write_text(
        """#!/bin/bash
set -u
out=""
url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift ;;
    http*) url="$1" ;;
  esac
  shift
done
printf '%s\\n' "$url" >> "$APP_INSTALL_CURL_CALLS"
case "$url" in
  */latest.json)
    printf '%s' '{"sha256":"unused","file":"jRemote.zip","build":67,"version":"1.0"}' > "$out"
    exit 0 ;;
  *) exit 99 ;;
esac
"""
    )
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR)

    env = os.environ.copy()
    env.update(
        PATH=f"{stub_dir}:{env['PATH']}",
        APP_INSTALL_CURL_CALLS=str(calls),
    )
    result = subprocess.run(
        ["bash", str(installer), "--yes", "--tag", "mac-app-test",
         "--repo", "owner/repo", "--prefix", str(prefix)],
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert calls.read_text().splitlines() == [
        "https://github.com/owner/repo/releases/download/mac-app-test/latest.json"
    ]
    assert "already installed" in result.stdout
