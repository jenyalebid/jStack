"""SOS inventory/ordering contracts; execute this suite in a disposable Mac VM."""
import io
import json
import os
import plistlib
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import uuid

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


@pytest.mark.parametrize("kind", ["outside", "symlink", "directory", "empty", "malformed"])
def test_keychain_inventory_refuses_unaccounted_locations(monkeypatch, tmp_path, kind):
    home = tmp_path.resolve() / "home"
    home.mkdir()
    target = home / "test.keychain-db"
    target.write_text("fixture")
    if kind == "outside":
        target = tmp_path / "outside"
        target.write_text("fixture")
    elif kind == "symlink":
        alias = home / "alias"
        alias.symlink_to(target)
        target = alias
    elif kind == "directory":
        target = home
    output = "" if kind == "empty" else '"unterminated' if kind == "malformed" else shlex.quote(str(target))
    monkeypatch.setattr(sos.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, output, ""))
    with pytest.raises(ValueError):
        sos.keychain_inventory(home, os.getuid())


def test_keychain_cleanup_requires_inventory():
    with pytest.raises(ValueError, match="explicit keychain"):
        sos.keychain_commands({}, [], delete=True)


def launch_keychain_script(script, directory):
    identifier = "com.example.sos-keychain-test-" + uuid.uuid4().hex
    source = directory / "job.plist"
    destination = Path("/Library/LaunchDaemons") / (identifier + ".plist")
    output, error = directory / "output", directory / "error"
    source.write_bytes(plistlib.dumps({
        "Label": identifier, "RunAtLoad": True,
        "ProgramArguments": ["/bin/bash", "-c", script],
        "StandardOutPath": str(output), "StandardErrorPath": str(error)}))
    loaded = False
    try:
        subprocess.run(["sudo", "-n", "install", "-o", "root", "-g", "wheel", "-m", "644",
                        str(source), str(destination)], check=True)
        subprocess.run(["sudo", "-n", "launchctl", "bootstrap", "system", str(destination)], check=True)
        loaded = True
        deadline = time.monotonic() + 45
        while not output.exists() or "SOS-KEYCHAIN-EXIT=" not in output.read_text():
            if time.monotonic() > deadline:
                pytest.fail("launchd keychain cleanup did not finish")
            time.sleep(.2)
        return output.read_text(), error.read_text()
    finally:
        if loaded:
            subprocess.run(["sudo", "-n", "launchctl", "bootout", "system/" + identifier], check=True)
        subprocess.run(["sudo", "-n", "unlink", str(destination)], check=True)


@pytest.mark.parametrize("condition", ["normal", "changed-directory", "changed-volume", "locked"])
def test_launchd_deletes_explicit_keychains_and_preserves_unrelated(condition):
    security = "/usr/bin/security"
    original = shlex.split(subprocess.check_output([security, "list-keychains", "-d", "user"], text=True))
    with tempfile.TemporaryDirectory(prefix="sos-keychain-test-", dir=Path.home()) as name:
        directory = Path(name)
        paths = [directory / "first.keychain-db", directory / "second space.keychain-db"]
        created = []
        try:
            for path in paths:
                subprocess.run([security, "create-keychain", "-p", "sos-fixture", str(path)], check=True)
                created.append(path)
                subprocess.run([security, "unlock-keychain", "-p", "sos-fixture", str(path)], check=True)
                for service in (*sos.KEYCHAIN_SERVICES, "unrelated-sos-sentinel"):
                    for account in ("first", "second"):
                        subprocess.run([security, "add-generic-password", "-s", service, "-a", account,
                                        "-w", "synthetic-sos-value", "-T", security, str(path)], check=True)
            records = [{"path": str(p), "volume_uuid": sos.volume_uuid(p),
                        "parent_inode": p.parent.stat().st_ino, "uid": os.getuid()} for p in paths]
            refused = condition in ("changed-directory", "changed-volume")
            if condition == "changed-directory":
                records[0]["parent_inode"] += 1
            if condition == "changed-volume":
                records[0]["volume_uuid"] = str(uuid.uuid4()).upper()
            if condition == "locked":
                for path in paths:
                    subprocess.run([security, "lock-keychain", str(path)], check=True)
            user = ["/usr/bin/sudo", "-n", "-H", "-u", "#" + str(os.getuid()),
                    "/bin/sh", "-c", 'cd / && exec "$@"', "sos-user"]
            plan = {"keychains": records}
            script = "\n".join([
                "set -u", "umask 077", "cd " + shlex.quote(name),
                'trap \'printf "SOS-KEYCHAIN-EXIT=%s\\n" "$?"\' EXIT',
                'fail() { echo "INCOMPLETE: $*"; }',
                *sos.keychain_commands(plan, user, delete=True),
                *sos.keychain_commands(plan, user, delete=False),
                *sos.keychain_commands(plan, user, delete=True),
                *sos.keychain_commands(plan, user, delete=False)])
            output, error = launch_keychain_script(script, directory)
            assert f'SOS-KEYCHAIN-EXIT={1 if refused else 0}' in output, (output, error)
            for path in paths:
                if condition == "locked":
                    subprocess.run([security, "unlock-keychain", "-p", "sos-fixture", str(path)], check=True)
                for service in (*sos.KEYCHAIN_SERVICES, "unrelated-sos-sentinel"):
                    for account in ("first", "second"):
                        result = subprocess.run([security, "find-generic-password", "-s", service,
                                                 "-a", account, str(path)], capture_output=True)
                        expected = 0 if refused or service == "unrelated-sos-sentinel" else 44
                        assert result.returncode == expected, (service, account, result.returncode)
        finally:
            for path in created:
                subprocess.run([security, "delete-keychain", str(path)], check=True)
            subprocess.run([security, "list-keychains", "-d", "user", "-s", *original], check=True)


