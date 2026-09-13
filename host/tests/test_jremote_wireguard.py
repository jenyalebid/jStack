"""The WireGuard mesh tooling actually ships, and the three readers of its
state agree on where that state is.

`test_jremote_tunnel` drives `tunnel.py` against a *fake* `wg_peer.py`, which is
the right shape for testing the pairing gate but proves nothing about the real
script — a payload that shells out to `hostenv.peer_script()` is worth exactly
as much as the file being there. Before these tests the file was there on no
install: `hostenv.peer_script()` named a path nothing shipped, so `can_pair()`
was permanently False and every host was local-only whatever mode it claimed.

Two things are pinned here that a fake cannot pin:
  1. the real script and its leaf templates ship at the path the package names;
  2. the *one* directory the hub keeps its keys in has four independent readers
     — `install_hub.sh` (mints it, under sudo), `wg_peer.py` (the tool that edits
     the peer table), `tunnel.py` (the server that shells out to it), and
     `wg_up.sh`/`wg_sync.sh` (the root daemons that load and re-apply the conf) —
     and a default that drifts between them is a hub that pairs into a directory
     its own server never reads.

Point 2 was asserted for three of those four by comparing *defaults*, which is
the configuration in which nothing can go wrong. The relocation case — set
`WG_PEER_DIR`, the documented way to move the mesh — was covered by a test that
asserted `install_hub.sh` still contained a literal derivation that cannot honour
it, and by three more that skipped themselves whenever the variable was set. The
suite was green and the mesh split in half: peers appended to one conf, the
tunnel loaded from another, no error at either end. Section 2b runs the scripts
instead of reading them, and nothing in this file skips on `WG_PEER_DIR` now.

Everything runs against a fake `wg` binary and a temp state dir; nothing here
brings up an interface or touches the network.
"""

import ipaddress
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jstack_host import hostenv, tunnel


WG_ROOT = hostenv.peer_script().parent
LEAF_TEMPLATES = ("wg_up.sh", "wg_leaf_watch.sh", "install_leaf.sh")
HUB_SCRIPTS = ("wg_sync.sh", "install_hub.sh")


# --- 1. the payload actually ships ------------------------------------------

def test_the_pairing_tool_ships_at_the_path_the_package_names():
    """`hostenv.peer_script()` is a promise; this is the file keeping it."""
    script = hostenv.peer_script()
    assert script.is_file(), f"the payload names {script} but nothing ships there"
    assert script.name == "wg_peer.py"


def test_the_leaf_templates_ship_beside_the_tool():
    """`wg_peer.py add --leaf` copies these out of its own directory into the
    bundle it emits — a missing one is a leaf that installs and cannot come up,
    and it fails at pairing time on the hub, far from where anyone would look."""
    for name in LEAF_TEMPLATES:
        assert (WG_ROOT / name).is_file(), f"leaf bundle would be missing {name}"


def test_the_hub_scripts_ship_beside_the_tool():
    for name in HUB_SCRIPTS:
        assert (WG_ROOT / name).is_file(), f"a hub cannot be stood up without {name}"


# --- 2. one location, three readers -----------------------------------------

def _wg_peer_default_dir() -> Path:
    """`wg_peer.py`'s own default, computed the way the script computes it:
    `parents[2]/Credentials/wireguard` from the script's location."""
    return hostenv.peer_script().resolve().parents[2] / "Credentials" / "wireguard"


def _tunnel_default_dir() -> Path:
    """What the server resolves with `WG_PEER_DIR` out of the picture — the
    profile's answer, which for the default profile is the package root."""
    return hostenv.wireguard_dir()


