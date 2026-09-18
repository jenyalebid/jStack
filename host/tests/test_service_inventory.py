import json
import plistlib
import subprocess

import pytest

from jstack_host import service_inventory as inventory


@pytest.fixture
def observed(monkeypatch):
    monkeypatch.setattr(inventory, "signature", lambda p: {"status": "valid"})
    monkeypatch.setattr(inventory, "launch_state", lambda *a: {"status": "loaded"})


def job(tmp_path, **values):
    path = tmp_path / "service.plist"
    path.write_bytes(plistlib.dumps({"Label": "live.jstack.test", **values}))
    return path


def test_inventory_redacts_arguments_and_environment(tmp_path, observed):
    executable = tmp_path / "service"
    executable.write_text("binary fixture")
    path = job(tmp_path, ProgramArguments=[str(executable), "--token", "secret-token"],
               EnvironmentVariables={"PASSWORD": "secret-password"})
    row = inventory.inspect_job(path, "gui/501")
    rendered = json.dumps(row)
    assert "secret-token" not in rendered and "secret-password" not in rendered
    assert row["executable_file"]["sha256"]
    assert row["definition"]["sha256"]


def test_same_name_changed_command_changes_definition(tmp_path, observed):
    path = job(tmp_path, ProgramArguments=["/bin/echo", "before"])
    before = inventory.inspect_job(path, "gui/501")
    job(tmp_path, ProgramArguments=["/bin/echo", "after"])
    after = inventory.inspect_job(path, "gui/501")
    assert before["label"] == after["label"]
    assert before["definition"]["sha256"] != after["definition"]["sha256"]


def test_definition_encoding_is_not_persistence_drift(tmp_path, observed):
    path = job(tmp_path, ProgramArguments=["/bin/echo", "before"])
    before = inventory.collect([(tmp_path, "gui/501")])
    value = plistlib.loads(path.read_bytes())
    path.write_bytes(plistlib.dumps(value, fmt=plistlib.FMT_BINARY, sort_keys=False))
    assert inventory.compare(before, inventory.collect([(tmp_path, "gui/501")]))["changes"] == []


def test_permission_change_is_persistence_drift(tmp_path, observed):
    path = job(tmp_path, ProgramArguments=["/bin/echo"])
    path.chmod(0o600)
    before = inventory.collect([(tmp_path, "gui/501")])
    path.chmod(0o666)
    changes = inventory.compare(before, inventory.collect([(tmp_path, "gui/501")]))["changes"]
    assert changes[0]["fields"] == ["definition"]


def test_unobservable_signature_never_compares_as_healthy(tmp_path, observed, monkeypatch):
    job(tmp_path, ProgramArguments=["/bin/echo"])
    monkeypatch.setattr(inventory, "signature", lambda _: {"status": "unobservable"})
    current = inventory.collect([(tmp_path, "gui/501")])
    assert inventory.compare(current, current)["unobserved"] == [str(tmp_path / "service.plist")]


def test_module_inventory_detects_package_code_drift_without_importing(tmp_path, observed):
    package = tmp_path / "dashboard"
    package.mkdir()
    (package / "__init__.py").write_text("raise RuntimeError('must not import')")
    (package / "app.py").write_text("application = 1")
    helper = package / "helpers.py"
    helper.write_text("before")
    job(tmp_path, ProgramArguments=["/usr/bin/python3", "-m", "dashboard.app"], WorkingDirectory=str(tmp_path))
    before = inventory.collect([(tmp_path, "gui/501")])
    helper.write_text("after")
    after = inventory.collect([(tmp_path, "gui/501")])
    assert inventory.compare(before, after)["changes"][0]["fields"] == ["module_source"]
    assert before["services"][0]["module_source"]["entry"] == str(package / "app.py")


@pytest.mark.parametrize("configuration", [{}, {"WorkingDirectory": "/no-such-source"},
                                             {"WorkingDirectory": "/tmp", "EnvironmentVariables": {"PYTHONPATH": "relative"}}])
def test_unresolved_module_is_unknown_not_an_approved_runtime(tmp_path, observed, configuration):
    path = job(tmp_path, ProgramArguments=["/usr/bin/python3", "-m", "private_module"], **configuration)
    row = inventory.inspect_job(path, "gui/501")
    assert "code_file_not_observed" in row["findings"]
    assert "unobserved" in row["module_source"]


def test_module_search_does_not_descend_into_protected_app_data(tmp_path, observed, monkeypatch):
    path = job(tmp_path, ProgramArguments=["/usr/bin/python3", "-m", "private_module"],
               WorkingDirectory=str(tmp_path / "Library/Application Support/Other"))
    monkeypatch.setattr(inventory.os, "walk", lambda *a, **k: pytest.fail("protected tree traversed"))
    assert "unobserved" in inventory.inspect_job(path, "gui/501")["module_source"]


def test_root_script_writability_is_independent_of_interpreter_signature(tmp_path, observed, monkeypatch):
    script = tmp_path / "network.sh"
    script.write_text("exit 0")
    path = job(tmp_path, ProgramArguments=["/bin/bash", str(script)])
    monkeypatch.setattr(inventory, "user_writable", lambda p: p == script)
    row = inventory.inspect_job(path, "system")
    assert row["signature"]["status"] == "valid"
    assert row["user_writable_root_code"] == [str(script)]
    assert "root_executes_user_writable_code" in row["findings"]