@pytest.mark.parametrize("path", ["/", "/Users", "/Applications", "/Library", "/opt/homebrew",
                                   Path.home(), Path.home() / "Library",
                                   Path.home().with_name("sos-other-account") / ".codex"])
def test_enclosing_and_other_account_paths_refused(path):
    with pytest.raises(ValueError):
        sos.safe_target(Path(path), Path.home())


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
    assert str(Path.home() / "Agents") in plan["history_sources"]
    assert str(Path.home() / "Projects") in plan["history_sources"]


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
        assert str(path) in plan["history_sources"]
        assert str(path) in plan["data"]
        assert str(path) not in plan["history"]
    script = sos.worker(plan)
    assert script.index("phase history") < script.index("--history-copies") < script.index("phase data")
    assert script.index("phase data") < script.index("remove " + shlex.quote(str(custom)))
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
    home = Path.home()
    plan = {"home": str(home), "uid": os.getuid(), "root": str(home / "Stack"),
            "keychains": sos.keychain_inventory(Path.home(), os.getuid()),
            "services": [], "history": [str(home / ".codex")],
            "data": [str(home / "Stack/Agents")], "apps": ["/Applications/Codex.app"]}
    script = sos.worker(plan)
    assert script.index("phase history") < script.index("phase data") < script.index("phase apps")
    assert script.index("phase verify") < script.index("phase complete") < script.index("-replace SOSComplete")
    assert "rm -rf /private/var/db/live.jstack.sos" not in script
    supervisor = sos.supervisor()
    assert supervisor.index('test "$complete" = true') < supervisor.index("/bin/rm -rf")
    assert supervisor.index("/bin/rm -rf") < supervisor.index("/bin/rm -f")
    assert supervisor.rstrip().endswith("/bin/launchctl bootout system/live.jstack.sos")
    assert "rm -rf " + shlex.quote(str(home / "Stack")) + "\n" not in script
    assert "[ \"$failed\" = 0 ] || exit 1" in script