def test_the_tool_and_the_server_default_to_the_same_directory(monkeypatch):
    """The reconciliation this file exists for: `wg_peer.py` writing one
    directory while `tunnel.py` reads another is a hub that pairs a device and
    then reports it cannot pair, because `can_pair()` looks at the wrong conf.

    Asserted against the *tool this host actually runs* rather than against a
    fixed path, which is the half that was missing: `peer_script()` has always
    been a profile answer and `WG_DIR` was not, so a host whose tooling lives
    outside the package tree read `<package>/Credentials/wireguard` while its
    own daemons drove another directory entirely. That is jStack#42 — the Mac
    holding `10.66.0.1` and five peers reporting itself `local`, with
    `/tunnel/pair` answering 503 on the one machine that owns the mesh. The
    relationship, not the literal, is what has to hold on every profile.

    `WG_PEER_DIR` is cleared rather than skipped over. This test used to skip
    itself whenever that variable was set, which is to say it ran only in the
    configuration where nothing could go wrong and stood down in the one where
    the readers actually split. A check that switches off in the failing case
    reports the same green as a check that passed.
    """
    monkeypatch.delenv("WG_PEER_DIR", raising=False)
    assert _wg_peer_default_dir() == _tunnel_default_dir()


def test_the_directory_is_a_profile_answer_and_follows_the_tool(monkeypatch):
    """A host whose mesh predates the package: the profile names both, and the
    server has to follow it rather than the tree it was imported from."""
    class Elsewhere:
        def peer_script(self):
            return Path("/opt/mesh/scripts/wireguard/wg_peer.py")

        def wireguard_dir(self):
            return Path("/opt/mesh/Credentials/wireguard")

    monkeypatch.delenv("WG_PEER_DIR", raising=False)
    monkeypatch.setattr(hostenv, "profile", lambda: Elsewhere())
    assert hostenv.wireguard_dir() == Path("/opt/mesh/Credentials/wireguard")


def test_a_profile_that_predates_the_question_still_resolves(monkeypatch):
    """An external profile is somebody else's file. A package upgrade that
    raises AttributeError on their machine is a package that broke them, so the
    seam falls back to the package default instead of insisting."""
    class Older:
        pass

    monkeypatch.delenv("WG_PEER_DIR", raising=False)
    monkeypatch.setattr(hostenv, "profile", lambda: Older())
    assert hostenv.wireguard_dir() == (
        hostenv.package_root() / "Credentials" / "wireguard")


def test_adopting_the_agents_environment_carries_the_mesh_and_rebinds(
        tmp_path, monkeypatch):
    """The other half of #42, from the other side: a shell has none of the
    installed agent's environment, and `WG_PEER_DIR` is the variable that says
    where the mesh is. Adopted after `tunnel` already resolved, it has to move
    the paths that were bound at import — a process reading one directory while
    its own daemons write another is the whole defect."""
    from jstack_host import install_host

    plist = tmp_path / "com.jremote.host.plist"
    mesh = tmp_path / "elsewhere" / "wireguard"
    plist.write_bytes(install_host.plistlib.dumps({
        "Label": "com.jremote.host",
        "EnvironmentVariables": {"JREMOTE_STATE_DIR": str(tmp_path / "state"),
                                 "WG_PEER_DIR": str(mesh)},
    }))
    assert install_host.installed_environment(plist)["WG_PEER_DIR"] == str(mesh)

    # Pinned so monkeypatch puts them back: `rebind()` reassigns module state,
    # and a test that leaves the process pointed at a tmp dir breaks the ones
    # after it rather than itself.
    for name in ("PEER_SCRIPT", "WG_DIR", "CLIENTS_DIR", "HUB_CONF"):
        monkeypatch.setattr(tunnel, name, getattr(tunnel, name))
    monkeypatch.delenv("WG_PEER_DIR", raising=False)
    monkeypatch.setattr(os, "environ", dict(os.environ))
    install_host.adopt_installed_environment(plist)
    assert tunnel.WG_DIR == mesh
    assert tunnel.HUB_CONF == mesh / "wg0.conf"
    assert tunnel.CLIENTS_DIR == mesh / "clients"


