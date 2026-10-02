"""SOS inventory/ordering contracts; execute this suite in a disposable Mac VM."""
import io
import json
import os
import plistlib
from pathlib import Path
import subprocess
import tempfile

import pytest

from jstack_host import sos, sos_user_cleanup, engines


@pytest.fixture(autouse=True)
def guest_only():
    model = subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.model"], text=True).strip()
    if not model.startswith("VirtualMac"):
        pytest.fail("SOS tests run only in isolated macOS VMs")
    if not (Path.home() / ".sos-test-guest").is_file():
        pytest.fail("SOS guest was not explicitly provisioned")


def test_every_provider_has_removal_coverage():
    assert set(sos.PROVIDERS) == set(engines.engine_ids())


@pytest.mark.parametrize("path", ["/", "/Users", "/Applications", "/Library", "/opt/homebrew",
                                   "/Users/admin", "/Users/admin/Library", "/Users/other/.codex"])
def test_enclosing_and_other_account_paths_refused(path):
    with pytest.raises(ValueError):
        sos.safe_target(Path(path), Path("/Users/admin"))


def test_symlink_target_is_removed_without_following_but_ancestor_refused(tmp_path):
    tmp_path = tmp_path.resolve()
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
    assert str(Path.home() / "Agents") in plan["history"]
    assert str(Path.home() / "Projects") in plan["history"]


def test_inventory_retains_service_and_shell_history_locations(monkeypatch):
    base = Path.home() / "sos-inventory-fixture"
    monkeypatch.setattr(sos.service_settings, "read", lambda: {
        "environment": {"CODEX_HOME": str(base / "installed-codex"),
                        "JREMOTE_STATE_DIR": str(base / "installed-state"),
                        "JREMOTE_CREDENTIALS_DIR": str(base / "credentials")},
        "scheduler": {"environment": {"JSTACK_LOGS_DIR": str(base / "scheduler-logs"),
                                       "SCHEDULER_STATE_DIR": str(base / "scheduler-state")}},
        "migration_dir": str(base / "migration"),
    })
    monkeypatch.setenv("CODEX_HOME", str(base / "shell-codex"))
    monkeypatch.setenv("JREMOTE_STATE_DIR", str(base / "shell-state"))
    monkeypatch.setenv("JSTACK_TIMELINE_DIR", str(base / "timeline"))
    plan = sos.inventory()
    for name in ("installed-codex", "shell-codex", "installed-state", "shell-state",
                 "scheduler-logs", "scheduler-state", "timeline", "migration"):
        assert str(base / name) in plan["history"]
    assert str(base / "credentials") in plan["data"]
    assert str(Path.home() / "jRemote-Code") in plan["data"]
    assert str(Path.home() / ".scheduler") in plan["history"]


def test_agent_and_project_copies_precede_configuration_removal(monkeypatch):
    root = Path.home() / "sos-inventory-fixture"
    custom = Path.home() / "sos-custom-agents"
    monkeypatch.setattr(sos.service_settings, "read", lambda: {
        "environment": {"JSTACK_AGENTS_DIR": str(custom)}})
    monkeypatch.setattr(sos.hostenv, "stack_root", lambda: root)
    monkeypatch.setattr(sos.hostenv, "instance_root", lambda: custom)
    monkeypatch.setenv("JREMOTE_INSTANCE_ROOT", str(root / "other-instances"))
    plan = sos.inventory()
    for path in (root / "Agents", root / "Projects", custom, root / "other-instances"):
        assert str(path) in plan["history"]
        assert str(path) not in plan["data"]
    script = sos.worker(plan)
    assert script.index("phase history") < script.index("phase data")
    assert str(root / "Config") in plan["data"]
    assert str(root / "Credentials") in plan["data"]


def test_inventory_refuses_to_delete_execution_dependency_early(monkeypatch):
    root = Path.home() / "sos-inventory-fixture"
    monkeypatch.setattr(sos.hostenv, "stack_root", lambda: root)
    monkeypatch.setattr(sos.hostenv, "instance_root", lambda: root / "Agents")
    monkeypatch.setattr(sos.service_settings, "read", lambda: {
        "app": str(root / "Projects/jStack Hub.app")})
    with pytest.raises(ValueError, match="contains the required Hub executor"):
        sos.inventory()


