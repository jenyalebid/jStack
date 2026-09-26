"""The suite never writes the operator's ssh files (#186).

Every shell-access writer answers `Path.home()` when it is handed no home,
because that is how it runs for real. These are the entry points the suite
reached that way — detach's authorized_keys cleanup, the `/shell/refresh`
material write, the hub's peer config behind `/forget` and enrolment — each
called here with no home, exactly as production calls it. Each must land in
the test's HOME, and the real files must read byte-for-byte as they did before.
"""

import os
import sys
from pathlib import Path

import pytest

import conftest
from conftest import REAL_HOME, real_ssh_fingerprint
from jstack_host import detach_parent, shell_access, shell_grants

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJ5x7N0bJ1m3vHn0kq6bQy2Wc1dXo2mGmP3fHq9sR2Tw hub-main"


class _Runner:
    def __call__(self, argv, **kw):
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()


def test_ssh_writers_never_reach_the_real_home(tmp_path):
    before = real_ssh_fingerprint()
    home = Path(os.environ["HOME"])
    assert home != REAL_HOME
    assert Path.home() == home
    assert Path(os.path.expanduser("~")) == home

    # `/shell/refresh` on a leaf: the parent's grant written into Path.home().
    shell_access.apply_material({"authorized": [KEY], "peers": []}, Path.home())
    assert KEY in (home / ".ssh" / "authorized_keys").read_text()

    # The hub's own peer config, rewritten on /forget and on enrolment.
    shell_grants.refresh_hub_config()
    assert (home / ".ssh" / "config").exists()

    # Detach with no home: disable() empties the managed block it finds.
    detach_parent.detach(root=tmp_path / "root", state=tmp_path / "state",
                         runner=_Runner(), poster=lambda url, token: (200, {}),
                         sudo=False)
    assert KEY not in (home / ".ssh" / "authorized_keys").read_text()

    assert real_ssh_fingerprint() == before


def test_a_write_to_the_real_ssh_dir_is_refused_and_named():
    """The tripwire itself: raised as the audit event the write would raise,
    so proving it never touches the real directory even if it were broken."""
    target = str(REAL_HOME / ".ssh" / "authorized_keys")
    with pytest.raises(PermissionError):
        sys.audit("open", target, "w", os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    with pytest.raises(PermissionError):
        sys.audit("os.rename", str(REAL_HOME / ".ssh" / "x"), target, -1, -1)
    sys.audit("open", target, "r", os.O_RDONLY)  # reading stays allowed
    assert [e for e, _ in conftest.REAL_SSH_WRITES] == ["open", "os.rename"]
    del conftest.REAL_SSH_WRITES[:]