def test_user_cleanup_can_read_cwd_after_leaving_root_only_worker(tmp_path):
    protected = tmp_path / "root-only"
    protected.mkdir(mode=0o700)
    subprocess.run(["sudo", "-n", "chown", "root:wheel", str(protected)], check=True)
    try:
        plan = {"home": str(Path.home()), "uid": os.getuid(), "root": str(tmp_path),
                "keychains": sos.keychain_inventory(Path.home(), os.getuid()),
                "services": [], "history": [], "data": [], "apps": []}
        command = next(line for line in sos.worker(plan).splitlines() if "_wipe-user-cleanup" in line)
        argv = shlex.split(command.split(" || ", 1)[0])
        probe = [sys.executable, "-c", "import os; print(os.getcwd())"]
        root_launcher = ["sudo", "-n", "/bin/sh", "-c", 'cd "$1" && shift && exec "$@"',
                         "cwd-regression", str(protected)]
        old = subprocess.run(root_launcher + ["sudo", "-n", "-H", "-u", "#" + str(os.getuid())] + probe,
                             capture_output=True, text=True)
        assert old.returncode != 0 and "PermissionError" in old.stderr
        result = subprocess.run(root_launcher + argv[:-2] + probe, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "/"
    finally:
        subprocess.run(["sudo", "-n", "rmdir", str(protected)], check=True)


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


@pytest.fixture
def unregistered_service_bundle(tmp_path, request):
    import uuid
    app = tmp_path.resolve() / "Service Fixture.app"
    contents = app / "Contents"
    for directory in ("MacOS", "Resources", "Library/LaunchAgents"):
        (contents / directory).mkdir(parents=True, exist_ok=True)
    label = "live.jstack.sos-fixture." + uuid.uuid4().hex
    (contents / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "live.jstack.network" if getattr(request, "param", False) else label,
        "CFBundleExecutable": "JStackHub",
        "CFBundlePackageType": "APPL", "CFBundleVersion": "1"}))
    (contents / "Resources/services.json").write_text(json.dumps({
        "absent": label + ".absent.plist", "unused": label + ".unused.plist"}))
    (contents / "Library/LaunchAgents" / (label + ".unused.plist")).write_bytes(plistlib.dumps({
        "Label": label + ".unused", "BundleProgram": "Contents/MacOS/JStackHub",
        "ProgramArguments": ["JStackHub", "status"], "RunAtLoad": False}))
    executable = contents / "MacOS/JStackHub"
    source = Path(__file__).resolve().parents[1] / "macos/ServiceControl.swift"
    subprocess.run(["xcrun", "swiftc", str(source), "-o", str(executable)], check=True)
    subprocess.run(["codesign", "--force", "--sign", "-", str(app)], check=True)
    return executable, label


@pytest.mark.parametrize("role,status", [("absent", "not_found"), ("unused", "not_registered")])
def test_native_unregister_of_absent_service_is_repeatable(unregistered_service_bundle, role, status):
    executable, label = unregistered_service_bundle
    if role == "unused":
        # A never-registered bundled plist reports not_found on macOS. Reach
        # not_registered through a real register/remove cycle, not a mock.
        subprocess.run([str(executable), "register", role], check=True, capture_output=True)
        try:
            registered = json.loads(subprocess.check_output([str(executable), "status"], text=True))
            assert registered[role] == "enabled", registered
            subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}.{role}"],
                           check=True, capture_output=True)
        finally:
            subprocess.run([str(executable), "unregister", role], check=True, capture_output=True)
        assert subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}.{role}"],
                              capture_output=True).returncode != 0
    before = json.loads(subprocess.check_output([str(executable), "status"], text=True))
    assert before[role] == status, before
    for _ in range(3):
        result = subprocess.run([str(executable), "unregister", role], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"service": role, "status": status}
        assert json.loads(subprocess.check_output([str(executable), "status"], text=True)) == before


@pytest.mark.parametrize("unregistered_service_bundle", [False, True], indirect=True)
def test_native_unregister_does_not_hide_loaded_job(unregistered_service_bundle, tmp_path):
    executable, label = unregistered_service_bundle
    label += ".absent"
    definition = tmp_path / "live.plist"
    definition.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": ["/bin/sleep", "60"],
                                          "RunAtLoad": True}))
    info = plistlib.loads((executable.parents[1] / "Info.plist").read_bytes())
    privileged = info["CFBundleIdentifier"] == "live.jstack.network"
    domain = "system" if privileged else "gui/" + str(os.getuid())
    prefix = ["sudo", "-n"] if privileged else []
    if privileged:
        subprocess.run(["sudo", "-n", "chown", "root:wheel", str(definition)], check=True)
    target = domain + "/" + label
    subprocess.run([*prefix, "launchctl", "bootstrap", domain, str(definition)], check=True)
    try:
        result = subprocess.run([str(executable), "unregister", "absent"], capture_output=True, text=True)
        assert result.returncode != 0
        assert "still loaded" in result.stderr
        subprocess.run(["launchctl", "print", target], check=True, capture_output=True)
    finally:
        subprocess.run([*prefix, "launchctl", "bootout", target], check=True)


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

    def approve(*paths, **extra):
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps({"schema": 1, "home": str(Path.home()),
                                       "history": [str(p) for p in paths], "data": [], "apps": [], **extra}))
        subprocess.run(["sudo", "-n", "install", "-o", "root", "-g", "wheel", "-m", "600",
                        str(manifest), str(work / "manifest.json")], check=True)
        return work / "manifest.json"

    try:
        yield approve
    finally:
        subprocess.run(["sudo", "-n", "rm", "-rf", str(work)], check=True)


