"""`jstack-host attach` — the one deliberate step that makes this Mac a managed
hub of another.

Two things are pinned here, and the split matters:

  1. **The orchestration**, against a recorder: a host code is redeemed, the
     leaf bundle in the response is written out with the right permissions, the
     installer is invoked from inside it, and the token the parent handed back
     is recorded so a re-attach re-keys instead of multiplying rows. The refusals
     are pinned too — a device code, a parent with no mesh, a bad code, a lockout,
     a bad host key, a short bundle — because each is a place the command could
     claim a success it did not earn.

  2. **The bundle we write is one `install_leaf.sh` actually accepts**, against
     the real shipped script under `LEAF_DEST` (its own documented test seam),
     with a mock `launchctl` and stub wireguard binaries. Nothing is brought up
     and no root is used; the assertable part is the installed layout, which is
     exactly where the script itself stops in a test.

The HTTP POST and the installer subprocess are injected, so none of this reaches
a network or the real `/Library`.
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from jstack_host import attach_parent, grants, hostenv, tunnel


@pytest.fixture(autouse=True)
def _own_state_dir(tmp_path, monkeypatch):
    """Point this host's state dir at a temp one for every test — attach writes
    the parent record and (by default) the bundle in here, and neither belongs
    in the running machine's live state."""
    monkeypatch.setenv("JREMOTE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("JREMOTE_HOST_ID", "test-host-0001")


# A host key this machine can legitimately present — the shape host_id() mints.
GOOD_KEY = "test-host-0001"


def _bundle(scripts_from_disk=False) -> dict:
    """A leaf bundle shaped like the one `tunnel._read_bundle` returns.

    `scripts_from_disk=True` reads the real leaf scripts the payload ships, so
    the integration test writes and runs the actual `install_leaf.sh`; the
    default uses placeholders, which is all the orchestration tests inspect.
    """
    wg = hostenv.peer_script().parent
    leaf_env = ("WG_ADDR=10.66.0.7/32\nWG_SUBNET=10.66.0.0/24\nWG_HUB=10.66.0.1\n")
    if scripts_from_disk:
        return {
            "jrleaf.conf": "[Interface]\nPrivateKey = KEY\n[Peer]\n"
                           "PublicKey = PUB\nEndpoint = hub.example.com:51820\n"
                           "AllowedIPs = 10.66.0.0/24\n",
            "leaf.env": leaf_env,
            "install_leaf.sh": (wg / "install_leaf.sh").read_text(),
            "wg_up.sh": (wg / "wg_up.sh").read_text(),
            "wg_leaf_watch.sh": (wg / "wg_leaf_watch.sh").read_text(),
            "README.md": "# leaf\n",
        }
    return {name: f"contents of {name}\n" for name in tunnel.LEAF_FILES}


def _host_response(bundle=None) -> dict:
    """A successful host-code redeem, the shape enrolment.redeem returns."""
    return {
        "device": {"id": "dev_new", "name": "studio", "revoked": False},
        "token": "TOKEN-FROM-PARENT",
        "tunnel": {"device": "studio", "bundle": bundle or _bundle(),
                   "created": True},
        "tunnel_note": "",
        "kind": "host",
        "host": {"key": GOOD_KEY, "name": "studio", "address": "10.66.0.7",
                 "port": 9090, "deleted": False},
        "superseded": False,
        # #62's reverse direction: the grant the parent issued this leaf, and
        # the parent's own identity, so the leaf can tile and mint on it.
        "leaf_grant": "jrg1.parentgrant.secret",
        "parent_identity": {"key": "parent-host-0001", "name": "Studio",
                            "address": "10.66.0.1", "port": 9090},
    }


def _recording_poster(status, body, sink):
    """A poster that records (url, payload) and returns a canned answer."""
    def poster(url, payload):
        sink.append((url, payload))
        return status, body
    return poster


def _ok_runner(sink):
    """A runner that records the argv/cwd/env and reports success."""
    def runner(argv, cwd=None, env=None, **kw):
        sink.append({"argv": argv, "cwd": cwd, "env": env})
        return subprocess.CompletedProcess(argv, 0, stdout="installed", stderr="")
    return runner


def _exploding(*a, **k):
    raise AssertionError("must not be called")


# ── the orchestration ──────────────────────────────────────────────────────

def test_attach_redeems_writes_the_bundle_and_runs_the_installer(tmp_path):
    posts, runs = [], []
    dest = tmp_path / "bundle"
    result = attach_parent.attach(
        "ABCD-1234", "http://studio.local:9090", host_key=GOOD_KEY,
        dest_dir=dest, poster=_recording_poster(200, _host_response(), posts),
        runner=_ok_runner(runs), sudo=False)

    # It POSTed to the parent's one redeem path, carrying this machine's key.
    url, payload = posts[0]
    assert url == "http://studio.local:9090/api/jremote/v1/enrolment/redeem"
    # …and the grant it minted on itself, so the parent can let devices in here
    # without a second code (grants.py). Checked by shape, not by value: the
    # secret is fresh every attach and the point is that one was sent.
    grant = payload.pop("grant_token")
    assert grant.startswith("jrg1.") and len(grant.split(".")) == 3
    assert grants.authenticate(grant) == "http://studio.local:9090"
    # …and its shell identity, minted here — pubkey only, never the private
    # half (#131). Shape-checked like the grant; the key is fresh per machine.
    import getpass
    assert payload.pop("ssh_pubkey").startswith("ssh-ed25519 ")
    assert payload.pop("ssh_user") == getpass.getuser()
    assert payload == {"code": "ABCD-1234", "host_key": GOOD_KEY, "port": 9090}

    # Every leaf file landed, with the mode its job needs.
    for name in tunnel.LEAF_FILES:
        assert (dest / name).is_file(), f"bundle missing {name}"
    assert stat.S_IMODE((dest / "jrleaf.conf").stat().st_mode) == 0o600
    assert stat.S_IMODE((dest / "install_leaf.sh").stat().st_mode) == 0o755

    # The installer ran from inside the bundle, via bash, no sudo asked for.
    run = runs[0]
    assert run["argv"] == ["bash", str(dest / "install_leaf.sh")]
    assert run["cwd"] == str(dest)

    assert result["bundle_dir"] == str(dest)
    assert result["superseded"] is False


def test_the_parent_token_is_recorded_0600_for_a_later_re_attach(tmp_path):
    attach_parent.attach(
        "ABCD-1234", "http://studio.local:9090", host_key=GOOD_KEY,
        dest_dir=tmp_path / "b", poster=_recording_poster(200, _host_response(), []),
        runner=_ok_runner([]), sudo=False)

    rec_path = hostenv.state_dir() / attach_parent.PARENT_RECORD
    assert rec_path.is_file()
    assert stat.S_IMODE(rec_path.stat().st_mode) == 0o600
    rec = json.loads(rec_path.read_text())
    assert rec["token"] == "TOKEN-FROM-PARENT"
    assert rec["parent_url"] == "http://studio.local:9090"
    assert rec["device_id"] == "dev_new"


def test_attach_records_the_parent_identity_and_holds_its_grant(tmp_path):
    """#62: the leaf keeps what it needs to show its own devices a tile for the
    parent — the parent's id, name and mesh address in the record, and the
    grant the parent issued held in the roster so `mint_on` has something to
    spend. The grant is held, never written into the record beside the token: a
    credential belongs in the grant store, not a plaintext file."""
    attach_parent.attach(
        "ABCD-1234", "http://studio.local:9090", host_key=GOOD_KEY,
        dest_dir=tmp_path / "b", poster=_recording_poster(200, _host_response(), []),
        runner=_ok_runner([]), sudo=False)

    rec = attach_parent.parent_record()
    assert rec["parent_key"] == "parent-host-0001"
    assert rec["parent_name"] == "Studio"
    assert rec["parent_address"] == "10.66.0.1"
    assert rec["parent_port"] == 9090

    assert grants.held("parent-host-0001") == "jrg1.parentgrant.secret"
    body = (hostenv.state_dir() / attach_parent.PARENT_RECORD).read_text()
    assert "parentgrant" not in body


def test_a_parent_on_an_older_build_leaves_no_grant_or_identity(tmp_path):
    """A parent from before #62 sends neither field. The leaf records the token
    as it always did and holds no grant — the pre-#62 behaviour, a machine on
    the mesh whose devices simply do not see home, not a crash on a missing
    key."""
    older = {k: v for k, v in _host_response().items()
             if k not in ("leaf_grant", "parent_identity")}
    attach_parent.attach(
        "ABCD-1234", "http://studio.local:9090", host_key=GOOD_KEY,
        dest_dir=tmp_path / "b", poster=_recording_poster(200, older, []),
        runner=_ok_runner([]), sudo=False)

    rec = attach_parent.parent_record()
    assert rec["parent_key"] == "" and rec["token"] == "TOKEN-FROM-PARENT"
    assert grants.held("parent-host-0001") == ""


def test_a_re_attach_presents_the_prior_token_so_the_parent_re_keys(tmp_path):
    state = hostenv.state_dir()
    state.mkdir(parents=True, exist_ok=True)
    (state / attach_parent.PARENT_RECORD).write_text(
        json.dumps({"token": "OLD-TOKEN", "device_id": "dev_old",
                    "parent_url": "http://studio.local:9090"}))

    posts = []
    attach_parent.attach(
        "ABCD-1234", "http://studio.local:9090", host_key=GOOD_KEY,
        dest_dir=tmp_path / "b", poster=_recording_poster(200, _host_response(), posts),
        runner=_ok_runner([]), sudo=False)

    assert posts[0][1].get("device_token") == "OLD-TOKEN"


def test_a_device_code_is_refused_before_anything_is_written(tmp_path):
    dev_resp = {**_host_response(), "kind": "device", "tunnel": None}
    dest = tmp_path / "b"
    runs = []
    with pytest.raises(attach_parent.AttachError, match="device, not a machine"):
        attach_parent.attach(
            "ABCD-1234", "http://studio.local:9090", host_key=GOOD_KEY,
            dest_dir=dest, poster=_recording_poster(200, dev_resp, []),
            runner=_ok_runner(runs), sudo=False)
    assert not dest.exists()
    assert not runs


def test_a_parent_with_no_mesh_is_refused_and_says_why(tmp_path):
    no_mesh = {**_host_response(), "tunnel": None,
               "tunnel_note": "this host does not run the tunnel"}
    runs = []
    with pytest.raises(attach_parent.AttachError, match="no mesh to join"):
        attach_parent.attach(
            "ABCD-1234", "http://studio.local:9090", host_key=GOOD_KEY,
            dest_dir=tmp_path / "b", poster=_recording_poster(200, no_mesh, []),
            runner=_ok_runner(runs), sudo=False)
    assert not runs


def test_an_incomplete_bundle_is_refused_naming_the_missing_file(tmp_path):
    short = _bundle()
    del short["leaf.env"]
    resp = _host_response(bundle=short)
    dest = tmp_path / "b"
    runs = []
    with pytest.raises(attach_parent.AttachError, match="leaf.env"):
        attach_parent.attach(
            "ABCD-1234", "http://studio.local:9090", host_key=GOOD_KEY,
            dest_dir=dest, poster=_recording_poster(200, resp, []),
            runner=_ok_runner(runs), sudo=False)
    assert not runs


def test_a_bad_code_surfaces_the_parents_refusal(tmp_path):
    with pytest.raises(attach_parent.AttachError, match="not valid"):
        attach_parent.attach(
            "ABCD-1234", "http://studio.local:9090", host_key=GOOD_KEY,
            dest_dir=tmp_path / "b",
            poster=_recording_poster(401, {"detail": "that enrolment code is "
                                           "not valid — ask for a new one"}, []),
            runner=_exploding, sudo=False)


def test_a_lockout_is_reported_as_rate_limiting(tmp_path):
    with pytest.raises(attach_parent.AttachError, match="rate-limiting"):
        attach_parent.attach(
            "ABCD-1234", "http://studio.local:9090", host_key=GOOD_KEY,
            dest_dir=tmp_path / "b",
            poster=_recording_poster(429, {"detail": "locked out"}, []),
            runner=_exploding, sudo=False)


def test_a_bad_host_key_never_reaches_the_network(tmp_path):
    with pytest.raises(attach_parent.AttachError, match="usable host id"):
        attach_parent.attach(
            "ABCD-1234", "http://studio.local:9090", host_key="",
            dest_dir=tmp_path / "b", poster=_exploding, runner=_exploding)


def test_a_non_http_parent_is_refused(tmp_path):
    with pytest.raises(attach_parent.AttachError, match="http"):
        attach_parent.attach(
            "ABCD-1234", "studio.local:9090", host_key=GOOD_KEY,
            dest_dir=tmp_path / "b", poster=_exploding, runner=_exploding)


def test_an_installer_failure_is_surfaced_with_the_bundle_path(tmp_path):
    def failing(argv, cwd=None, env=None, **kw):
        return subprocess.CompletedProcess(argv, 1, stdout="",
                                           stderr="run with sudo")
    dest = tmp_path / "b"
    with pytest.raises(attach_parent.AttachError, match="run with sudo"):
        attach_parent.attach(
            "ABCD-1234", "http://studio.local:9090", host_key=GOOD_KEY,
            dest_dir=dest, poster=_recording_poster(200, _host_response(), []),
            runner=failing, sudo=False)


# ── the bundle installs against the real script ─────────────────────────────

def test_the_bundle_we_write_installs_with_the_real_install_leaf_script(tmp_path):
    """End to end against the shipped `install_leaf.sh`, its own `LEAF_DEST`
    seam, a mock launchctl and stub wg binaries — the proof that what attach
    writes is a bundle the installer accepts, not just six files with the right
    names."""
    leaf_dest = tmp_path / "root"
    mock_launchctl = tmp_path / "launchctl"
    mock_launchctl.write_text("#!/bin/bash\nexit 0\n")
    mock_launchctl.chmod(0o755)

    env = {**os.environ,
           "LEAF_DEST": str(leaf_dest),
           "LAUNCHCTL": str(mock_launchctl),
           "WG_GO": "/usr/bin/true",
           "WG": "/usr/bin/true"}

    result = attach_parent.attach(
        "ABCD-1234", "http://studio.local:9090", host_key=GOOD_KEY,
        dest_dir=tmp_path / "bundle",
        poster=_recording_poster(200, _host_response(_bundle(scripts_from_disk=True)), []),
        runner=subprocess.run, sudo=False, install_env=env)

    # The installer laid down its whole layout under LEAF_DEST.
    assert (leaf_dest / "etc/wireguard/jrleaf.conf").is_file()
    assert (leaf_dest / "Library/LaunchDaemons/com.jremote.leaf.plist").is_file()
    assert (leaf_dest / "Library/LaunchDaemons/com.jremote.leaf-watch.plist").is_file()
    app = leaf_dest / "Library/Application Support/jRemote Leaf"
    assert (app / "wg_up.sh").is_file() and (app / "leaf.env").is_file()
    # This bundle's leaf.env predates WG_MTU — the installer must still pin
    # the clamp into the daemon, or re-installing an old bundle reverts the
    # leaf to the 1420 default that stalls constrained paths (jStack#54).
    plist = (leaf_dest / "Library/LaunchDaemons/com.jremote.leaf.plist").read_text()
    assert "<key>WG_MTU</key>" in plist
    assert "<string>1240</string>" in plist
    assert "installed com.jremote.leaf" in result["installer_output"]


# ── the CLI wires the command to the module ─────────────────────────────────

@pytest.mark.parametrize("app_status", [0, 1])
def test_the_cli_passes_this_machines_id_and_reads_the_mode_back(monkeypatch, app_status):
    from jstack_host import cli, mode

    monkeypatch.setattr(cli, "_adopt", lambda a: None)
    monkeypatch.setattr(mode, "current", lambda: {
        "mode": "managed", "live": True, "note": "attached to a parent hub"})

    seen = {}

    def fake_attach(code, parent, *, host_key, port):
        seen.update(code=code, parent=parent, host_key=host_key, port=port)
        return {"host": {"name": "studio"}, "superseded": False,
                "parent_url": parent, "bundle_dir": "/x"}

    monkeypatch.setattr(attach_parent, "attach", fake_attach)
    introductions = []
    monkeypatch.setattr(cli, "_hand_to_app",
                        lambda row: introductions.append(row) or app_status)

    args = cli.build_parser().parse_args(
        ["attach", "ABCD-1234", "--parent", "http://studio.local:9090"])
    assert args.fn(args) == app_status
    assert len(introductions) == 1
    assert introductions[0]["kind"] == "local"
    assert seen["code"] == "ABCD-1234"
    assert seen["parent"] == "http://studio.local:9090"
    assert seen["host_key"] == hostenv.host_id()   # the machine's own id
    assert seen["port"] == 9090


# --- a parent only the tunnel can reach ------------------------------------

@pytest.mark.parametrize("current", [False, True])
def test_capabilities_requires_the_installed_app_to_support_managed_mode(tmp_path, monkeypatch, capsys, current):
    import plistlib
    from types import SimpleNamespace
    from jstack_host import cli, desk
    app = tmp_path / "app" / "Contents"
    app.mkdir(parents=True)
    (app / "Info.plist").write_bytes(plistlib.dumps({"JRManagedAccess": current}))
    monkeypatch.setattr(desk, "APP", str(app.parent))
    assert cli._cmd_capabilities(SimpleNamespace()) == 0
    caps = capsys.readouterr().out.splitlines()
    assert "managed-access-v1" in caps
    assert ("managed-app-v1" in caps) is current

def test_attach_refuses_a_mesh_parent_when_this_machine_is_not_a_peer(monkeypatch):
    """The reported bug: `--parent http://10.66.0.1:9090` from a Mac that is not
    on the mesh. Attaching is what joins the mesh, so that address cannot answer
    until after the attach it is gating has already worked. Unchecked it spent
    30s in httpx and returned `could not reach the parent ... timed out`, which
    reads as a hub that is down — and sends someone to restart a healthy hub."""
    monkeypatch.setattr(attach_parent.addresses, "_inet_addrs",
                        lambda: ["192.168.0.44", "127.0.0.1"])

    def _never_called(*a, **k):  # the point is that no request is made
        raise AssertionError("attach posted to an address it could not reach")

    with pytest.raises(attach_parent.AttachError) as e:
        attach_parent.attach("ABCD-1234", "http://10.66.0.1:9090",
                             host_key="work-mac-key", poster=_never_called)
    msg = str(e.value)
    assert "not on the mesh yet" in msg
    assert "studio.local" in msg, "the refusal must name what to use instead"


def test_attach_allows_a_mesh_parent_once_this_machine_is_a_peer(monkeypatch):
    """Re-attaching an existing leaf, or moving it to another parent, is a
    legitimate in-tunnel conversation. The guard must not break it."""
    monkeypatch.setattr(attach_parent.addresses, "_inet_addrs",
                        lambda: ["10.66.0.7", "192.168.0.44"])
    reached = {}

    def _poster(url, payload):
        reached["url"] = url
        raise attach_parent.AttachError("stop here — the guard let it through")

    with pytest.raises(attach_parent.AttachError):
        attach_parent.attach("ABCD-1234", "http://10.66.0.1:9090",
                             host_key="work-mac-key", poster=_poster)
    assert reached.get("url", "").startswith("http://10.66.0.1:9090"), (
        "a machine already on the mesh was refused its own parent")


def test_a_named_parent_is_left_to_dns(monkeypatch):
    """`studio.local` is not an address this can classify, and refusing names
    would break the recommended form the refusal itself prints."""
    monkeypatch.setattr(attach_parent.addresses, "_inet_addrs", lambda: ["192.168.0.44"])
    attach_parent._refuse_a_parent_only_the_tunnel_can_reach("http://studio.local:9090")