def test_worker_prioritizes_history_and_removes_itself_last():
    plan = {"home": "/Users/admin", "uid": 501, "root": "/Users/admin/Stack",
            "services": [], "history": ["/Users/admin/.codex"],
            "data": ["/Users/admin/Stack/Agents"], "apps": ["/Applications/Codex.app"]}
    script = sos.worker(plan)
    assert script.index("phase history") < script.index("phase data") < script.index("phase apps")
    assert script.index("phase verify") < script.index("phase complete") < script.index("-replace SOSComplete")
    assert "rm -rf /private/var/db/live.jstack.sos" not in script
    supervisor = sos.supervisor()
    assert supervisor.index('test "$complete" = true') < supervisor.index("/bin/rm -rf")
    assert supervisor.index("/bin/rm -rf") < supervisor.index("/bin/rm -f")
    assert supervisor.rstrip().endswith("/bin/launchctl bootout system/live.jstack.sos")
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


@pytest.fixture(scope="module")
def eraser(tmp_path_factory):
    destination = tmp_path_factory.mktemp("native").resolve() / "Erase"
    source = Path(__file__).resolve().parents[1] / "macos/Erase.swift"
    subprocess.run(["/usr/bin/xcrun", "swiftc", str(source), "-o", str(destination)], check=True)
    return destination


@pytest.fixture
def authorization(guest_only, tmp_path):
    work = Path("/private/var/db/live.jstack.sos")
    assert not work.exists(), "never replace an existing wipe's state"
    subprocess.run(["sudo", "-n", "mkdir", "-m", "700", str(work)], check=True)

    def approve(*paths):
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps({"schema": 1, "home": str(Path.home()),
                                       "history": [str(p) for p in paths], "data": [], "apps": []}))
        subprocess.run(["sudo", "-n", "install", "-o", "root", "-g", "wheel", "-m", "600",
                        str(manifest), str(work / "manifest.json")], check=True)
        return work / "manifest.json"

    try:
        yield approve
    finally:
        subprocess.run(["sudo", "-n", "rm", "-rf", str(work)], check=True)


def erase(eraser, target):
    return subprocess.run(["sudo", "-n", str(eraser), str(target)], capture_output=True, text=True)


def test_native_removal_preserves_symlink_destination(eraser, authorization, tmp_path):
    tmp_path = tmp_path.resolve()
    target = tmp_path / "selected"
    target.mkdir()
    outside = tmp_path / "preserved"
    outside.mkdir()
    sentinel = outside / "data"
    sentinel.write_bytes(b"must survive")
    (target / "link").symlink_to(outside)
    (target / "nested").mkdir()
    (target / "nested/file").write_text("history")
    authorization(target)
    result = erase(eraser, target)
    assert result.returncode == 0, result.stderr
    assert not target.exists()
    assert sentinel.read_bytes() == b"must survive"
    assert erase(eraser, target).returncode == 0


def test_native_refuses_symlink_ancestor(eraser, authorization, tmp_path):
    tmp_path = tmp_path.resolve()
    outside = tmp_path / "preserved"
    outside.mkdir()
    sentinel = outside / "data"
    sentinel.write_text("must survive")
    link = tmp_path / "link"
    link.symlink_to(outside)
    authorization(link / "data")
    result = erase(eraser, link / "data")
    assert result.returncode != 0
    assert sentinel.read_text() == "must survive"


@pytest.mark.parametrize("path", ["/", "/Users/admin", "/Library/LaunchDaemons", "/opt/homebrew",
                                   "/Library/Keychains", "/Library/Preferences", "/private/tmp",
                                   "/Volumes/Backup", "/opt/unlisted"])
def test_native_refuses_unlisted_paths(eraser, authorization, tmp_path, path):
    target = tmp_path.resolve() / "selected"
    target.write_text("history")
    authorization(target)
    assert erase(eraser, path).returncode != 0
    assert target.read_text() == "history"


def test_native_approval_does_not_expand_to_parent_sibling_or_child(eraser, authorization, tmp_path):
    tmp_path = tmp_path.resolve()
    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / "child").write_text("history")
    other = tmp_path / "other"
    other.write_text("preserve")
    authorization(selected)
    for target in (tmp_path, other, selected / "child"):
        assert erase(eraser, target).returncode != 0
    assert other.read_text() == "preserve"
    assert (selected / "child").read_text() == "history"


