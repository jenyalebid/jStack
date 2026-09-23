"""The carried join bundle — `jstack-host adopt --offline`.

The thing under test is an ORDER, not a file. A Mac off the hub's LAN has no
route to the hub until the tunnel is up, and the enrolment code is redeemed
over that same tunnel — so a join script that attaches before it installs is a
join script that cannot ever work, however correct each half looks alone. The
ordering test below is the one that matters; the rest guard the parts it
depends on.
"""

from __future__ import annotations

import pytest

from jstack_host import adopt_offline


@pytest.fixture(autouse=True)
def no_real_installer(tmp_path, monkeypatch):
    # A newly added bootstrap path must fail locally unless a test explicitly
    # supplies its own installer. An incomplete command mock must never reach
    # the public installer or mutate the developer machine's packages.
    monkeypatch.setenv("JSTACK_INSTALL_URL", (tmp_path / "no-installer").as_uri())


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    """A leaf bundle folder as `tunnel.issue(leaf=True)` leaves it."""
    from jstack_host import tunnel

    clients = tmp_path / "clients"
    folder = clients / "work-mac-leaf"
    folder.mkdir(parents=True)
    for name in tunnel.LEAF_FILES:
        (folder / name).write_text(f"# {name}\n")
    monkeypatch.setattr(tunnel, "CLIENTS_DIR", clients)
    return folder


@pytest.mark.parametrize("display_name,peer", [("Work Mac", "work-mac"),
                                               ("New Mac", "new-mac")])
def test_cli_joiner_accepts_the_name_shown_in_the_adopt_dialog(
        bundle, monkeypatch, capsys, display_name, peer):
    import json
    from pathlib import Path
    from jstack_host import cli, enrolment, tunnel

    folder = bundle.with_name(peer + "-leaf")
    if folder != bundle:
        bundle.rename(folder)
    monkeypatch.setattr(tunnel, "can_pair", lambda: True)
    monkeypatch.setattr(tunnel, "live_peers", lambda: {peer})
    row = {"name": display_name, "code": "TEST-CODE", "expires_in": 600,
           "kind": enrolment.KIND_HOST}
    assert cli._adopt_offline(display_name, row, 9090, as_json=True) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["name"] == display_name
    assert (folder / "join.sh").is_file()
    assert Path(answer["file"]).is_file()


def _run_join(bundle, tmp_path, capable=True):
    """Run join.sh for real against fakes, and return what it invoked, in order.

    Asserted by RUNNING it rather than by reading it. The first version of this
    test compared the text positions of `install_leaf.sh` and `jstack-host
    attach`, which is not the execution order at all — `install_leaf.sh` is
    also named in a prerequisite check near the top of the file, so the test
    went green against a script with the two steps deliberately swapped. A
    probe that cannot observe the thing it is named for is worse than none.
    """
    import os
    import subprocess

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "calls.log"

    for name, body in (
        # `sudo <path>/install_leaf.sh` — the tunnel step, logged by what it ran.
        ("sudo", 'echo "install:$(basename "$1")" >> "$CALLS"'),
        # An old build has no `capabilities` subcommand, so argparse exits
        # non-zero on the unknown choice — the real signal, reproduced.
        ("jstack-host",
         'echo "jstack-host:$1" >> "$CALLS"\n'
         'if [ "$1" = capabilities ]; then\n'
         f'  {"printf \'managed-access-v1\\nmanaged-app-v1\\n\'" if capable else "exit 2"}\n'
         'fi'),
        ("ping", 'echo "ping" >> "$CALLS"'),
        ("curl", 'echo "download" >> "$CALLS"; exit 9'),
        # Present only so the prereq gate passes; never invoked.
        ("wireguard-go", "true"),
        ("wg", "true"),
    ):
        p = fake_bin / name
        p.write_text(f'#!/bin/bash\n{body}\nexit 0\n')
        p.chmod(0o755)

    env = dict(os.environ, PATH=f"{fake_bin}:/usr/bin:/bin", CALLS=str(calls))
    subprocess.run(["/bin/bash", str(bundle / "join.sh")],
                   env=env, capture_output=True, text=True, timeout=60)
    return calls.read_text().splitlines() if calls.exists() else []