def erase(eraser, target):
    return subprocess.run(["sudo", "-n", str(eraser), str(target)], capture_output=True, text=True)


def test_preference_inventory_does_not_select_a_domain_with_the_same_prefix(tmp_path):
    domain = "com.example.sos"
    directory = tmp_path / "Library/Preferences/ByHost"
    directory.mkdir(parents=True)
    selected = directory / (domain + "." + str(uuid.uuid4()) + ".plist")
    neighbour = directory / (domain + ".unrelated." + str(uuid.uuid4()) + ".plist")
    for path in (selected, neighbour):
        path.write_bytes(plistlib.dumps({"preserve": True}))
    paths = sos.preference_files(tmp_path, [domain])
    assert selected in paths
    assert neighbour not in paths


@pytest.mark.parametrize("condition", ["normal", "symlink", "immutable", "unreadable", "wrong-user"])
def test_native_preferences_from_launchd_preserve_other_domains_and_root(
        eraser, authorization, tmp_path, condition):
    domain = "com.example.sos-preferences-" + uuid.uuid4().hex
    other = domain + ".unrelated"
    selected = Path.home() / "Library/Preferences" / (domain + ".plist")
    outside = tmp_path / "preserved.plist"
    outside.write_bytes(plistlib.dumps({"sos-sentinel": "must survive"}))
    outside_bytes = outside.read_bytes()

    def defaults(target, *args, root=False, host=False, check=True):
        return subprocess.run((["sudo", "-n", "-H"] if root else [])
                              + ["/usr/bin/defaults", *(["-currentHost"] if host else []),
                                 args[0], target, *args[1:]], capture_output=True, text=True, check=check)

    seeded = []
    try:
        for target, root in ((domain, False), (other, False), (domain, True)):
            for host in (False, True):
                defaults(target, "write", "sos-sentinel", "-string", "synthetic-value", root=root, host=host)
                seeded.append((target, root, host))
                assert defaults(target, "read", "sos-sentinel", root=root, host=host).stdout.strip() == "synthetic-value"
        assert selected.is_file()
        selected_bytes = selected.read_bytes()
        by_host = Path.home() / "Library/Preferences/ByHost"
        archived = by_host / (domain + "." + str(uuid.uuid4()) + ".plist")
        archived.write_bytes(plistlib.dumps({"archived-host": "synthetic-value"}))
        authorization(preferences=[domain], uid=0 if condition == "wrong-user" else os.getuid())
        retained = sos.WORK / "PreferenceErase"
        subprocess.run(["sudo", "-n", "install", "-o", "root", "-g", "wheel", "-m", "700",
                        str(eraser), str(retained)], check=True)
        assert erase(retained, "--verify-preferences").returncode != 0
        if condition == "symlink":
            selected.unlink()
            selected.symlink_to(outside)
        elif condition == "immutable":
            subprocess.run(["chflags", "uchg", str(selected)], check=True)
        elif condition == "unreadable":
            selected.chmod(0)
        command = shlex.join([str(retained), "--preferences"])
        script = "\n".join([
            "set -eu", 'trap \'printf "SOS-KEYCHAIN-EXIT=%s\\n" "$?"\' EXIT',
            "cd /", command, command,
            shlex.join([str(retained), "--verify-preferences"])])
        output, error = launch_keychain_script(script, tmp_path)
        refused = condition != "normal"
        assert f"SOS-KEYCHAIN-EXIT={1 if refused else 0}" in output, (output, error)
        if not refused:
            assert not selected.exists()
            assert not archived.exists()
            assert not list(by_host.glob(domain + ".????????-????-????-????-????????????.plist"))
            for host in (False, True):
                result = defaults(domain, "read", "sos-sentinel", host=host, check=False)
                assert result.returncode == 1 and "does not exist" in result.stderr, result
            # A fresh write must also make the independent verification fail.
            defaults(domain, "write", "new-key", "-string", "recreated")
            assert erase(retained, "--verify-preferences").returncode != 0
        for target, root in ((other, False), (domain, True)):
            for host in (False, True):
                assert defaults(target, "read", "sos-sentinel", root=root, host=host).stdout.strip() == "synthetic-value"
        assert outside.read_bytes() == outside_bytes
        if condition in ("immutable", "wrong-user"):
            assert selected.read_bytes() == selected_bytes
    finally:
        if condition == "symlink" and selected.is_symlink():
            selected.unlink()
        elif condition == "immutable" and selected.exists():
            subprocess.run(["chflags", "nouchg", str(selected)], check=True)
        elif condition == "unreadable" and selected.exists():
            selected.chmod(0o600)
        for target, root, host in seeded:
            defaults(target, "delete", root=root, host=host, check=False)
        if "archived" in locals():
            archived.unlink(missing_ok=True)


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


