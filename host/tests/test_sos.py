"""SOS inventory/ordering contracts; execute this suite in a disposable Mac VM."""
import io
from pathlib import Path
import subprocess

import pytest

from jstack_host import sos, engines


@pytest.fixture(autouse=True)
def guest_only():
    model = subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.model"], text=True).strip()
    if not model.startswith("VirtualMac"):
        pytest.fail("SOS tests run only in isolated macOS VMs")


def test_every_provider_has_removal_coverage():
    assert set(sos.PROVIDERS) == set(engines.engine_ids())


@pytest.mark.parametrize("path", ["/", "/Users", "/Applications", "/Library", "/opt/homebrew",
                                   "/Users/admin", "/Users/admin/Library", "/Users/other/.codex"])
def test_enclosing_and_other_account_paths_refused(path):
    with pytest.raises(ValueError):
        sos.safe_target(Path(path), Path("/Users/admin"))


def test_symlink_target_is_removed_without_following_but_ancestor_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(outside)
    assert sos.safe_target(link, Path.home()) == link
    with pytest.raises(ValueError, match="symlink"):
        sos.safe_target(link / "child", Path.home())


def test_root_is_never_a_recursive_target(monkeypatch, tmp_path):
    monkeypatch.setattr(sos.service_settings, "read", lambda: {})
    monkeypatch.setattr(sos.hostenv, "stack_root", lambda: Path.home())
    monkeypatch.setattr(sos.hostenv, "instance_root", lambda: Path.home() / "Agents")
    plan = sos.inventory()
    assert str(Path.home()) not in sum((plan[p] for p in ("history", "data", "apps")), [])
    assert str(Path.home() / "Agents") in plan["data"]


def test_worker_prioritizes_history_and_removes_itself_last():
    plan = {"home": "/Users/admin", "uid": 501, "root": "/Users/admin/Stack",
            "services": [], "history": ["/Users/admin/.codex"],
            "data": ["/Users/admin/Stack/Agents"], "apps": ["/Applications/Codex.app"]}
    script = sos.worker(plan)
    assert script.index("phase history") < script.index("phase data") < script.index("phase apps")
    assert script.index("phase verify") < script.index("phase complete") < script.index("Erase /private/var/db/live.jstack.sos")
    assert script.rstrip().endswith("/bin/launchctl bootout system/live.jstack.sos")
    assert "rm -rf /Users/admin/Stack\n" not in script
    assert "[ \"$failed\" = 0 ] || exit 1" in script


@pytest.mark.parametrize("action", ["reboot", "shutdown", "lock", "wipe"])
def test_confirmation_names_target_and_rejects_wrong_phrase(monkeypatch, action):
    monkeypatch.setattr(sos.sys, "stdin", type("TTY", (), {"isatty": lambda self: True})())
    monkeypatch.setattr(sos.socket, "gethostname", lambda: "sos-guest")
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    assert not sos.confirm(action, out=io.StringIO())
    monkeypatch.setattr("builtins.input", lambda prompt: action.upper() + " sos-guest")
    assert sos.confirm(action, out=io.StringIO())


def test_noninteractive_confirmation_refused(monkeypatch):
    monkeypatch.setattr(sos.sys, "stdin", io.StringIO("WIPE sos-guest\n"))
    with pytest.raises(ValueError, match="interactive"):
        sos.confirm("wipe")
