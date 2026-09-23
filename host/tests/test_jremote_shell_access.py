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


# ── the sudoers drop-in ─────────────────────────────────────────────────────

def test_sudoers_grants_the_enrolled_account_passwordless_sudo():
    content = shell_access.sudoers_content("jarvis")
    assert "jarvis ALL=(ALL) NOPASSWD: ALL" in content
    assert content.endswith("\n"), "sudoers refuses a file with no final newline"


@pytest.mark.skipif(not os.path.exists("/usr/sbin/visudo"),
                    reason="no visudo on this machine")
def test_sudoers_content_passes_visudo(tmp_path):
    import subprocess
    f = tmp_path / "jremote-managed"
    f.write_text(shell_access.sudoers_content("jarvis"))
    f.chmod(0o440)
    proc = subprocess.run(["/usr/sbin/visudo", "-c", "-f", str(f)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# ── enable: the joiner's root steps ─────────────────────────────────────────

def _step(steps, name):
    return next((s for s in steps if s["step"] == name), None)


def test_enable_turns_remote_login_on_and_records_that_it_did(tmp_path):
    runner = _Runner(answers={"-getremotelogin": "Remote Login: Off\n"})
    steps = shell_access.enable("jarvis", runner=runner, sudo=False,
                                root=tmp_path)

    assert any("-setremotelogin" in c and "on" in c for c in runner.calls)
    assert _step(steps, "remote-login")["ok"] is True

    sudoers = tmp_path / shell_access.SUDOERS_PATH
    assert sudoers.read_text() == shell_access.sudoers_content("jarvis")
    assert _mode(sudoers) == 0o440
    assert _step(steps, "sudoers")["ok"] is True

    # Recorded, so disable knows whether Off is the state to restore.
    assert shell_access.enabled_remote_login() is True


def test_enable_leaves_remote_login_alone_when_it_was_already_on(tmp_path):
    runner = _Runner(answers={"-getremotelogin": "Remote Login: On\n"})
    shell_access.enable("jarvis", runner=runner, sudo=False, root=tmp_path)

    assert not any("-setremotelogin" in c for c in runner.calls)
    assert shell_access.enabled_remote_login() is False


def test_enable_asks_for_sudo_on_a_real_root(tmp_path):
    """On a real machine both root steps need root; under the test root the
    sudoers write is direct, but systemsetup always goes through the prefix."""
    runner = _Runner(answers={"-getremotelogin": "Remote Login: Off\n"})
    shell_access.enable("jarvis", runner=runner, sudo=True, root=tmp_path)
    settings = [c for c in runner.calls if "-setremotelogin" in c]
    assert settings and settings[0][0] == "sudo"


# ── disable: detach's reverse ───────────────────────────────────────────────

def test_disable_restores_remote_login_only_if_enable_turned_it_on(tmp_path):
    runner = _Runner(answers={"-getremotelogin": "Remote Login: Off\n"})
    shell_access.enable("jarvis", runner=runner, sudo=False, root=tmp_path)

    off = _Runner()
    steps = shell_access.disable("jarvis", runner=off, sudo=False,
                                 root=tmp_path,
                                 authorized_keys=tmp_path / "authorized_keys")
    assert any("-setremotelogin" in c and "off" in c for c in off.calls)
    assert _step(steps, "remote-login")["ok"] is True


def test_disable_leaves_remote_login_up_when_it_predates_the_grant(tmp_path):
    runner = _Runner(answers={"-getremotelogin": "Remote Login: On\n"})
    shell_access.enable("jarvis", runner=runner, sudo=False, root=tmp_path)

    off = _Runner()
    steps = shell_access.disable("jarvis", runner=off, sudo=False,
                                 root=tmp_path,
                                 authorized_keys=tmp_path / "authorized_keys")
    assert not any("-setremotelogin" in c for c in off.calls)
    assert "was on before" in _step(steps, "remote-login")["note"]


def test_disable_removes_the_sudoers_drop_in_the_block_and_the_identity(tmp_path):
    shell_access.identity()
    ak = tmp_path / "authorized_keys"
    ak.write_text("ssh-rsa USEROWNKEY someone@laptop\n")
    shell_access.write_authorized_block(ak, [LINE_A])
    runner = _Runner(answers={"-getremotelogin": "Remote Login: Off\n"})
    shell_access.enable("jarvis", runner=runner, sudo=False, root=tmp_path)

    steps = shell_access.disable("jarvis", runner=_Runner(), sudo=False,
                                 root=tmp_path, authorized_keys=ak)

    assert not (tmp_path / shell_access.SUDOERS_PATH).exists()
    assert _step(steps, "sudoers")["ok"] is True
    assert shell_access.MARK_BEGIN not in ak.read_text()
    assert "USEROWNKEY" in ak.read_text()
    assert _step(steps, "authorized-keys")["ok"] is True
    assert shell_access.public_key() == ""
    assert _step(steps, "identity")["ok"] is True


def test_disable_on_a_machine_that_never_had_a_grant_is_clean(tmp_path):
    steps = shell_access.disable("jarvis", runner=_Runner(), sudo=False,
                                 root=tmp_path,
                                 authorized_keys=tmp_path / "authorized_keys")
    assert all(s["ok"] for s in steps)


# ── the convenience ssh config ──────────────────────────────────────────────

def test_ssh_config_lists_each_peer_and_survives_a_rewrite(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("Host mine\n  HostName example.com\n")
    peers = [{"name": "work-main", "address": "10.66.0.16", "user": "jenya"},
             {"name": "work-temp", "address": "10.66.0.21", "user": "jenya"}]

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
              "user": "jenya"}])