def test_missing_executable_is_not_healthy(tmp_path, observed):
    path = job(tmp_path, ProgramArguments=[str(tmp_path / "gone")])
    row = inventory.inspect_job(path, "gui/501")
    assert "code_file_unreadable" in row["findings"]


def test_duplicate_labels_are_detected_across_agent_directories(tmp_path, observed):
    user, system = tmp_path / "user", tmp_path / "system"
    user.mkdir()
    system.mkdir()
    for folder in (user, system):
        job(folder, ProgramArguments=["/bin/echo"])
    rows = inventory.collect([(user, "gui/501"), (system, "gui/501")])["services"]
    assert len(rows) == 2
    assert all("duplicate_launchd_label" in r["findings"] for r in rows)


@pytest.mark.parametrize("message,expected", [
    ('Could not find service "test" in domain', "not_loaded"),
    ("Operation not permitted", "unobservable"),
])
def test_launchctl_failure_is_not_always_absence(monkeypatch, message, expected):
    monkeypatch.setattr(inventory, "run", lambda args: subprocess.CompletedProcess(args, 1, "", message))
    assert inventory.launch_state("system", "test")["status"] == expected


def test_launchctl_ignores_nested_resource_state(monkeypatch):
    output = "system/test = {\n\tstate = not running\n\tresources = {\n\t\tstate = active\n\t}\n}"
    monkeypatch.setattr(inventory, "run", lambda args: subprocess.CompletedProcess(args, 0, output, ""))
    assert inventory.launch_state("system", "test")["state"] == "not running"


def test_program_takes_precedence_over_argv_zero(tmp_path, observed):
    path = job(tmp_path, Program="/bin/echo", ProgramArguments=["display-name"])
    assert inventory.inspect_job(path, "gui/501")["executable"] == "/bin/echo"


def test_invalid_plist_is_reported_without_stopping_inventory(tmp_path, observed):
    (tmp_path / "broken.plist").write_text("broken")
    rows = inventory.collect([(tmp_path, "gui/501")])["services"]
    assert rows[0]["findings"] == ["unreadable_or_unsupported_definition"]


def test_service_doctor_does_not_adopt_or_probe_host(monkeypatch):
    from jstack_host import cli
    monkeypatch.setattr(cli, "_adopt", lambda args: pytest.fail("inventory must not adopt host state"))
    monkeypatch.setattr(inventory, "report", lambda **kwargs: 7 if kwargs["as_json"] else 8)
    args = cli.build_parser().parse_args(["doctor", "--services", "--json"])
    assert args.fn(args) == 7


def test_app_data_not_opened_or_codesigned(tmp_path, monkeypatch):
    path = tmp_path / "Library/Application Support/Other/App"
    monkeypatch.setattr(inventory, "run", lambda args: pytest.fail("must not inspect protected app data"))
    assert "unobserved" in inventory.fingerprint(path)
    assert inventory.signature(path)["status"] == "unobservable"
    assert inventory.user_writable(path) is None


def test_app_owned_definition_checks_the_whole_owner_seal(tmp_path, observed, monkeypatch):
    app = tmp_path / "Hub.app"
    definitions = app / "Contents/Library/LaunchAgents"
    definitions.mkdir(parents=True)
    executable = app / "Contents/MacOS/JStackRuntime"
    executable.parent.mkdir()
    executable.write_bytes(b"native fixture")
    path = job(definitions, BundleProgram="Contents/MacOS/JStackRuntime", ProgramArguments=["JStackRuntime", "host"])
    monkeypatch.setattr(inventory, "signature", lambda p: {"status": "invalid" if p == app else "valid"})
    row = inventory.inspect_job(path, "gui/501")
    assert row["executable"] == str(executable)
    assert row["signature"]["status"] == "valid"
    assert "owner_resource_seal_not_verified" in row["findings"]


def test_app_owned_definition_cannot_escape_its_bundle(tmp_path, observed):
    definitions = tmp_path / "Hub.app/Contents/Library/LaunchAgents"
    definitions.mkdir(parents=True)
    path = job(definitions, BundleProgram="../outside", ProgramArguments=["JStackRuntime"])
    assert inventory.inspect_job(path, "gui/501")["findings"] == ["unreadable_or_unsupported_definition"]


def test_baseline_comparison_catches_same_named_script_changes(tmp_path, observed):
    script = tmp_path / "watch.py"
    script.write_text("before")
    job(tmp_path, ProgramArguments=["/bin/echo", str(script)])
    before = inventory.collect([(tmp_path, "gui/501")])
    script.write_text("after")
    after = inventory.collect([(tmp_path, "gui/501")])
    changes = inventory.compare(before, after)["changes"]
    assert len(changes) == 1
    assert changes[0]["fields"] == ["scripts"]


def test_baseline_does_not_treat_a_pid_change_as_persistence_drift(tmp_path, observed):
    job(tmp_path, ProgramArguments=["/bin/echo"])
    before = inventory.collect([(tmp_path, "gui/501")])
    after = inventory.collect([(tmp_path, "gui/501")])
    after["services"][0]["launchd"]["pid"] = "999"
    assert inventory.compare(before, after)["changes"] == []