def test_the_join_script_installs_the_tunnel_before_it_redeems(bundle, tmp_path):
    """The whole point, asserted as an order — by running it.

    Reversed, every other assertion in this file still passes and the bundle is
    still useless: `attach` would be asked to reach a mesh address from a
    machine that is not yet on the mesh, which is the exact deadlock this
    feature exists to break.
    """
    adopt_offline.emit("work-mac", "PQ4V-LUGA", 9090)
    calls = _run_join(bundle, tmp_path)

    assert "install:install_leaf.sh" in calls, f"the tunnel never installed: {calls}"
    attach = next(i for i, c in enumerate(calls) if c.startswith("jstack-host:attach"))
    install = calls.index("install:install_leaf.sh")
    assert install < attach, f"redeemed before the tunnel was up: {calls}"


def test_a_host_too_old_to_delegate_is_refused_before_it_attaches(bundle,
                                                                  tmp_path):
    """The live 2026-09-11 adoption, as a gate.

    That Mac's `jstack-host` predated delegated minting. The joiner asked only
    whether the binary existed, so `attach` ran, succeeded, and handed back no
    grant — leaving a machine the hub could never mint onto again, with the
    tunnel up and every step reporting success. It surfaced as `pair-by-hand`
    in a menu days later.

    Spending the code is the irreversible part, so the check belongs *before*
    it: refusing costs an upgrade, attaching costs the code and produces the
    half-adopted Mac anyway. `version` cannot gate this — it has printed the
    same 0.1.0 on every build ever cut.
    """
    adopt_offline.emit("work-mac", "PQ4V-LUGA", 9090)
    calls = _run_join(bundle, tmp_path, capable=False)

    assert "install:install_leaf.sh" in calls, (
        f"the tunnel must still go up — it is the half that survives: {calls}")
    assert not any(c.startswith("jstack-host:attach") for c in calls), (
        f"attached with a host that cannot hand back a grant: {calls}")


def test_the_join_script_carries_the_code_and_the_mesh_parent(bundle):
    adopt_offline.emit("work-mac", "PQ4V-LUGA", 9090)
    script = (bundle / "join.sh").read_text()

    assert 'CODE="PQ4V-LUGA"' in script
    assert 'PARENT="http://10.66.0.1:9090"' in script


def test_the_join_script_is_executable(bundle):
    """It is handed to a person as `./join.sh`, so it has to run as one."""
    adopt_offline.emit("work-mac", "PQ4V-LUGA", 9090)
    assert (bundle / "join.sh").stat().st_mode & 0o111


def test_an_expired_code_is_reported_as_survivable(bundle):
    """The tunnel outlives the code, and the script has to say so.

    A bundle carried to another building may well be opened after the code has
    aged out. Reported as a flat failure, that reads as a wasted trip and the
    person carries a second folder over. It is not: step one is permanent, and
    a fresh code can be redeemed from the far Mac itself.
    """
    adopt_offline.emit("work-mac", "PQ4V-LUGA", 9090)
    script = (bundle / "join.sh").read_text()

    assert "The tunnel is UP" in script
    assert "jstack-host adopt work-mac" in script


def test_the_hub_owned_readme_is_left_alone(bundle):
    """`tunnel._read_bundle` reads README.md back as this peer's issued state.

    Writing the join instructions over it would change what every future
    redeem of this peer hands back — so they go beside it.
    """
    before = (bundle / "README.md").read_text()
    adopt_offline.emit("work-mac", "PQ4V-LUGA", 9090)

    assert (bundle / "README.md").read_text() == before
    assert "join.sh" in (bundle / "JOIN.md").read_text()


