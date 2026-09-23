"""Installing a mesh must leave the signed application byte-for-byte intact."""
import hashlib
import os
from pathlib import Path
import plistlib
import runpy
import shutil
import subprocess

import pytest

from jstack_host import hostenv


def tree_bytes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("override", [None, "JREMOTE_CREDENTIALS_DIR", "WG_PEER_DIR"])
def test_mesh_install_does_not_change_the_app_bundle(tmp_path, monkeypatch, override):
    app = tmp_path / "jStack Hub.app"
    packages = app / "Contents/Resources/packages"
    scripts = packages / "scripts/wireguard"
    source = Path(__file__).resolve().parents[1] / "scripts/wireguard"
    shutil.copytree(source, scripts)
    home = tmp_path / "home"
    home.mkdir()
    fake = tmp_path / "wg"
    fake.write_text('#!/bin/bash\ncase "$1" in\n'
                    'genkey) echo test-private ;;\n'
                    'pubkey) cat >/dev/null; echo test-public ;;\nesac\n')
    fake.chmod(0o755)
    env = {k: v for k, v in os.environ.items()
           if k not in {"WG_PEER_DIR", "WG_CONF", "SUDO_USER", "JREMOTE_CREDENTIALS_DIR"}}
    env.update(HOME=str(home), HUB_DEST=str(tmp_path / "root"),
               WG=str(fake), WG_GO=str(fake), LAUNCHCTL="/usr/bin/true")
    if override:
        env[override] = str(tmp_path / "relocated")
    before = tree_bytes(app)
    result = subprocess.run(["/bin/bash", str(scripts / "install_hub.sh"),
                             "--endpoint", "192.0.2.1:51820"],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert tree_bytes(app) == before, "mesh provisioning changed sealed application resources"
    daemons = tmp_path / "root/Library/LaunchDaemons"
    configs = []
    for name in ("com.jremote.hub", "com.jremote.hub-sync"):
        plist = plistlib.loads((daemons / (name + ".plist")).read_bytes())
        configs.append(Path(plist["EnvironmentVariables"]["WG_CONF"]))
    assert configs[0] == configs[1]
    assert configs[0].is_file()
    assert not configs[0].is_relative_to(app)
    monkeypatch.setattr(os, "environ", env)
    monkeypatch.setattr(hostenv, "HOME", home)
    monkeypatch.setattr(hostenv, "package_root", lambda: packages)
    monkeypatch.setattr(hostenv, "profile", lambda: hostenv.DefaultProfile(home))
    tool = runpy.run_path(str(scripts / "wg_peer.py"))
    assert tool["WG_DIR"] == configs[0].parent == hostenv.wireguard_dir()