def test_the_env_wins_over_the_profile(monkeypatch):
    """`WG_PEER_DIR` is the variable `wg_peer.py` itself honours, so it has to
    outrank the profile here too — the tool and its readers move together or
    the split comes back under a different name."""
    class Elsewhere:
        def wireguard_dir(self):
            return Path("/opt/mesh/Credentials/wireguard")

    monkeypatch.setenv("WG_PEER_DIR", "/tmp/somewhere-else")
    monkeypatch.setattr(hostenv, "profile", lambda: Elsewhere())
    assert hostenv.wireguard_dir() == Path("/tmp/somewhere-else")


def test_the_server_uses_that_default_when_nothing_relocates_it(monkeypatch):
    """Same reason as above for clearing rather than skipping: the module
    constants are bound at import, so the environment has to be cleared *and*
    `rebind()` called, which is exactly what the one legitimate late-adopter
    (`adopt_installed_environment`) does."""
    monkeypatch.delenv("WG_PEER_DIR", raising=False)
    monkeypatch.setattr(tunnel, "WG_DIR", tunnel.WG_DIR)
    monkeypatch.setattr(tunnel, "CLIENTS_DIR", tunnel.CLIENTS_DIR)
    monkeypatch.setattr(tunnel, "HUB_CONF", tunnel.HUB_CONF)
    monkeypatch.setattr(tunnel, "PEER_SCRIPT", tunnel.PEER_SCRIPT)
    tunnel.rebind()
    assert tunnel.WG_DIR == _tunnel_default_dir()


# --- 2b. the shell half moves with the rest ---------------------------------
#
# `WG_PEER_DIR` is the documented way to relocate the mesh, and until this block
# existed only the Python half honoured it. `install_hub.sh` minted the keys and
# the conf by deriving from its own location, then wrote *that* path into both
# LaunchDaemons, while `wg_peer.py` appended peers to the relocated directory.
# The device got a working config; the hub never got the peer; the sync daemon
# watched a file nobody wrote. Nothing errored on either side.
#
# It survived a full suite because the test that covered it asserted the literal
# derivation was still present in the script — pinning the defect in place and
# calling it agreement — while the three tests that could have caught the split
# skipped themselves whenever `WG_PEER_DIR` was set. These run the scripts.