def test_emit_refuses_when_the_tunnel_half_was_never_written(tmp_path, monkeypatch):
    """A join script pointing at an installer that is not there is worse than none."""
    from jstack_host import tunnel

    monkeypatch.setattr(tunnel, "CLIENTS_DIR", tmp_path / "clients")
    with pytest.raises(tunnel.TunnelError) as exc:
        adopt_offline.emit("work-mac", "PQ4V-LUGA", 9090)
    assert "tunnel half" in str(exc.value)


def test_the_packed_file_is_one_executable_that_carries_everything(bundle, tmp_path):
    """What a person actually carries — one file, not a folder of eight.

    A folder is eight things, and the one that has to be run is not obviously
    the one to run. Asserted by unpacking and running the packed file, because
    a payload that base64-decodes but does not contain the installer is a file
    that fails on the machine you travelled to.
    """
    import os
    import subprocess

    adopt_offline.emit("work-mac", "PQ4V-LUGA", 9090)
    packed = adopt_offline.pack("work-mac", "PQ4V-LUGA", 9090)

    assert packed.name == "join-work-mac.sh"
    assert packed.stat().st_mode & 0o111, "it has to run as ./join-work-mac.sh"
    # It carries a private key and a live code: not group- or world-readable.
    assert not packed.stat().st_mode & 0o077, oct(packed.stat().st_mode)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "packed-calls.log"
    for name, body in (
        ("sudo", 'echo "install:$(basename "$1")" >> "$CALLS"'),
        # A current build — this test is about the payload, not the gate.
        ("jstack-host",
         'echo "jstack-host:$1" >> "$CALLS"\n'
         'if [ "$1" = capabilities ]; then printf "managed-access-v1\\nmanaged-app-v1\\n"; fi'),
        ("ping", 'echo "ping" >> "$CALLS"'),
        ("wireguard-go", "true"),
        ("wg", "true"),
    ):
        p = fake_bin / name
        p.write_text(f'#!/bin/bash\n{body}\nexit 0\n')
        p.chmod(0o755)

    env = dict(os.environ, PATH=f"{fake_bin}:/usr/bin:/bin", CALLS=str(calls))
    proc = subprocess.run(["/bin/bash", str(packed)], env=env,
                          capture_output=True, text=True, timeout=60)
    done = calls.read_text().splitlines() if calls.exists() else []

    assert "install:install_leaf.sh" in done, (proc.stdout, proc.stderr, done)
    attach = next(i for i, c in enumerate(done) if c.startswith("jstack-host:attach"))
    assert done.index("install:install_leaf.sh") < attach, done


def test_the_packed_file_leaves_nothing_unpacked_behind(bundle, tmp_path):
    """It unpacks a private key to run. That directory does not outlive the run."""
    import os
    import re
    import subprocess
    from pathlib import Path

    adopt_offline.emit("work-mac", "PQ4V-LUGA", 9090)
    packed = adopt_offline.pack("work-mac", "PQ4V-LUGA", 9090)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("sudo", "jstack-host", "ping", "wireguard-go", "wg", "curl", "brew"):
        p = fake_bin / name
        # `sudo` echoes where it was told to run from, which is the temp dir.
        body = ('printf "managed-access-v1\\nmanaged-app-v1\\n"' if name == "jstack-host" else 'echo "RAN:$1"')
        p.write_text(f'#!/bin/bash\n{body}\nexit 0\n')
        p.chmod(0o755)

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env = dict(os.environ, PATH=f"{fake_bin}:/usr/bin:/bin", TMPDIR=str(scratch))
    proc = subprocess.run(["/bin/bash", str(packed)], env=env,
                          capture_output=True, text=True, timeout=60)

    unpacked = re.search(r"RAN:(\S+)/install_leaf\.sh", proc.stdout)
    assert unpacked, proc.stdout
    assert not Path(unpacked.group(1)).exists(), "the unpacked key outlived the run"


