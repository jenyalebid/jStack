"""jStack#127 — attach must not re-install a tunnel that is already up.

The offline joiner installs the carried bundle first and redeems second, so the
redeem hands attach the very bundle the machine is already running. Before this,
attach ran `install_leaf.sh` again regardless, which bounced the live daemon and
— reading the utun name the old daemon left behind — waited twenty seconds for
a handshake on an interface that no longer existed. The hub received heartbeats
throughout; the joiner printed "the leaf installer failed".

Pinned here, against the real probe and the real shipped `install_leaf.sh`
under their `LEAF_DEST` seam:

  · identical installed files + a fresh handshake → the installer is not run;
  · a differing conf, a missing install, or a stale/absent handshake → it is;
  · the installer clears the old utun name file before it boots the daemon.
"""

import os
import subprocess
import time
from pathlib import Path

import pytest

from jstack_host import attach_parent, hostenv, tunnel


@pytest.fixture(autouse=True)
def _own_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("JREMOTE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("JREMOTE_HOST_ID", "test-host-0001")


GOOD_KEY = "test-host-0001"
CONF = ("[Interface]\nPrivateKey = KEY\n[Peer]\nPublicKey = PUB\n"
        "Endpoint = hub.example.com:51820\nAllowedIPs = 10.66.0.0/24\n")
LEAF_ENV = "WG_ADDR=10.66.0.7/32\nWG_SUBNET=10.66.0.0/24\nWG_HUB=10.66.0.1\n"


def _bundle() -> dict:
    wg = hostenv.peer_script().parent
    return {
        "jrleaf.conf": CONF,
        "leaf.env": LEAF_ENV,
        "install_leaf.sh": (wg / "install_leaf.sh").read_text(),
        "wg_up.sh": (wg / "wg_up.sh").read_text(),
        "wg_leaf_watch.sh": (wg / "wg_leaf_watch.sh").read_text(),
        "README.md": "# leaf\n",
    }


def _host_response() -> dict:
    return {
        "device": {"id": "dev_new", "name": "studio", "revoked": False},
        "token": "TOKEN-FROM-PARENT",
        "tunnel": {"device": "studio", "bundle": _bundle(), "created": False},
        "tunnel_note": "",
        "kind": "host",
        "host": {"key": GOOD_KEY, "name": "studio", "address": "10.66.0.7",
                 "port": 9090, "deleted": False},
        "superseded": False,
    }


def _poster(url, payload):
    return 200, _host_response()


def _recording_runner(sink):
    def runner(argv, cwd=None, env=None, **kw):
        sink.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="installed", stderr="")
    return runner


def _installed_root(tmp_path: Path, *, conf=CONF, iface="utun9",
                    handshake_age=5) -> dict:
    """A LEAF_DEST root shaped the way `install_leaf.sh` leaves a machine, with
    a stub `wg` whose `latest-handshakes` answer is `handshake_age` seconds old
    (None: never handshook)."""
    root = tmp_path / "root"
    (root / "etc/wireguard").mkdir(parents=True)
    (root / "etc/wireguard/jrleaf.conf").write_text(conf)
    app = root / "Library/Application Support/jRemote Leaf"
    app.mkdir(parents=True)
    (app / "leaf.env").write_text(LEAF_ENV)
    (root / "var/run/wireguard").mkdir(parents=True)
    if iface:
        (root / "var/run/wireguard/jremote-wg.name").write_text(iface)
    stamp = 0 if handshake_age is None else int(time.time()) - handshake_age
    wg = tmp_path / "wg"
    wg.write_text(f"#!/bin/bash\nprintf 'PUB\\t{stamp}\\n'\n")
    wg.chmod(0o755)
    launchctl = tmp_path / "launchctl"
    launchctl.write_text("#!/bin/bash\nexit 0\n")
    launchctl.chmod(0o755)
    return {**os.environ, "LEAF_DEST": str(root), "LAUNCHCTL": str(launchctl),
            "WG_GO": "/usr/bin/true", "WG": str(wg)}


def _attach(tmp_path, env, runs):
    return attach_parent.attach(
        "ABCD-1234", "http://studio.local:9090", host_key=GOOD_KEY,
        dest_dir=tmp_path / "bundle", poster=_poster,
        runner=_recording_runner(runs), sudo=False, install_env=env)