@pytest.mark.parametrize("copies_only", [False, True])
@pytest.mark.parametrize("mount_is_target", [False, True])
def test_native_refuses_mounted_filesystem_and_recovers_after_detach(
        eraser, authorization, tmp_path, copies_only, mount_is_target):
    base = tmp_path.resolve()
    selected = base / "selected"
    mount = selected / "mounted"
    mount.mkdir(parents=True)
    outside = base / "unrelated.jsonl"
    outside.write_bytes(b"unrelated history survives")
    image = base / "fixture.dmg"
    subprocess.run(["/usr/bin/hdiutil", "create", "-size", "32m", "-fs", "HFS+",
                    "-volname", "SOS Mount Fixture", str(image)], check=True, capture_output=True, timeout=30)
    attached = False
    target = mount if mount_is_target else selected
    command = ["sudo", "-n", str(eraser), *(["--history-copies"] if copies_only else []), str(target)]
    try:
        result = subprocess.run(["/usr/bin/hdiutil", "attach", "-nobrowse", "-mountpoint", str(mount),
                                 "-plist", str(image)], check=True, capture_output=True, timeout=30)
        attached = True
        entities = plistlib.loads(result.stdout)["system-entities"]
        assert any(item.get("mount-point") == str(mount) for item in entities)
        assert mount.stat().st_dev != selected.stat().st_dev
        mounted_history = mount / "preserved.jsonl"
        mounted_history.write_bytes(b"mounted history survives")
        authorization(target)
        refused = subprocess.run(command, capture_output=True, text=True, timeout=30)
        assert refused.returncode != 0, refused.stderr
        assert mounted_history.read_bytes() == b"mounted history survives"
        assert outside.read_bytes() == b"unrelated history survives"
    finally:
        if attached:
            subprocess.run(["/usr/bin/hdiutil", "detach", str(mount)], check=True,
                           capture_output=True, timeout=30)
    assert mount.stat().st_dev == selected.stat().st_dev
    retry = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert retry.returncode == 0, retry.stderr
    assert outside.read_bytes() == b"unrelated history survives"