def test_relift_rebuilds_a_bundle_without_touching_the_keypair(tmp_path, monkeypatch):
    """The already-paired case — the one the feature exists for.

    `wg_peer add` refuses a name already in the peer table, so the only code
    path that writes leaf artefacts is closed to exactly the machines that need
    them: a Mac paired as a device, or one whose bundle folder was deleted.
    Re-minting would hand the hub a public key that machine cannot produce, so
    the keypair has to survive the rebuild.
    """
    from jstack_host import tunnel

    clients = tmp_path / "clients"
    clients.mkdir(parents=True)
    (clients / "work-mac.conf").write_text(
        "[Interface]\n"
        "PrivateKey = cMhvzZeAeoCL/Mnbk7eojex8RYmyB6hxFBUxAZWGyVs=\n"
        "Address = 10.66.0.7/32\n"
        "\n"
        "[Peer]\n"
        "PublicKey = 20azGn2YM1qDB6s6J1JstrbzyXUGi8LGi0WvlzgDnU4=\n"
        "AllowedIPs = 10.66.0.0/24\n"
        "Endpoint = wg.example.com:51820\n"
        "PersistentKeepalive = 25\n")
    monkeypatch.setattr(tunnel, "CLIENTS_DIR", clients)

    folder = adopt_offline.relift("work-mac")
    leaf = (folder / "jrleaf.conf").read_text()

    assert "cMhvzZeAeoCL/Mnbk7eojex8RYmyB6hxFBUxAZWGyVs=" in leaf
    # `wg setconf` rejects wg-quick syntax: the address has to move out of the
    # conf and into leaf.env, which is the only difference between the shapes.
    assert "Address" not in leaf
    assert "MTU" not in leaf
    env = (folder / "leaf.env").read_text()
    assert "WG_ADDR=10.66.0.7/32" in env
    assert "WG_HUB=10.66.0.1" in env
    # This conf predates the MTU key entirely — the rebuild must still clamp,
    # or every relifted leaf comes back at the 1420 default that stalls
    # constrained paths (jStack#54).
    assert "WG_MTU=1240" in env


def test_relift_refuses_when_the_private_key_is_gone(tmp_path, monkeypatch):
    """A peer whose conf was deleted cannot be rebuilt, and saying so is the fix.

    The private key lived in two places only — that file and the far machine.
    Inventing a new one here would produce a bundle that installs cleanly and
    never handshakes, which is the failure that is hardest to read from the
    other end.
    """
    from jstack_host import tunnel

    clients = tmp_path / "clients"
    clients.mkdir(parents=True)
    monkeypatch.setattr(tunnel, "CLIENTS_DIR", clients)

    with pytest.raises(tunnel.TunnelError) as exc:
        adopt_offline.relift("work-mac")
    assert "cannot be rebuilt" in str(exc.value)


def test_an_offline_code_gets_the_longest_life_the_mint_allows():
    """A carried code is walked to another building, not typed within the minute."""
    from jstack_host import enrolment

    assert enrolment.MAX_TTL > enrolment.DEFAULT_TTL


def test_an_installed_hub_can_name_the_installer_without_git(tmp_path, monkeypatch):
    """The second wall the joiner file hit, after the mesh tooling.

    Every other test in this file runs under the autouse fixture that sets
    `JSTACK_INSTALL_URL`, so none of them ever exercised the resolver a real
    hub falls back to. This one unsets it and stands where the shipped Hub
    stands: a signed tree, no checkout, identity file beside the package.
    """
    from jstack_host import sourcestamp
    from test_jremote_sourcestamp import bundle_tree
    monkeypatch.delenv("JSTACK_INSTALL_URL", raising=False)
    monkeypatch.setattr(sourcestamp, "_PKG", bundle_tree(tmp_path))
    monkeypatch.setattr(sourcestamp, "_stamp", None)
    assert (adopt_offline._installer_url()
            == "https://raw.githubusercontent.com/jenyalebid/jStack/main/install.sh")
