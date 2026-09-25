"""Shell access on a managed machine — the leaf-side primitives.

Four things are pinned, each a place a shell grant could rot into a lie:

  · the machine's SSH identity is minted once, private key never leaving 0600,
    pubkey attributable to the machine that holds it;
  · the managed block in `authorized_keys` is *owned* — rewritten in place,
    never appended, gone entirely when the grant list empties — and the user's
    own keys around it are never touched;
  · the root steps (Remote Login, the sudoers drop-in) are graded like
    detach's: reported per step, and Remote Login is only ever restored to
    what it was before the grant turned it on;
  · disable is the full reverse of enable, because a detached machine that
    still answers `sudo -n true` for a revoked parent is the failure mode
    this whole module exists to prevent.

Everything runs against a fake root (`root=tmp_path`), a recording runner and
a temp state dir. The identity tests use the real `ssh-keygen`.
"""

import os
import stat
from pathlib import Path

import pytest

from jstack_host import hostenv, shell_access


@pytest.fixture(autouse=True)
def _own_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("JREMOTE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("JREMOTE_HOST_ID", "test-host-0001")
    # Redeeming a shell-bearing enrolment writes the hub's own ~/.ssh/config;
    # HOME must never resolve to the real home under test.
    monkeypatch.setenv("HOME", str(tmp_path / "test-home"))


class _Runner:
    """Stands in for subprocess.run — records argv, answers from a script."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        out = ""
        for needle, canned in self.answers.items():
            if needle in argv:
                out = canned
        return type("P", (), {"returncode": 0, "stdout": out, "stderr": ""})()


def _mode(path):
    return stat.S_IMODE(Path(path).stat().st_mode)


# ── the machine's own identity ──────────────────────────────────────────────

def test_identity_is_minted_once_and_the_private_key_never_leaves_0600():
    pub = shell_access.identity()
    assert pub.startswith("ssh-ed25519 ")
    key = hostenv.state_dir() / shell_access.KEY_FILE
    assert _mode(key) == 0o600
    assert _mode(key.parent) == 0o700

    before = key.read_bytes()
    assert shell_access.identity() == pub, "a second call must not rotate"
    assert key.read_bytes() == before


def test_the_pubkey_names_the_machine_that_holds_it():
    """The comment field is the machine's own host id — an authorized_keys
    line on some other machine has to be attributable to exactly one leaf, or
    revoking that leaf cannot find its line."""
    assert shell_access.identity().split()[2] == "test-host-0001"


def test_public_key_answers_empty_when_no_identity_was_ever_minted():
    assert shell_access.public_key() == ""
    minted = shell_access.identity()
    assert shell_access.public_key() == minted


# ── the managed authorized_keys block ───────────────────────────────────────

LINE_A = "ssh-ed25519 AAAAexampleA parent-host-0001"
LINE_B = "ssh-ed25519 AAAAexampleB sibling-host-0002"


def test_the_block_is_written_inside_marks_preserving_the_users_own_keys(tmp_path):
    ak = tmp_path / ".ssh" / "authorized_keys"
    ak.parent.mkdir(mode=0o700)
    ak.write_text("ssh-rsa USEROWNKEY someone@laptop\n")

    shell_access.write_authorized_block(ak, [LINE_A, LINE_B])

    body = ak.read_text()
    assert "ssh-rsa USEROWNKEY someone@laptop" in body
    assert body.index(shell_access.MARK_BEGIN) < body.index(LINE_A)
    assert body.index(LINE_B) < body.index(shell_access.MARK_END)
    assert _mode(ak) == 0o600


def test_rewriting_the_block_replaces_it_rather_than_appending(tmp_path):
    ak = tmp_path / "authorized_keys"
    shell_access.write_authorized_block(ak, [LINE_A, LINE_B])
    shell_access.write_authorized_block(ak, [LINE_A])

    body = ak.read_text()
    assert LINE_B not in body, "a revoked grant's key survived the rewrite"
    assert body.count(shell_access.MARK_BEGIN) == 1


def test_an_empty_grant_list_removes_the_block_entirely(tmp_path):
    ak = tmp_path / "authorized_keys"
    ak.write_text("ssh-rsa USEROWNKEY someone@laptop\n")
    shell_access.write_authorized_block(ak, [LINE_A])
    shell_access.write_authorized_block(ak, [])

    body = ak.read_text()
    assert shell_access.MARK_BEGIN not in body
    assert body.strip() == "ssh-rsa USEROWNKEY someone@laptop"


def test_a_missing_authorized_keys_is_created_with_the_ssh_dir_locked_down(tmp_path):
    ak = tmp_path / "home" / ".ssh" / "authorized_keys"
    shell_access.write_authorized_block(ak, [LINE_A])
    assert _mode(ak) == 0o600
    assert _mode(ak.parent) == 0o700


def test_read_block_returns_exactly_the_managed_lines(tmp_path):
    ak = tmp_path / "authorized_keys"
    ak.write_text("ssh-rsa USEROWNKEY someone@laptop\n")
    shell_access.write_authorized_block(ak, [LINE_A, LINE_B])
    assert shell_access.read_authorized_block(ak) == [LINE_A, LINE_B]
    assert shell_access.read_authorized_block(tmp_path / "absent") == []


# ── enable: the joiner's root step ──────────────────────────────────────────

def _step(steps, name):
    return next((s for s in steps if s["step"] == name), None)


def test_enable_turns_remote_login_on_and_records_that_it_did():
    runner = _Runner(answers={"-getremotelogin": "Remote Login: Off\n"})
    steps = shell_access.enable(runner=runner, sudo=False)

    assert any("-setremotelogin" in c and "on" in c for c in runner.calls)
    assert _step(steps, "remote-login")["ok"] is True

    # Recorded, so disable knows whether Off is the state to restore.
    assert shell_access.enabled_remote_login() is True


def test_a_grant_lays_no_sudoers_drop_in(tmp_path):
    """A grant carries no standing sudo: shell is the enrolled account's own
    authority, root on a granted machine is asked for per use."""
    runner = _Runner(answers={"-getremotelogin": "Remote Login: Off\n"})
    steps = shell_access.enable(runner=runner, sudo=False)
    assert _step(steps, "sudoers") is None
    assert not (tmp_path / shell_access.SUDOERS_PATH).exists()
    assert not any("sudoers" in " ".join(map(str, c)) for c in runner.calls)


def test_enable_leaves_remote_login_alone_when_it_was_already_on():
    runner = _Runner(answers={"-getremotelogin": "Remote Login: On\n"})
    shell_access.enable(runner=runner, sudo=False)

    assert not any("-setremotelogin" in c for c in runner.calls)
    assert shell_access.enabled_remote_login() is False


def test_enable_runs_systemsetup_under_sudo():
    runner = _Runner(answers={"-getremotelogin": "Remote Login: Off\n"})
    shell_access.enable(runner=runner, sudo=True)
    settings = [c for c in runner.calls if "-setremotelogin" in c]
    assert settings and settings[0][0] == "sudo"


# ── disable: detach's reverse ───────────────────────────────────────────────

def test_disable_restores_remote_login_only_if_enable_turned_it_on(tmp_path):
    runner = _Runner(answers={"-getremotelogin": "Remote Login: Off\n"})
    shell_access.enable(runner=runner, sudo=False)

    off = _Runner()
    steps = shell_access.disable(runner=off, sudo=False,
                                 root=tmp_path,
                                 authorized_keys=tmp_path / "authorized_keys")
    assert any("-setremotelogin" in c and "off" in c for c in off.calls)
    assert _step(steps, "remote-login")["ok"] is True


def test_disable_leaves_remote_login_up_when_it_predates_the_grant(tmp_path):
    runner = _Runner(answers={"-getremotelogin": "Remote Login: On\n"})
    shell_access.enable(runner=runner, sudo=False)

    off = _Runner()
    steps = shell_access.disable(runner=off, sudo=False,
                                 root=tmp_path,
                                 authorized_keys=tmp_path / "authorized_keys")
    assert not any("-setremotelogin" in c for c in off.calls)
    assert "was on before" in _step(steps, "remote-login")["note"]


def test_disable_removes_the_sudoers_drop_in_the_block_and_the_identity(tmp_path):
    shell_access.identity()
    ak = tmp_path / "authorized_keys"
    ak.write_text("ssh-rsa USEROWNKEY someone@laptop\n")
    shell_access.write_authorized_block(ak, [LINE_A])
    # Laid by hand: no build grants sudo any more, but disable keeps sweeping
    # the drop-in an earlier build left behind.
    dropin = tmp_path / shell_access.SUDOERS_PATH
    dropin.parent.mkdir(parents=True, exist_ok=True)
    dropin.write_text("alex ALL=(ALL) NOPASSWD: ALL\n")
    runner = _Runner(answers={"-getremotelogin": "Remote Login: Off\n"})
    shell_access.enable(runner=runner, sudo=False)

    steps = shell_access.disable(runner=_Runner(), sudo=False,
                                 root=tmp_path, authorized_keys=ak)

    assert not dropin.exists()
    assert _step(steps, "sudoers")["ok"] is True
    assert shell_access.MARK_BEGIN not in ak.read_text()
    assert "USEROWNKEY" in ak.read_text()
    assert _step(steps, "authorized-keys")["ok"] is True
    assert shell_access.public_key() == ""
    assert _step(steps, "identity")["ok"] is True


def test_disable_on_a_machine_that_never_had_a_grant_is_clean(tmp_path):
    steps = shell_access.disable(runner=_Runner(), sudo=False,
                                 root=tmp_path,
                                 authorized_keys=tmp_path / "authorized_keys")
    assert all(s["ok"] for s in steps)


# ── the convenience ssh config ──────────────────────────────────────────────

def test_ssh_config_lists_each_peer_and_survives_a_rewrite(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("Host mine\n  HostName example.com\n")
    peers = [{"name": "work-main", "address": "10.66.0.16", "user": "alex"},
             {"name": "work-temp", "address": "10.66.0.21", "user": "alex"}]

    shell_access.write_ssh_config(cfg, peers, key_path=Path("/k/id_jremote"))
    body = cfg.read_text()
    assert "Host mine" in body, "the user's own entry was clobbered"
    for peer in peers:
        assert f"Host {peer['name']}" in body
        assert f"HostName {peer['address']}" in body
    assert "IdentityFile /k/id_jremote" in body
    assert "ServerAliveInterval" in body

    shell_access.write_ssh_config(cfg, peers[:1], key_path=Path("/k/id_jremote"))
    body = cfg.read_text()
    assert "work-temp" not in body, "a removed peer's entry survived"
    assert "Host mine" in body


def test_a_peer_name_that_cannot_be_an_ssh_alias_is_refused(tmp_path):
    """Host aliases land unquoted in a config ssh parses — a name with
    whitespace or a comment char would smuggle directives into it."""
    with pytest.raises(shell_access.ShellAccessError):
        shell_access.write_ssh_config(
            tmp_path / "config",
            [{"name": "evil name\n  ProxyCommand x", "address": "10.66.0.9",
              "user": "alex"}])


# ── what a machine may claim as its pubkey ──────────────────────────────────

def test_valid_pubkey_takes_real_key_lines_and_refuses_smuggling():
    """These lines are written verbatim into other machines' authorized_keys.
    A newline is a second key; an options prefix is a command."""
    assert shell_access.valid_pubkey(LINE_A)
    assert shell_access.valid_pubkey("ssh-ed25519 AAAAexampleA")
    assert not shell_access.valid_pubkey("")
    assert not shell_access.valid_pubkey(LINE_A + "\n" + LINE_B)
    assert not shell_access.valid_pubkey(
        'command="rm -rf /" ssh-ed25519 AAAAexampleA x')
    assert not shell_access.valid_pubkey("ssh-ed25519 AAAA" + "x" * 2000)


# ── the adoption handshake carries it ───────────────────────────────────────

@pytest.fixture
def hub(tmp_path, monkeypatch):
    """A hub's store, wired the way the enrolment tests wire theirs, with the
    live tunnel and the announce path both stubbed out."""
    from jstack_host import devices, enrolment, tunnel
    from jstack_host.store import SessionStore
    s = SessionStore(db_path=tmp_path / "hub.sqlite")
    monkeypatch.setattr(devices, "_store", lambda: s)
    monkeypatch.setattr(enrolment, "_store", lambda: s)
    monkeypatch.setattr("jstack_host.store.get_store", lambda: s)
    monkeypatch.setattr(tunnel, "can_pair", lambda: False)
    monkeypatch.setattr("jstack_host.hostenv.security_alert", lambda *a: None)
    return s


def _redeem_host(hub, host_key, **kw):
    from jstack_host import enrolment
    out = enrolment.mint_code("work-mac", "", 600, kind=enrolment.KIND_HOST)
    return enrolment.redeem(enrolment.normalize(out["code"]), "198.51.100.4",
                            host_key=host_key, port=9090, **kw)


LEAF_LINE = "ssh-ed25519 AAAAleafkey work-key1"


def test_redeem_stores_the_machines_key_and_answers_with_the_hubs(hub):
    result = _redeem_host(hub, "work-key1",
                          ssh_pubkey=LEAF_LINE, ssh_user="alex")

    row = hub.host_row("work-key1")
    assert row["shell_pubkey"] == LEAF_LINE
    assert row["shell_user"] == "alex"
    # The hub's own identity comes back as the one line the leaf must
    # authorize — minted on first adoption, stable ever after.
    assert result["shell"]["authorized"] == [shell_access.identity()]
    assert result["shell"]["peers"] == []


def test_redeem_carries_granted_siblings_in_both_directions(hub):
    hub.upsert_host("sib-key1", "Work Temp", "10.66.0.21", 9090)
    hub.set_host_shell("sib-key1", LINE_B, "alex")
    hub.set_shell_grant("sib-key1", "work-key1", True)
    hub.set_shell_grant("work-key1", "sib-key1", True)

    result = _redeem_host(hub, "work-key1",
                          ssh_pubkey=LEAF_LINE, ssh_user="alex")

    assert LINE_B in result["shell"]["authorized"]
    assert result["shell"]["peers"] == [
        {"name": "work-temp", "address": "10.66.0.21", "user": "alex"}]


def test_a_flipped_off_grant_stops_riding_the_handshake(hub):
    hub.upsert_host("sib-key1", "Work Temp", "10.66.0.21", 9090)
    hub.set_host_shell("sib-key1", LINE_B, "alex")
    hub.set_shell_grant("sib-key1", "work-key1", True)
    hub.set_shell_grant("sib-key1", "work-key1", False)

    result = _redeem_host(hub, "work-key1",
                          ssh_pubkey=LEAF_LINE, ssh_user="alex")
    assert LINE_B not in result["shell"]["authorized"]


def test_an_old_leaf_sending_no_pubkey_gets_no_shell_and_loses_nothing(hub):
    result = _redeem_host(hub, "work-key1")
    assert result["shell"] == {}
    assert hub.host_row("work-key1")["shell_pubkey"] == ""
    assert result["token"], "the enrolment itself must be untouched"


def test_a_pubkey_that_would_smuggle_options_is_dropped_not_stored(hub):
    result = _redeem_host(hub, "work-key1",
                          ssh_pubkey='command="x" ssh-ed25519 AAAA k',
                          ssh_user="alex")
    assert hub.host_row("work-key1")["shell_pubkey"] == ""
    assert result["shell"] == {}


# ── the hub's grant flip, applied live ──────────────────────────────────────

@pytest.fixture
def fleet(hub):
    """Two adopted machines with shell material and held grants — the smallest
    fleet a leaf→leaf flip can exist on. Nothing keys on there being two."""
    hub.upsert_host("src-key1", "Work Main", "10.66.0.16", 9090)
    hub.upsert_host("dst-key1", "Work Temp", "10.66.0.21", 9090)
    hub.set_host_shell("src-key1", LINE_A, "alex")
    hub.set_host_shell("dst-key1", LINE_B, "alex")
    hub.put_host_grant("src-key1", "jrg1.aa.src-secret")
    hub.put_host_grant("dst-key1", "jrg1.bb.dst-secret")
    return hub


def _wire(log, down=()):
    """A poster that answers both legs of a poke — the delegated mint and the
    refresh — and plays dead for any address in `down`."""
    from jstack_host import grants

    def poster(url, payload, token):
        log.append((url, payload, token))
        if any(host in url for host in down):
            return 503, {"detail": "unreachable"}
        if url.endswith(grants.MINT_PATH):
            return 200, {"device": {"name": "shell refresh"},
                         "token": "jr1.projected.tok"}
        return 200, {"steps": [{"step": "authorized-keys", "ok": True,
                                "note": ""}]}
    return poster


def test_flip_records_the_grant_and_pokes_both_machines(fleet):
    from jstack_host import shell_grants
    log = []
    out = shell_grants.flip("src-key1", "dst-key1", True, poster=_wire(log))

    assert fleet.shell_sources_for("dst-key1") == ["src-key1"]
    refreshes = [u for u, _, _ in log if u.endswith(shell_grants.REFRESH_PATH)]
    assert any("10.66.0.21" in u for u in refreshes), "dst: its keys changed"
    assert any("10.66.0.16" in u for u in refreshes), "src: its reach changed"
    assert len(out["steps"]) == 2 and all(s["ok"] for s in out["steps"])


def test_flip_off_removes_the_row_and_still_pokes_both(fleet):
    from jstack_host import shell_grants
    shell_grants.flip("src-key1", "dst-key1", True, poster=_wire([]))
    log = []
    shell_grants.flip("src-key1", "dst-key1", False, poster=_wire(log))

    assert fleet.shell_sources_for("dst-key1") == []
    assert len([u for u, _, _ in log
                if u.endswith(shell_grants.REFRESH_PATH)]) == 2


def test_an_unreachable_machine_keeps_the_flip_and_says_so(fleet):
    from jstack_host import shell_grants
    out = shell_grants.flip("src-key1", "dst-key1", True,
                            poster=_wire([], down=("10.66.0.21",)))

    assert fleet.shell_sources_for("dst-key1") == ["src-key1"], \
        "the store is the truth; the poke is best-effort"
    failed = [s for s in out["steps"] if not s["ok"]]
    assert len(failed) == 1 and "dst-key1" in failed[0]["step"]


def test_flip_refuses_unknown_machines_and_self_grants(fleet):
    from jstack_host import shell_grants
    with pytest.raises(shell_grants.ShellGrantError):
        shell_grants.flip("nobody-key", "dst-key1", True, poster=_wire([]))
    with pytest.raises(shell_grants.ShellGrantError):
        shell_grants.flip("src-key1", "src-key1", True, poster=_wire([]))
    assert fleet.shell_sources_for("dst-key1") == []


def test_the_parent_answers_a_pull_with_the_machines_current_set(fleet):
    from jstack_host import shell_grants
    shell_grants.flip("src-key1", "dst-key1", True, poster=_wire([]))

    shell = shell_grants.leaf_shell("dst-key1")
    assert shell["authorized"] == [shell_access.identity(), LINE_A]
    assert shell["peers"] == []
    src = shell_grants.leaf_shell("src-key1")
    assert src["peers"] == [{"name": "work-temp", "address": "10.66.0.21",
                             "user": "alex"}]


def test_a_machine_that_never_presented_a_key_pulls_nothing(fleet):
    from jstack_host import shell_grants
    fleet.upsert_host("old-key1", "Old Mac", "10.66.0.30", 9090)
    assert shell_grants.leaf_shell("old-key1") == {}


# ── the hub's own ssh config ────────────────────────────────────────────────

def test_the_hub_writes_a_config_entry_for_every_machine_it_can_reach(fleet, tmp_path):
    from jstack_host import shell_grants
    fleet.upsert_host("old-key1", "Old Mac", "10.66.0.30", 9090)
    path = tmp_path / "hub-ssh-config"
    shell_grants.refresh_hub_config(path)

    text = path.read_text()
    assert "Host work-main" in text and "Host work-temp" in text
    assert "10.66.0.30" not in text, "no shell account there — no entry"


def test_adopting_a_machine_lands_it_in_the_hubs_ssh_config(hub, monkeypatch):
    # The stubbed tunnel gives the row no mesh address; a machine without one
    # is unreachable and correctly earns no entry, so hand it one.
    from jstack_host import enrolment
    monkeypatch.setattr(enrolment, "mesh_address", lambda peer: "10.66.0.44")
    _redeem_host(hub, "work-key1", ssh_pubkey=LEAF_LINE, ssh_user="alex")
    cfg = Path(os.environ["HOME"]) / ".ssh" / "config"
    text = cfg.read_text()
    assert "Host work-mac" in text and "10.66.0.44" in text


# ── a poked machine pulls and applies without root ──────────────────────────

def test_apply_material_writes_both_files_and_needs_no_runner(tmp_path):
    home = tmp_path / "leaf-home"
    steps = shell_access.apply_material(
        {"authorized": [LINE_A],
         "peers": [{"name": "work-temp", "address": "10.66.0.21",
                    "user": "alex"}]}, home)

    assert shell_access.read_authorized_block(
        home / ".ssh" / "authorized_keys") == [LINE_A]
    assert "Host work-temp" in (home / ".ssh" / "config").read_text()
    assert [s["step"] for s in steps] == ["authorized-keys", "ssh-config",
                                          "remote-login"]
    assert all(s["ok"] for s in steps[:2])
    # No grant on this machine ever spent the root step, so it is owed, not
    # assumed done — the whole point of a presentation that costs no root.
    assert steps[2]["ok"] is False


def test_refresh_route_pulls_from_the_parent_and_applies(tmp_path, monkeypatch):
    from jstack_host import managed_access, router
    monkeypatch.setattr(managed_access, "is_leaf", lambda: True)
    monkeypatch.setattr(managed_access, "parent_shell",
                        lambda: {"authorized": [LINE_A], "peers": []})
    out = router.shell_refresh(device_id="ignored")

    assert shell_access.read_authorized_block(
        Path(os.environ["HOME"]) / ".ssh" / "authorized_keys") == [LINE_A]
    written = [s for s in out["steps"] if s["step"] != "remote-login"]
    assert written and all(s["ok"] for s in written)


def test_refresh_route_with_no_material_touches_nothing(monkeypatch):
    from jstack_host import managed_access, router
    monkeypatch.setattr(managed_access, "is_leaf", lambda: True)
    monkeypatch.setattr(managed_access, "parent_shell", lambda: {})
    assert router.shell_refresh(device_id="ignored") == {"steps": []}
    assert not (Path(os.environ["HOME"]) / ".ssh").exists()


def test_refresh_route_on_a_hub_refuses(monkeypatch):
    from fastapi import HTTPException
    from jstack_host import managed_access, router
    monkeypatch.setattr(managed_access, "is_leaf", lambda: False)
    with pytest.raises(HTTPException):
        router.shell_refresh(device_id="ignored")


# ── what the console sees, and what devices never do ────────────────────────

def test_devices_see_no_key_material_and_the_console_sees_the_grants(fleet):
    from jstack_host import router, shell_grants
    shell_grants.flip("src-key1", "dst-key1", True, poster=_wire([]))
    row = fleet.host_row("dst-key1")

    public = router._serve_host(row)
    assert "shell_pubkey" not in public and "shell_user" not in public
    console = router._serve_host(row, policy=True)
    assert console["shell_sources"] == ["src-key1"]
    assert console["shell_user"] == "alex"


def test_forgetting_a_machine_ends_its_reach_and_its_reachability(fleet):
    from jstack_host import shell_grants
    shell_grants.flip("src-key1", "dst-key1", True, poster=_wire([]))
    shell_grants.flip("dst-key1", "src-key1", True, poster=_wire([]))
    fleet.forget_host("src-key1")

    log = []
    steps = shell_grants.machine_forgotten("src-key1", poster=_wire(log))
    assert fleet.shell_sources_for("dst-key1") == []
    assert fleet.shell_targets_for("src-key1") == []
    # The one counterpart is poked; the forgotten machine is not — its own
    # cleanup is detach's job on the machine itself.
    refreshed = [s["step"] for s in steps if s["step"].startswith("refresh:")]
    assert refreshed == ["refresh:dst-key1"]
    cfg = (Path(os.environ["HOME"]) / ".ssh" / "config").read_text()
    assert "work-main" not in cfg


def test_the_console_route_flips_and_maps_unknown_to_404(fleet, monkeypatch):
    from fastapi import HTTPException
    from jstack_host import managed_access, router
    monkeypatch.setattr(managed_access, "require_console", lambda r: None)
    monkeypatch.setattr(
        "jstack_host.shell_grants.refresh_on",
        lambda key, *, poster=None: {"step": f"refresh:{key}", "ok": True,
                                     "note": ""})
    out = router.set_host_shell_grant(
        "dst-key1", router.LeafShellGrantRequest(src="src-key1", allowed=True),
        request=None)
    assert fleet.shell_sources_for("dst-key1") == ["src-key1"]
    assert all(s["ok"] for s in out["steps"])

    with pytest.raises(HTTPException) as caught:
        router.set_host_shell_grant(
            "nope-key", router.LeafShellGrantRequest(src="src-key1",
                                                     allowed=True),
            request=None)
    assert caught.value.status_code == 404


# ── attach applies what the parent answered ─────────────────────────────────

def _attach(tmp_path, response, runner=None, posts=None, home=None):
    import subprocess
    from jstack_host import attach_parent

    def poster(url, payload):
        if posts is not None:
            posts.append((url, payload))
        return 200, response

    def ok_runner(argv, cwd=None, env=None, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout="installed",
                                           stderr="")

    return attach_parent.attach(
        "ABCD-1234", "http://studio.local:9090", host_key="test-host-0001",
        dest_dir=tmp_path / "bundle", poster=poster,
        runner=runner or ok_runner, sudo=False,
        install_env={"LEAF_DEST": str(tmp_path / "root")},
        home=home or tmp_path / "home")


def _host_response(**extra):
    from jstack_host import tunnel
    bundle = {name: f"contents of {name}\n" for name in tunnel.LEAF_FILES}
    return {"device": {"id": "dev_new", "name": "studio", "revoked": False},
            "token": "TOKEN-FROM-PARENT", "kind": "host",
            "tunnel": {"device": "studio", "bundle": bundle, "created": True},
            "tunnel_note": "", "host": {"key": "test-host-0001"},
            "superseded": False, **extra}


def test_attach_mints_and_presents_this_machines_key(tmp_path):
    import getpass
    posts = []
    _attach(tmp_path, _host_response(), posts=posts)
    payload = posts[0][1]
    assert payload["ssh_pubkey"] == shell_access.public_key()
    assert payload["ssh_pubkey"].startswith("ssh-ed25519 ")
    assert payload["ssh_user"] == getpass.getuser()


def test_attach_applies_the_shell_the_parent_answered(tmp_path):
    shell = {"authorized": [LINE_A],
             "peers": [{"name": "work-temp", "address": "10.66.0.21",
                        "user": "alex"}]}
    runner = _Runner(answers={"-getremotelogin": "Remote Login: Off\n"})
    result = _attach(tmp_path, _host_response(shell=shell), runner=runner)

    home = tmp_path / "home"
    assert shell_access.read_authorized_block(
        home / ".ssh" / "authorized_keys") == [LINE_A]
    assert not (tmp_path / "root" / shell_access.SUDOERS_PATH).exists()
    assert any("-setremotelogin" in c for c in runner.calls)
    assert "Host work-temp" in (home / ".ssh" / "config").read_text()
    assert all(s["ok"] for s in result["shell_steps"])
    assert result["shell_steps"], "the steps are the report"


def test_a_parent_without_shell_answers_changes_nothing(tmp_path):
    result = _attach(tmp_path, _host_response())
    assert result["shell_steps"] == []
    assert not (tmp_path / "home" / ".ssh").exists()
    assert not (tmp_path / "root" / shell_access.SUDOERS_PATH).exists()


# ── detach takes it all back ────────────────────────────────────────────────

def test_detach_reverses_shell_access(tmp_path):
    from jstack_host import detach_parent
    state = hostenv.state_dir()
    home = tmp_path / "home"
    shell_access.identity()
    ak = home / ".ssh" / "authorized_keys"
    shell_access.write_authorized_block(ak, [LINE_A])
    runner = _Runner(answers={"-getremotelogin": "Remote Login: Off\n"})
    shell_access.enable(runner=runner, sudo=False)
    dropin = tmp_path / shell_access.SUDOERS_PATH
    dropin.parent.mkdir(parents=True, exist_ok=True)
    dropin.write_text("alex ALL=(ALL) NOPASSWD: ALL\n")

    result = detach_parent.detach(root=tmp_path, state=state, home=home,
                                  runner=_Runner(), poster=lambda u, t: (200, {}),
                                  sudo=False)

    assert not (tmp_path / shell_access.SUDOERS_PATH).exists()
    assert shell_access.read_authorized_block(ak) == []
    assert shell_access.public_key() == ""
    for name in ("remote-login", "sudoers", "authorized-keys", "identity"):
        assert _step(result["steps"], name)["ok"] is True, name


# ── the capability is probed, not assumed ───────────────────────────────────

def test_shell_access_is_a_probed_host_capability():
    from jstack_host import router
    assert router._probe("shell_access") is False
    shell_access.identity()
    assert router._probe("shell_access") is True


# ── a machine adopted before the feature presents its key late ──────────────

def _presenting(monkeypatch, mapping: dict):
    """Wire credential → machine the way the hub resolves it, so a presentation
    can only ever be aimed at the row the credential names."""
    from jstack_host import managed_access
    monkeypatch.setattr(managed_access, "is_leaf", lambda: False)
    monkeypatch.setattr(managed_access, "leaf_for_device",
                        lambda device_id: mapping.get(device_id))


def test_a_machine_adopted_before_the_feature_presents_its_key_on_refresh(fleet, monkeypatch):
    """The defect this closes: `set_host_shell` had one writer, the adoption
    redeem, so a Mac adopted by an earlier build held an empty row forever and
    `leaf_shell` answered {} for it until somebody re-adopted it."""
    from jstack_host import router
    fleet.upsert_host("old-key1", "Old Mac", "10.66.0.30", 9090)
    from jstack_host import shell_grants
    assert shell_grants.leaf_shell("old-key1") == {}, "the pre-shell state"
    _presenting(monkeypatch, {"dev-old": fleet.host_row("old-key1")})

    out = router.managed_shell(router.ManagedShellRequest(pubkey=LINE_B, user="alex"),
                              device_id="dev-old")

    row = fleet.host_row("old-key1")
    assert row["shell_pubkey"] == LINE_B and row["shell_user"] == "alex"
    # And the same request answers the set it just became eligible for.
    assert out["authorized"] == [shell_access.identity()]
    assert shell_grants.leaf_shell("old-key1")["authorized"]


def test_presenting_late_puts_the_machine_in_the_hubs_own_ssh_config(fleet, monkeypatch):
    from jstack_host import router
    fleet.upsert_host("old-key1", "Old Mac", "10.66.0.30", 9090)
    _presenting(monkeypatch, {"dev-old": fleet.host_row("old-key1")})
    cfg = Path(os.environ["HOME"]) / ".ssh" / "config"

    router.managed_shell(router.ManagedShellRequest(pubkey=LINE_B, user="alex"),
                        device_id="dev-old")

    assert "Host old-mac" in cfg.read_text(), "the hub cannot ssh what it cannot name"


def test_a_machine_cannot_present_an_identity_for_another_machines_row(fleet, monkeypatch):
    """There is no key parameter to aim: the credential names the machine."""
    from jstack_host import router
    _presenting(monkeypatch, {"dev-src": fleet.host_row("src-key1")})
    other_before = dict(fleet.host_row("dst-key1"))

    router.managed_shell(router.ManagedShellRequest(pubkey=LEAF_LINE, user="mallory"),
                        device_id="dev-src")

    assert fleet.host_row("src-key1")["shell_pubkey"] == LEAF_LINE
    after = fleet.host_row("dst-key1")
    assert after["shell_pubkey"] == other_before["shell_pubkey"]
    assert after["shell_user"] == other_before["shell_user"] == "alex"


def test_an_unchanged_key_is_not_rewritten(fleet, monkeypatch):
    from jstack_host import router
    _presenting(monkeypatch, {"dev-src": fleet.host_row("src-key1")})
    writes = []
    monkeypatch.setattr(type(fleet), "set_host_shell",
                        lambda self, *a: writes.append(a) or True)

    router.managed_shell(router.ManagedShellRequest(pubkey=LINE_A, user="alex"),
                        device_id="dev-src")
    assert writes == [], "a settled machine's refresh must touch no row"

    router.managed_shell(router.ManagedShellRequest(pubkey=LINE_B, user="alex"),
                        device_id="dev-src")
    assert writes == [("src-key1", LINE_B, "alex")], "a changed key is stored"


def test_a_presented_key_that_would_smuggle_options_is_refused(fleet, monkeypatch):
    from jstack_host import router
    fleet.upsert_host("old-key1", "Old Mac", "10.66.0.30", 9090)
    _presenting(monkeypatch, {"dev-old": fleet.host_row("old-key1")})

    out = router.managed_shell(
        router.ManagedShellRequest(pubkey='command="rm -rf /" ssh-ed25519 AAAA k',
                                   user="alex"),
        device_id="dev-old")

    assert fleet.host_row("old-key1")["shell_pubkey"] == ""
    assert out == {}, "an unstorable identity earns no set"


def test_a_presented_account_that_is_not_a_name_never_blanks_a_working_one(fleet, monkeypatch):
    """Half a presentation is not an upgrade, and must not be a downgrade.

    `hub_peers` skips a row with no `shell_user`, so storing a valid key beside
    an unusable account would take a machine the hub can currently reach and
    make it unreachable — while answering 200. Both halves or neither.
    """
    from jstack_host import router
    _presenting(monkeypatch, {"dev-src": fleet.host_row("src-key1")})
    before = dict(fleet.host_row("src-key1"))
    assert before["shell_user"] == "alex", "the fixture's working state"

    for account in ("", "root; rm -rf /", "a" * 300, "has space"):
        router.managed_shell(
            router.ManagedShellRequest(pubkey=LINE_B, user=account),
            device_id="dev-src")
        row = fleet.host_row("src-key1")
        assert row["shell_user"] == "alex", f"{account!r} blanked the account"
        assert row["shell_pubkey"] == before["shell_pubkey"], (
            f"{account!r} stored a key with no account to reach it under")


def test_a_pull_that_presents_nothing_still_reads(fleet, monkeypatch):
    """Every build before this one posts an empty body; it must be unaffected."""
    from jstack_host import router, shell_grants
    shell_grants.flip("src-key1", "dst-key1", True, poster=_wire([]))
    _presenting(monkeypatch, {"dev-dst": fleet.host_row("dst-key1")})

    assert router.managed_shell(None, device_id="dev-dst") == \
        shell_grants.leaf_shell("dst-key1")
    assert fleet.host_row("dst-key1")["shell_pubkey"] == LINE_B


def test_the_leaf_presents_its_identity_on_every_pull(monkeypatch):
    import getpass
    from jstack_host import managed_access
    sent = []
    monkeypatch.setattr(managed_access, "_post_parent",
                        lambda route, body: sent.append((route, body)) or {})
    managed_access.parent_shell()

    assert sent == [("shell", {"pubkey": shell_access.public_key(),
                               "user": getpass.getuser()})]
    assert sent[0][1]["pubkey"].startswith("ssh-ed25519 ")


def test_a_mint_that_fails_costs_shell_access_and_never_the_refresh(monkeypatch):
    from jstack_host import managed_access
    sent = []
    monkeypatch.setattr(managed_access, "_post_parent",
                        lambda route, body: sent.append((route, body)) or {})
    monkeypatch.setattr(shell_access, "identity", lambda *a, **kw: (_ for _ in ()).throw(
        shell_access.ShellAccessError("no ssh-keygen here")))

    assert managed_access.parent_shell() == {}
    assert sent == [("shell", {})], "the pull itself must still happen"


# ── the one root step stays honest ──────────────────────────────────────────

def test_a_refresh_on_a_machine_whose_remote_login_is_off_owes_the_root_step(monkeypatch):
    """The user-writable half lands with no root at all; the root step adoption
    spends is reported outstanding rather than acquired or assumed."""
    from jstack_host import managed_access, router
    monkeypatch.setattr(managed_access, "is_leaf", lambda: True)
    monkeypatch.setattr(managed_access, "parent_shell",
                        lambda: {"authorized": [LINE_A], "peers": []})
    assert shell_access.root_step_spent() is False

    steps = router.shell_refresh(device_id="ignored")["steps"]

    home = Path(os.environ["HOME"])
    assert shell_access.read_authorized_block(home / ".ssh" / "authorized_keys") == [LINE_A]
    assert _step(steps, "authorized-keys")["ok"] is True
    assert _step(steps, "ssh-config")["ok"] is True
    owed = _step(steps, "remote-login")
    assert owed and owed["ok"] is False
    assert "Remote Login" in owed["note"]


def test_the_outstanding_root_step_reaches_the_hub_as_a_failed_refresh(fleet):
    """`refresh_on` is the channel: no new one was invented for this."""
    from jstack_host import shell_grants

    def poster(url, payload, token):
        from jstack_host import grants
        if url.endswith(grants.MINT_PATH):
            return 200, {"device": {"name": "shell refresh"}, "token": "jr1.p.tok"}
        return 200, {"steps": [{"step": "authorized-keys", "ok": True, "note": ""},
                               {"step": "remote-login", "ok": False,
                                "note": "inbound ssh needs Remote Login on"}]}

    step = shell_grants.refresh_on("dst-key1", poster=poster)
    assert step["ok"] is False
    assert "Remote Login" in step["note"]


def test_a_machine_that_spent_the_root_step_owes_nothing(monkeypatch):
    from jstack_host import managed_access, router
    shell_access.enable(runner=_Runner(answers={"-getremotelogin": "Remote Login: Off\n"}),
                        sudo=False)
    assert shell_access.root_step_spent() is True
    monkeypatch.setattr(managed_access, "is_leaf", lambda: True)
    monkeypatch.setattr(managed_access, "parent_shell",
                        lambda: {"authorized": [LINE_A], "peers": []})

    steps = router.shell_refresh(device_id="ignored")["steps"]
    assert _step(steps, "remote-login") is None
    assert all(s["ok"] for s in steps)


def test_a_machine_whose_grant_found_remote_login_already_on_owes_nothing():
    """`enabled_remote_login` is False here — it answers whether the grant
    turned it on — so it cannot be the predicate on its own: the record's
    existence is what says the root step was spent."""
    shell_access.enable(runner=_Runner(answers={"-getremotelogin": "Remote Login: On\n"}),
                        sudo=False)
    assert shell_access.enabled_remote_login() is False
    assert shell_access.root_step_spent() is True
    steps = shell_access.apply_material({"authorized": [LINE_A], "peers": []},
                                        Path(os.environ["HOME"]))
    assert _step(steps, "remote-login") is None


def test_no_material_means_no_owed_root_step(tmp_path):
    """A machine with nothing granted owes nothing; the step follows the keys."""
    steps = shell_access.apply_material({"authorized": [], "peers": []},
                                        tmp_path / "empty-home")
    assert _step(steps, "remote-login") is None


# ── the adoption path is untouched ──────────────────────────────────────────

def test_the_adoption_path_still_stores_the_key_and_spends_the_root_step(tmp_path):
    """Adoption keeps its one root moment and reports every step ok — the late
    presentation added a second road in, it did not change this one."""
    shell = {"authorized": [LINE_A], "peers": []}
    runner = _Runner(answers={"-getremotelogin": "Remote Login: Off\n"})
    result = _attach(tmp_path, _host_response(shell=shell), runner=runner)

    assert _step(result["shell_steps"], "remote-login")["ok"] is True
    assert all(s["ok"] for s in result["shell_steps"])
    assert [s["step"] for s in result["shell_steps"]].count("remote-login") == 1
    assert shell_access.read_authorized_block(
        tmp_path / "home" / ".ssh" / "authorized_keys") == [LINE_A]
    assert not (tmp_path / "root" / shell_access.SUDOERS_PATH).exists()