@pytest.mark.parametrize("change", ["owner", "mode", "acl", "symlink", "missing", "malformed"])
def test_native_refuses_untrusted_authorization(eraser, authorization, tmp_path, change):
    target = tmp_path.resolve() / "selected"
    target.write_text("history")
    manifest = authorization(target)
    if change == "owner":
        command = ["chown", str(os.getuid()), str(manifest)]
    elif change == "mode":
        command = ["chmod", "666", str(manifest)]
    elif change == "acl":
        command = ["chmod", "+a", "everyone allow write", str(manifest)]
    elif change in {"symlink", "missing"}:
        command = ["rm", str(manifest)]
    else:
        malformed = tmp_path / "bad.json"
        malformed.write_text("{broken")
        command = ["install", "-m", "600", str(malformed), str(manifest)]
    subprocess.run(["sudo", "-n", *command], check=True)
    if change == "symlink":
        subprocess.run(["sudo", "-n", "ln", "-s", str(tmp_path / "manifest.json"), str(manifest)], check=True)
    assert erase(eraser, target).returncode != 0
    assert target.read_text() == "history"


def test_native_refuses_self_removal_before_completion(eraser, authorization, tmp_path):
    authorization(tmp_path.resolve() / "selected")
    assert erase(eraser, sos.WORK).returncode != 0
    assert sos.WORK.exists()


def test_native_requires_root_even_with_approved_manifest(eraser, authorization, tmp_path):
    target = tmp_path.resolve() / "selected"
    target.write_text("history")
    authorization(target)
    assert subprocess.run([str(eraser), str(target)]).returncode != 0
    assert target.read_text() == "history"


def test_failed_staging_rolls_back_without_disabling_runtime(guest_only, tmp_path):
    assert not sos.WORK.exists()
    assert not sos.PLIST.exists()
    app = tmp_path / "Hub.app"
    helper = app / "Contents/MacOS/JStackErase"
    helper.parent.mkdir(parents=True)
    helper.write_bytes(b"disappearing build input")
    plan = {"app": str(app), "home": str(Path.home()), "uid": os.getuid(),
            "root": str(tmp_path), "history": [], "data": [], "apps": [], "services": []}
    script = sos.bootstrap(plan)
    helper.unlink()
    result = subprocess.run(["sudo", "-n", "/bin/sh", "-c", script], capture_output=True, text=True)
    assert result.returncode != 0
    assert not sos.WORK.exists(), result.stderr
    assert not sos.PLIST.exists()


def test_existing_operation_is_never_replaced(eraser, authorization, tmp_path):
    manifest = authorization(tmp_path.resolve() / "selected")
    before = subprocess.check_output(["sudo", "-n", "cat", str(manifest)])
    app = tmp_path / "Hub.app"
    helper = app / "Contents/MacOS/JStackErase"
    helper.parent.mkdir(parents=True)
    helper.write_bytes(eraser.read_bytes())
    plan = {"app": str(app), "home": str(Path.home()), "uid": os.getuid(),
            "root": str(tmp_path), "history": [], "data": [], "apps": [], "services": []}
    result = subprocess.run(["sudo", "-n", "/bin/sh", "-c", sos.bootstrap(plan)], capture_output=True, text=True)
    assert result.returncode != 0
    assert subprocess.check_output(["sudo", "-n", "cat", str(manifest)]) == before


@pytest.mark.parametrize("complete", [False, True])
def test_supervisor_recovers_missing_worker_only_after_verified_completion(guest_only, tmp_path, monkeypatch, complete):
    # Use distinct root-owned fixture paths and an unregistered label so the
    # final bootout cannot terminate pytest or an actual SOS operation.
    fixture = Path("/private/var/db") / ("sos-supervisor-test-" + tmp_path.name)
    assert not fixture.exists()
    subprocess.run(["sudo", "-n", "mkdir", "-m", "700", str(fixture)], check=True)
    work = fixture / "work"
    plist = fixture / "definition.plist"
    monkeypatch.setattr(sos, "WORK", work)
    monkeypatch.setattr(sos, "PLIST", plist)
    monkeypatch.setattr(sos, "LABEL", "test.sos.never-registered")
    definition = tmp_path / "definition.plist"
    definition.write_bytes(plistlib.dumps({"SOSComplete": complete}))
    subprocess.run(["sudo", "-n", "install", "-m", "600", str(definition), str(plist)], check=True)
    subprocess.run(["sudo", "-n", "mkdir", "-m", "700", str(work)], check=True)
    try:
        subprocess.run(["sudo", "-n", "/bin/sh", "-c", sos.supervisor()], capture_output=True)
        remaining = subprocess.run(["sudo", "-n", "test", "-e", str(plist)]).returncode == 0
        assert remaining is not complete
        remaining_work = subprocess.run(["sudo", "-n", "test", "-e", str(work)]).returncode == 0
        assert remaining_work is not complete
    finally:
        subprocess.run(["sudo", "-n", "rm", "-rf", str(fixture)], check=True)