def test_the_same_tunnel_already_shaking_hands_is_left_alone(tmp_path):
    env = _installed_root(tmp_path, handshake_age=5)
    runs = []
    result = _attach(tmp_path, env, runs)

    assert runs == [], "the installer ran against a tunnel that was already up"
    assert result["installer_ran"] is False
    assert "already up on utun9" in result["installer_output"]
    # Everything else attach does still happened: the bundle is on disk and the
    # parent record was written, so a later re-attach re-keys.
    assert (tmp_path / "bundle/jrleaf.conf").read_text() == CONF
    assert (Path(env["JREMOTE_STATE_DIR"]) / attach_parent.PARENT_RECORD).is_file()


def test_a_different_installed_conf_gets_the_installer(tmp_path):
    env = _installed_root(tmp_path, conf=CONF.replace("KEY", "OLDKEY"))
    runs = []
    result = _attach(tmp_path, env, runs)
    assert runs == [["bash", str(tmp_path / "bundle/install_leaf.sh")]]
    assert result["installer_ran"] is True


def test_nothing_installed_gets_the_installer(tmp_path):
    env = {**os.environ, "LEAF_DEST": str(tmp_path / "empty-root"),
           "WG": "/usr/bin/true"}
    runs = []
    result = _attach(tmp_path, env, runs)
    assert len(runs) == 1 and runs[0][-1].endswith("install_leaf.sh")
    assert result["installer_ran"] is True


@pytest.mark.parametrize("age", [None, 600], ids=["never", "stale"])
def test_matching_files_without_a_live_handshake_get_the_installer(tmp_path, age):
    """The skip is not a way past a repair: same conf, dead tunnel → re-install."""
    env = _installed_root(tmp_path, handshake_age=age)
    runs = []
    result = _attach(tmp_path, env, runs)
    assert len(runs) == 1
    assert result["installer_ran"] is True


def test_matching_files_but_no_utun_name_get_the_installer(tmp_path):
    env = _installed_root(tmp_path, iface="")
    runs = []
    _attach(tmp_path, env, runs)
    assert len(runs) == 1


def test_a_probe_that_cannot_run_falls_back_to_the_installer(tmp_path):
    env = _installed_root(tmp_path)
    runs = []

    def broken_prober(argv, **kw):
        raise OSError("no bash here")

    result = attach_parent.attach(
        "ABCD-1234", "http://studio.local:9090", host_key=GOOD_KEY,
        dest_dir=tmp_path / "bundle", poster=_poster,
        runner=_recording_runner(runs), sudo=False, install_env=env,
        prober=broken_prober)
    assert len(runs) == 1 and result["installer_ran"] is True


def test_on_a_real_machine_the_probe_runs_under_sudo_and_the_installer_too(tmp_path):
    """No LEAF_DEST means the real `/`, where the conf and the name file are
    root-only — both the probe and the installer must ask for sudo."""
    seen = []

    def prober(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 3, stdout="", stderr="")

    runs = []
    env = {k: v for k, v in os.environ.items() if k != "LEAF_DEST"}
    attach_parent.attach(
        "ABCD-1234", "http://studio.local:9090", host_key=GOOD_KEY,
        dest_dir=tmp_path / "bundle", poster=_poster,
        runner=_recording_runner(runs), sudo=True, install_env=env,
        prober=prober)
    assert seen[0][:3] == ["sudo", "bash", "-c"]
    assert seen[0][-2] == ""  # root argument: the real filesystem
    assert runs == [["sudo", "bash", str(tmp_path / "bundle/install_leaf.sh")]]


def test_the_installer_clears_the_stale_utun_name_before_booting_the_daemon(tmp_path):
    """Run the real shipped script under LEAF_DEST with a utun name left behind
    by a previous daemon: after the install pass the file is gone, so the
    handshake wait can only read the name the fresh daemon writes."""
    env = _installed_root(tmp_path, iface="utun3")
    root = Path(env["LEAF_DEST"])
    name_file = root / "var/run/wireguard/jremote-wg.name"
    assert name_file.read_text() == "utun3"

    bundle = tmp_path / "bundle"
    attach_parent._write_bundle(_bundle(), bundle)
    proc = subprocess.run(["bash", str(bundle / "install_leaf.sh")], cwd=str(bundle),
                          env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "installed com.jremote.leaf" in proc.stdout
    assert not name_file.exists()
    # …and the layout still lands, so this is a removal, not a different install.
    assert (root / "etc/wireguard/jrleaf.conf").read_text() == CONF
    for name in tunnel.LEAF_FILES:
        assert (bundle / name).is_file()