def _fake_wg(tmp_path: Path) -> Path:
    """A `wg` that mints deterministic keys and shrugs at everything else."""
    binary = tmp_path / "fake-wg"
    binary.write_text(
        "#!/bin/bash\n"
        'case "$1" in\n'
        '  genkey) echo "SERVER-PRIVATE-KEY" ;;\n'
        '  pubkey) cat >/dev/null; echo "SERVER-PUBLIC-KEY" ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n")
    binary.chmod(0o755)
    return binary


def _fake_launchctl(tmp_path: Path) -> Path:
    """`install_hub.sh` bootstraps two system LaunchDaemons. Under `HUB_DEST`
    it writes the plists into a temp tree, but the `launchctl` calls are not
    gated by it — unmocked, this test would try to load a real root daemon."""
    binary = tmp_path / "fake-launchctl"
    binary.write_text("#!/bin/bash\nexit 0\n")
    binary.chmod(0o755)
    return binary


def _run_sh(script: str, env: dict, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/bash", str(WG_ROOT / script), *args],
                          capture_output=True, text=True, env=env, timeout=60)


def _sh_env(tmp_path: Path, **extra) -> dict:
    """The environment every shell probe here runs under.

    `WG_CONF` is stripped because the probes are about what these scripts
    resolve when nothing pins them, and an ambient one would mask the answer.

    `WG_GO` and `WG` are pinned to the fake unconditionally. `wg_up.sh` only
    names its conf in the refusal it gives when that file is absent — so the
    moment a regression makes it resolve a conf that *does* exist, it walks
    straight past the refusal and into `wireguard-go -f utun`, which asks the
    kernel for a TUN device. The red-check for this block did exactly that.
    A suite that can spawn a tunnel daemon is a suite that can take down the
    mesh of whoever runs it.
    """
    env = {k: v for k, v in os.environ.items() if k != "WG_CONF"}
    fake = str(_fake_wg(tmp_path))
    env.update({"WG_GO": fake, "WG": fake, **extra})
    return env


def test_the_installer_mints_into_the_relocated_directory(tmp_path):
    """The load-bearing one. `install_hub.sh` is what creates the conf every
    other reader then argues about, so if it ignores `WG_PEER_DIR` the mesh is
    split before anything else gets a say."""
    mesh = tmp_path / "elsewhere" / "wireguard"
    env = _sh_env(tmp_path, WG_PEER_DIR=str(mesh),
                  HUB_DEST=str(tmp_path / "root"),
                  LAUNCHCTL=str(_fake_launchctl(tmp_path)))
    r = _run_sh("install_hub.sh", env, "--endpoint", "hub.example.com:51820")
    assert r.returncode == 0, r.stderr

    assert (mesh / "server.key").is_file(), (
        f"install_hub.sh ignored WG_PEER_DIR and minted somewhere else: {r.stdout}")
    assert (mesh / "wg0.conf").is_file()
    assert (mesh / "endpoint").read_text().strip() == "hub.example.com:51820"

    # And it must write that same conf into the daemons it installs, or the
    # tunnel comes up from one file while pairing edits another.
    daemons = tmp_path / "root" / "Library" / "LaunchDaemons"
    for name in ("com.jremote.hub.plist", "com.jremote.hub-sync.plist"):
        assert str(mesh / "wg0.conf") in (daemons / name).read_text(), (
            f"{name} names a conf the pairing tool does not write to")

    # The hub daemon carries the MTU clamp: a leaf at 1240 still stalls if the
    # hub keeps emitting 1420-sized datagrams toward it (jStack#54).
    hub_plist = (daemons / "com.jremote.hub.plist").read_text()
    assert "<key>WG_MTU</key>" in hub_plist
    assert "<string>1240</string>" in hub_plist


def test_the_tunnel_script_follows_the_relocated_mesh(tmp_path):
    """`wg_up.sh` resolves its conf before it touches the network, and says so
    on the way out — which is the observation point that needs no root and no
    interface. An absent conf under the relocated dir proves it looked there."""
    mesh = tmp_path / "elsewhere" / "wireguard"
    r = _run_sh("wg_up.sh", _sh_env(tmp_path, WG_PEER_DIR=str(mesh)))
    assert r.returncode != 0
    assert f"missing conf {mesh / 'wg0.conf'}" in r.stderr, (
        f"wg_up.sh resolved its conf somewhere other than WG_PEER_DIR: {r.stderr}")


def test_an_explicit_conf_still_outranks_the_relocated_mesh(tmp_path):
    """The rung order matters as much as the new rung: both installers write an
    explicit `WG_CONF` into the LaunchDaemon they make, and an installed copy
    that started preferring `WG_PEER_DIR` over it would move every running
    tunnel the first time anyone exported the variable in a shell."""
    mesh = tmp_path / "elsewhere" / "wireguard"
    pinned = tmp_path / "pinned" / "wg0.conf"
    r = _run_sh("wg_up.sh", _sh_env(tmp_path, WG_PEER_DIR=str(mesh),
                                    WG_CONF=str(pinned)))
    assert f"missing conf {pinned}" in r.stderr, (
        f"WG_CONF stopped winning — an installed tunnel would relocate: {r.stderr}")


def test_the_sync_daemon_follows_the_relocated_mesh(tmp_path):
    """The other hub-side reader. It is the one `WatchPaths` fires, so a sync
    script reading the tree's conf is a hub that never applies a new peer."""
    mesh = tmp_path / "elsewhere" / "wireguard"
    name_file = tmp_path / "iface.name"
    name_file.write_text("utun9\n")
    r = _run_sh("wg_sync.sh", _sh_env(tmp_path, WG_PEER_DIR=str(mesh),
                                      WG_NAME_FILE=str(name_file)))
    assert r.returncode == 0, r.stderr
    assert f"wg_sync: {mesh / 'wg0.conf'} -> utun9" in r.stdout, (
        f"wg_sync.sh synced a conf the pairing tool does not write to: {r.stdout}")


def test_every_reader_of_the_mesh_lands_on_one_directory(tmp_path):
    """The whole point, stated once. Four independent programs in three
    languages answer 'where is this hub's mesh state'; under a relocation they
    have to give one answer, and the count is asserted so a fifth reader added
    without being wired in fails here rather than in the field."""
    mesh = tmp_path / "elsewhere" / "wireguard"
    fake = _fake_wg(tmp_path)
    base = _sh_env(tmp_path, WG_PEER_DIR=str(mesh))

    answers = {}

    # 1. the tunnel daemon (bash, as root). First, deliberately: it names its
    # conf only in the refusal it gives when that file is absent, so probing it
    # after the installer has minted one observes nothing at all.
    r = _run_sh("wg_up.sh", base)
    assert "missing conf " in r.stderr, (
        f"wg_up.sh no longer names the conf it could not find: {r.stderr}")
    answers["wg_up.sh"] = Path(
        r.stderr.split("missing conf ", 1)[1].strip()).parent

    # 2. the server (Python, in-process)
    os.environ["WG_PEER_DIR"] = str(mesh)
    try:
        answers["tunnel.py"] = hostenv.wireguard_dir()
    finally:
        os.environ.pop("WG_PEER_DIR", None)

    # 3. the installer (bash, under sudo in real life)
    env = {**base, "HUB_DEST": str(tmp_path / "root"),
           "LAUNCHCTL": str(_fake_launchctl(tmp_path))}
    assert _run_sh("install_hub.sh", env, "--endpoint", "h:51820").returncode == 0
    assert (mesh / "wg0.conf").is_file()
    answers["install_hub.sh"] = (mesh / "wg0.conf").parent

    # 4. the pairing tool (python, subprocess, unprivileged)
    (mesh / "clients").mkdir(exist_ok=True)
    peer_env = {**base, "WG_BIN": str(fake), "WG_ENDPOINT": "h:51820"}
    assert _run_peer(peer_env, "add", "a-device").returncode == 0
    assert "# device: a-device" in (mesh / "wg0.conf").read_text()
    answers["wg_peer.py"] = mesh

    assert len(answers) == 4, "a reader was added without being asserted here"
    assert set(answers.values()) == {mesh}, f"the mesh split: {answers}"


def test_the_server_reads_the_subnet_out_of_the_real_script(monkeypatch):
    """`tunnel._peer_subnet()` parses `SUBNET_PREFIX` out of `wg_peer.py` rather
    than restating it. The existing suite proves that against a fake; this proves
    the *real* script still declares it in a form the parser accepts (it uses the
    `os.environ.get(..., "10.66.0")` override form, not a bare assignment)."""
    monkeypatch.setattr(tunnel, "PEER_SCRIPT", hostenv.peer_script())
    assert tunnel._peer_subnet() == ipaddress.ip_network("10.66.0.0/24")


# --- 3. the real tool pairs, lists, emits a leaf bundle, and revokes ---------

@pytest.fixture
def hub(tmp_path):
    """A hub the test owns end to end, against a fake `wg`: server keys, an
    empty conf, an endpoint, and `WG_PEER_DIR` pointing the real `wg_peer.py`
    at it. No interface, no network — just the file surgery pairing is."""
    wg_dir = tmp_path / "wireguard"
    (wg_dir / "clients").mkdir(parents=True)
    (wg_dir / "server.key").write_text("SERVER-PRIVATE-KEY\n")
    (wg_dir / "server.pub").write_text("SERVER-PUBLIC-KEY\n")
    (wg_dir / "wg0.conf").write_text(
        "[Interface]\nPrivateKey = SERVER-PRIVATE-KEY\nListenPort = 51820\n")
    (wg_dir / "endpoint").write_text("hub.example.com:51820\n")

    fake_wg = tmp_path / "fake-wg"
    fake_wg.write_text(
        "#!/bin/bash\n"
        'case "$1" in\n'
        '  genkey) echo "CLIENT-PRIVATE-KEY" ;;\n'
        '  pubkey) cat >/dev/null; echo "CLIENT-PUBLIC-KEY" ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n")
    fake_wg.chmod(0o755)

    env = {
        **os.environ,
        "WG_PEER_DIR": str(wg_dir),
        "WG_BIN": str(fake_wg),
        "WG_ENDPOINT": "hub.example.com:51820",
    }
    return wg_dir, env


def _run_peer(env, *args):
    return subprocess.run(
        [sys.executable, str(hostenv.peer_script()), *args],
        capture_output=True, text=True, env=env, timeout=60)


def test_the_real_tool_pairs_a_device(hub):
    wg_dir, env = hub
    r = _run_peer(env, "add", "work-mac")
    assert r.returncode == 0, r.stderr
    assert "# device: work-mac" in (wg_dir / "wg0.conf").read_text()
    conf = (wg_dir / "clients" / "work-mac.conf").read_text()
    assert "PrivateKey = CLIENT-PRIVATE-KEY" in conf
    assert "Endpoint = hub.example.com:51820" in conf
    assert "AllowedIPs = 10.66.0.0/24" in conf
    # Without a pinned MTU the profile inherits 1420, and any path narrower
    # than ~1480 passes the handshake then drops bulk traffic (jStack#54).
    assert "MTU = 1240" in conf


def test_the_real_tool_refuses_a_second_pairing_of_one_name(hub):
    _, env = hub
    assert _run_peer(env, "add", "work-mac").returncode == 0
    again = _run_peer(env, "add", "work-mac")
    assert again.returncode != 0
    assert "already paired" in again.stderr


def test_the_real_tool_lists_what_it_paired(hub):
    _, env = hub
    _run_peer(env, "add", "work-mac")
    _run_peer(env, "add", "phone")
    out = _run_peer(env, "list").stdout
    assert "work-mac" in out and "phone" in out


def test_a_leaf_bundle_carries_every_file_the_installer_reads(hub):
    """`tunnel.LEAF_FILES` is what the server hands back and what the leaf
    installs from; the tool must write all of them or the bundle installs a
    tunnel that cannot start. Assert against `tunnel.LEAF_FILES` so the two
    lists cannot drift apart."""
    wg_dir, env = hub
    r = _run_peer(env, "add", "--leaf", "studio")
    assert r.returncode == 0, r.stderr
    bundle = wg_dir / "clients" / "studio-leaf"
    for name in tunnel.LEAF_FILES:
        assert (bundle / name).is_file(), f"leaf bundle is missing {name}"
    leaf_conf = (bundle / "jrleaf.conf").read_text()
    assert "Address" not in leaf_conf, (
        "jrleaf.conf is setconf-style — an Address line makes `wg setconf` reject it")
    assert "MTU" not in leaf_conf, (
        "MTU is wg-quick syntax too — it travels in leaf.env, not the conf")
    assert "WG_MTU=1240" in (bundle / "leaf.env").read_text(), (
        "a bundle without the clamp brings the leaf up at 1420 and stalls "
        "constrained paths (jStack#54)")


def test_revoking_removes_the_peer_and_the_client_files(hub):
    wg_dir, env = hub
    _run_peer(env, "add", "work-mac")
    assert _run_peer(env, "remove", "work-mac").returncode == 0
    assert "# device: work-mac" not in (wg_dir / "wg0.conf").read_text()
    assert not (wg_dir / "clients" / "work-mac.conf").exists()