def test_native_history_sweep_across_mixed_trees_preserves_general_data(eraser, authorization, tmp_path):
    import gzip
    import sqlite3
    import zipfile

    roots = [tmp_path.resolve() / name for name in ("Agents", "Projects")]
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    (outside / "keep.jsonl").write_text("outside history must survive")
    histories, ordinary = [], []
    for root in roots:
        root.mkdir()
        for name, content in {
            "copy.jsonl": '{"message":"session copy"}\n',
            "renamed-claude": '{"type":"assistant","message":{"content":"private"}}\n',
            "renamed-codex": '{"type":"session_meta","payload":{"id":"private"}}\n',
            "activity.log": "private prompt",
            "state.sqlite-wal": "private WAL",
        }.items():
            path = root / name
            path.write_text(content)
            histories.append(path)
        database = root / "renamed-database"
        with sqlite3.connect(database) as db:
            db.execute("create table history(body text)")
            db.execute("insert into history values ('private prompt')")
        histories.append(database)
        archive = root / "renamed-archive"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("conversation.jsonl", '{"private":"history"}')
        histories.append(archive)
        compressed = root / "renamed-compressed"
        compressed.write_bytes(gzip.compress(b"private conversation"))
        histories.append(compressed)
        (root / "nested").mkdir()
        marker = root / "nested/general-data.txt"
        marker.write_text("general data must survive the history sweep")
        ordinary.append(marker)
        (root / "outside-link").symlink_to(outside, target_is_directory=True)
        os.mkfifo(root / "pipe")
    authorization(*roots)
    result = subprocess.run(["sudo", "-n", str(eraser), "--history-copies", *map(str, roots)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert not any(path.exists() for path in histories)
    assert all(path.read_text() == "general data must survive the history sweep" for path in ordinary)
    assert (outside / "keep.jsonl").read_text() == "outside history must survive"
    assert all((root / "pipe").exists() for root in roots)
    # Only now does general deletion proceed, after both roots' histories are gone.
    for root in roots:
        assert erase(eraser, root).returncode == 0
    assert (outside / "keep.jsonl").exists()


def test_native_copy_sweep_checks_all_authorizations_before_deleting(eraser, authorization, tmp_path):
    selected = tmp_path.resolve() / "selected"
    selected.mkdir()
    history = selected / "copy.jsonl"
    history.write_text("private transcript")
    unapproved = tmp_path.resolve() / "not-approved"
    authorization(selected)
    result = subprocess.run(["sudo", "-n", str(eraser), "--history-copies", str(selected), str(unapproved)],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert history.read_text() == "private transcript"


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


@pytest.mark.parametrize("path", ["/", Path.home(), "/Library/LaunchDaemons", "/opt/homebrew",
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
            "keychains": sos.keychain_inventory(Path.home(), os.getuid()),
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
            "keychains": sos.keychain_inventory(Path.home(), os.getuid()),
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


@pytest.mark.parametrize("absolute", [False, True])
def test_shell_alias_cleans_selected_target_and_preserves_link(tmp_path, absolute):
    home = tmp_path.resolve()
    target = home / ".zprofile"
    target.write_text("export UNRELATED=keep\nexport JSTACK_ROOT=/owned\n")
    target.chmod(0o640)
    link = home / ".profile"
    link.symlink_to(target if absolute else ".zprofile")
    (home / ".bash_profile").symlink_to(".profile")
    before = os.readlink(link)
    sos_user_cleanup.clean_shell_settings(home)
    assert target.read_text() == "export UNRELATED=keep\n"
    assert target.stat().st_mode & 0o777 == 0o640
    assert link.is_symlink() and os.readlink(link) == before
    sos_user_cleanup.clean_shell_settings(home)
    assert target.read_text() == "export UNRELATED=keep\n"


@pytest.mark.parametrize("trap", ["outside", "dangling", "cycle"])
def test_shell_alias_traps_refused_before_any_settings_change(tmp_path, trap):
    home = tmp_path.resolve()
    selected = home / ".zprofile"
    original = "export JSTACK_ROOT=/owned\n"
    selected.write_text(original)
    outside = home / "unrelated"
    if trap == "outside":
        outside.write_text(original)
    (home / ".profile").symlink_to(".profile" if trap == "cycle" else outside)
    with pytest.raises(ValueError, match="cycle|leaves selected"):
        sos_user_cleanup.clean_shell_settings(home)
    assert selected.read_text() == original
    if trap == "outside":
        assert outside.read_text() == original


def test_shell_alias_destination_swapped_to_symlink_is_not_followed(monkeypatch, tmp_path):
    home = tmp_path.resolve()
    target = home / ".zprofile"
    target.write_text("export JSTACK_ROOT=/owned\n")
    outside = home / "unrelated"
    outside.write_text("must survive")
    (home / ".profile").symlink_to(target)
    real_rewrite = sos_user_cleanup.rewrite

    def swap(path, transform):
        if path == target:
            target.unlink()
            target.symlink_to(outside)
        return real_rewrite(path, transform)

    monkeypatch.setattr(sos_user_cleanup, "rewrite", swap)
    with pytest.raises(OSError):
        sos_user_cleanup.clean_shell_settings(home)
    assert outside.read_text() == "must survive"


@pytest.mark.parametrize("action", ["reboot", "shutdown", "lock", "wipe"])
@pytest.mark.parametrize("interrupt", [EOFError, KeyboardInterrupt])
def test_confirmation_interrupted(monkeypatch, action, interrupt):
    monkeypatch.setattr(sos.sys, "stdin", type("TTY", (), {"isatty": lambda self: True})())
    def stop(prompt):
        raise interrupt
    monkeypatch.setattr("builtins.input", stop)
    assert not sos.confirm(action, out=io.StringIO())