def test_history_symlink_is_not_reported_as_complete(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="sos-provider-", dir=Path.home()) as folder:
        root = Path(folder)
        outside = root / "outside"
        outside.mkdir()
        link = root / "provider"
        link.symlink_to(outside)
        monkeypatch.setenv("CODEX_HOME", str(link))
        monkeypatch.setattr(sos.service_settings, "read", lambda: {})
        with pytest.raises(ValueError, match="history root is a symlink"):
            sos.inventory()


def test_settings_cleanup_preserves_unrelated_bytes_and_permissions(tmp_path):
    path = tmp_path.resolve() / "authorized_keys"
    original = ("ssh-ed25519 unrelated-before\n# >>> jremote managed keys >>>\n"
                "ssh-ed25519 owned\n# <<< jremote managed keys <<<\n"
                "ssh-ed25519 unrelated-after")
    path.write_text(original)
    path.chmod(0o640)
    transform = lambda text: sos_user_cleanup.strip_blocks(
        text, "# >>> jremote managed keys >>>", "# <<< jremote managed keys <<<")
    sos_user_cleanup.rewrite(path, transform)
    assert path.read_bytes() == b"ssh-ed25519 unrelated-before\nssh-ed25519 unrelated-after"
    assert path.stat().st_mode & 0o777 == 0o640
    inode = path.stat().st_ino
    sos_user_cleanup.rewrite(path, transform)
    assert path.stat().st_ino == inode


@pytest.mark.parametrize("contents", ["BEGIN\nowned\n", "END\n", "BEGIN\nBEGIN\nEND\nEND\n"])
def test_malformed_settings_fail_without_replacing_original(tmp_path, contents):
    path = tmp_path.resolve() / "settings"
    path.write_text(contents)
    with pytest.raises(ValueError):
        sos_user_cleanup.rewrite(path, lambda text: sos_user_cleanup.strip_blocks(text, "BEGIN", "END"))
    assert path.read_text() == contents


@pytest.mark.parametrize("ancestor", [False, True])
def test_settings_symlinks_never_rewrite_their_destination(tmp_path, ancestor):
    tmp_path = tmp_path.resolve()
    destination = tmp_path / "outside"
    destination.mkdir()
    original = destination / "settings"
    original.write_text("preserve")
    link = tmp_path / "link"
    link.symlink_to(destination if ancestor else original)
    with pytest.raises((ValueError, OSError)):
        sos_user_cleanup.rewrite(link / "settings" if ancestor else link, lambda text: "erased")
    assert original.read_text() == "preserve"


def test_concurrent_settings_edit_is_not_overwritten(tmp_path):
    path = tmp_path.resolve() / "settings"
    path.write_text("original")
    def race(text):
        path.write_text("concurrent user edit")
        return "cleanup"
    with pytest.raises(ValueError, match="settings changed"):
        sos_user_cleanup.rewrite(path, race)
    assert path.read_text() == "concurrent user edit"
    assert not list(path.parent.glob(".sos-*"))


@pytest.mark.parametrize("action", ["reboot", "shutdown", "lock", "wipe"])
@pytest.mark.parametrize("interrupt", [EOFError, KeyboardInterrupt])
def test_confirmation_interrupted(monkeypatch, action, interrupt):
    monkeypatch.setattr(sos.sys, "stdin", type("TTY", (), {"isatty": lambda self: True})())
    def stop(prompt):
        raise interrupt
    monkeypatch.setattr("builtins.input", stop)
    assert not sos.confirm(action, out=io.StringIO())
