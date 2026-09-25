"""The scheduler as one of the Hub's own sealed services.

macOS groups Login Items rows by the app that REGISTERED a job, so the only way
this daemon costs no row of its own is to be registered by the Hub like every
other service. These tests hold the two halves of that: the sealed bundle
carries the role, and the plugin's own tool refuses to write a competing plist
wherever a Hub owns it.
"""
from contextlib import nullcontext
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys

import pytest

from jstack_host import app_services, build_hub, install_signed, migrate_host, service_settings

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "plugins/jstack/bin/jstack-scheduler"
DOCTOR = REPO / "plugins/jstack/bin/jstack-doctor"
LEGACY = "com.jstack.scheduler"


# ------------------------------------------------------- the sealed definition


def test_the_sealed_bundle_carries_the_scheduler_role(tmp_path):
    services = build_hub.seal_services(tmp_path, "live.jstack.hub")
    assert services["scheduler"] == "live.jstack.hub.scheduler.plist"
    definition = plistlib.loads((tmp_path / services["scheduler"]).read_bytes())
    # The same runner the host and the updater use. A BundleProgram is resolved
    # inside the bundle, which is what makes the job the app's to register; a
    # `Program` pointing at an interpreter outside it would be the row this
    # change exists to delete.
    assert definition["BundleProgram"] == "Contents/MacOS/JStackRuntime"
    assert definition["ProgramArguments"] == ["JStackRuntime", "scheduler"]
    assert definition["Label"] == "live.jstack.hub.scheduler"
    assert definition["AssociatedBundleIdentifiers"] == ["live.jstack.hub"]
    assert "Program" not in definition and "EnvironmentVariables" not in definition
    # Every sealed role gets a file, so nothing can be in the catalog with no
    # definition behind it or the other way round.
    assert set(services) == set(build_hub.ROLES)
    assert {path.name for path in tmp_path.iterdir()} == set(services.values())


def test_the_runtime_dispatches_the_role_without_importing_the_plugin():
    """The sealed process must not import unsealed code from the checkout."""
    entry = (REPO / "host/macos/runtime_entry.py").read_text()
    body = entry.split('elif role == "scheduler":', 1)[1].split("elif role ==", 1)[0]
    assert "run_scheduler" in body
    assert "scheduler" not in body.replace("run_scheduler", "").replace('role == "scheduler"', "")


# ------------------------------------------------- which roles get registered


def test_declining_the_daemon_registers_no_scheduler_service():
    assert install_signed.registered({}) == ("host", "updater", "menu")
    declared = {"scheduler": {"python": "/usr/bin/python3", "plugin_root": "/p"}}
    assert install_signed.registered(declared) == ("host", "updater", "menu", "scheduler")
    # Every role the installer may register has a sealed plist to register.
    assert set(install_signed.ROLES) == set(build_hub.ROLES)


def test_settings_refuse_a_scheduler_block_that_is_not_absolute():
    base = {"schema": 1, "app": "/Applications/x.app", "port": 9090, "environment": {}}
    for block in ({"python": "python3", "plugin_root": "/p"},
                  {"python": "/usr/bin/python3", "plugin_root": "plugins/jstack"},
                  {"python": "/usr/bin/python3"},
                  {"python": "/usr/bin/python3", "plugin_root": "/p", "root": "/p"}):
        with pytest.raises(ValueError, match="scheduler"):
            service_settings.validate({**base, "scheduler": block})
    good = {"python": "/usr/bin/python3", "plugin_root": "/p", "environment": {"JSTACK_ROOT": "/r"}}
    assert service_settings.validate({**base, "scheduler": good})["scheduler"] == good
    assert service_settings.scheduler({**base, "scheduler": good}) == good
    assert service_settings.scheduler(base) == {}


def test_the_scheduler_environment_cannot_smuggle_an_import_path():
    base = {"schema": 1, "app": "/Applications/x.app", "port": 9090, "environment": {},
            "scheduler": {"python": "/usr/bin/python3", "plugin_root": "/p"}}
    for key in ("PYTHONPATH", "DYLD_INSERT_LIBRARIES", "JREMOTE_STATE_DIR"):
        value = dict(base, scheduler={**base["scheduler"], "environment": {key: "/x"}})
        with pytest.raises(ValueError, match="scheduler environment"):
            service_settings.validate(value)


def test_uninstall_stops_and_boots_out_every_sealed_role(monkeypatch, tmp_path):
    app = tmp_path / "Hub.app"
    app.mkdir()
    statuses = dict.fromkeys(build_hub.ROLES, "enabled")
    unregistered, booted = [], []

    def control(owner, action, role=None):
        if action == "status":
            return dict(statuses)
        unregistered.append(role)
        statuses[role] = "not_registered"
        return {"status": "not_registered"}

    monkeypatch.setattr(install_signed, "control", control)
    monkeypatch.setattr(service_settings, "path", lambda: tmp_path / "state/service-settings.json")
    monkeypatch.setattr(install_signed.install_host, "wait_unloaded",
                        lambda label, seconds=10: booted.append(label) or True)
    result = install_signed.uninstall(app)
    assert result["state"] == "uninstalled", result
    # The daemon that spawns work stops before the services it books against.
    assert unregistered == ["scheduler", "updater", "menu", "host"]
    # A label left out of the bootout sweep is a service still loaded after its
    # bundle is gone, which the next install then refuses over.
    assert booted == [f"live.jstack.hub.{role}" for role in install_signed.ROLES]


# ------------------------------------------- an install that predates the role


def test_migration_tolerates_a_bundle_sealed_before_the_role_existed(tmp_path, monkeypatch):
    app = tmp_path / "Hub.app"
    catalog = app / "Contents/Resources"
    catalog.mkdir(parents=True)
    old = {role: f"live.jstack.hub.{role}.plist" for role in ("host", "menu", "updater")}
    (catalog / "services.json").write_text(json.dumps(old))
    settings = {"app": str(app)}
    assert migrate_host.owned(settings) == ("host", "updater", "menu")
    asked = []
    monkeypatch.setattr(migrate_host, "control",
                        lambda owner, action, role=None: asked.append(action) or dict.fromkeys(old, "not_found"))
    # The KeyError this guards against would land mid-cutover, on a machine
    # whose legacy services are already stopped.
    assert migrate_host.statuses(settings) == dict.fromkeys(old, "not_found")
    (catalog / "services.json").write_text(json.dumps({**old, "scheduler": "live.jstack.hub.scheduler.plist"}))
    assert migrate_host.owned(settings) == ("host", "updater", "menu", "scheduler")


def test_removal_does_not_demand_a_role_older_bundles_never_sealed():
    """The uninstall floor stays at the three roles every Hub has ever had.

    Naming the scheduler here would refuse to uninstall an older bundle, and the
    loop under it removes whatever the catalog declares either way.
    """
    source = (REPO / "host/jstack_host/app_services.py").read_text()
    assert '{"host", "menu", "updater"} <= set(catalog)' in source
    assert '"scheduler"' in source  # the stop order still knows about it


# ------------------------------------------------ the plugin tool's two shapes


def hub_bundle(root: Path, *, scheduler=True, role_status="not_registered", state=None) -> Path:
    """A bundle that answers the two questions the plugin asks of a real one."""
    app = root / "jStack Hub.app"
    (app / "Contents/Resources").mkdir(parents=True)
    (app / "Contents/MacOS").mkdir(parents=True)
    roles = ("host", "menu", "updater") + (("scheduler",) if scheduler else ())
    (app / "Contents/Resources/services.json").write_text(
        json.dumps({role: f"live.jstack.hub.{role}.plist" for role in roles}))
    statuses = {role: "enabled" for role in roles}
    if scheduler:
        statuses["scheduler"] = role_status
    stub(app / "Contents/MacOS/JStackHub", json.dumps(statuses))
    stub(app / "Contents/MacOS/JStackCLI",
         f"name         fixture\nstate        {state or root / 'state'}\ntoken        x")
    return app


def stub(path: Path, output: str):
    path.write_text("#!/bin/sh\ncat <<'OUT'\n" + output + "\nOUT\n")
    path.chmod(0o755)


def tool(*arguments, home: Path, hub: Path | None = None):
    environment = {**os.environ, "HOME": str(home), "JSTACK_ROOT": str(home / "root")}
    environment.pop("JSTACK_SCHEDULER_HUB", None)
    if hub is not None:
        environment["JSTACK_SCHEDULER_HUB"] = str(hub)
    return subprocess.run([sys.executable, str(TOOL), *arguments],
                          capture_output=True, text=True, timeout=120, env=environment)


@pytest.fixture
def home(tmp_path):
    # One agent workspace is enough for `root.resolve` to accept the tree; the
    # seat's name is arbitrary and deliberately not anyone's.
    (tmp_path / "root/Agents/Scout").mkdir(parents=True)
    (tmp_path / "root/Agents/Scout/CLAUDE.md").write_text("# Scout\n")
    (tmp_path / "Library/LaunchAgents").mkdir(parents=True)
    return tmp_path


def test_install_refuses_where_the_hub_owns_the_daemon(home):
    result = tool("install", home=home, hub=hub_bundle(home))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "live.jstack.hub.scheduler" in result.stderr
    # The refusal has to name what manages it instead, or it is a dead end.
    assert "JStackHub" in result.stderr and "install.sh" in result.stderr
    assert not (home / "Library/LaunchAgents" / f"{LEGACY}.plist").exists()


def test_install_still_writes_its_own_service_where_there_is_no_hub(home):
    """The Hub-less Mac is the case `verify/scenarios/plugin/scheduler.sh` runs."""
    result = tool("install", "--dry-run", home=home)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{LEGACY}.plist" in result.stdout
    assert "would write" in result.stdout and "launchctl" in result.stdout


def test_a_hub_too_old_to_seal_the_role_keeps_the_standalone_service(home):
    result = tool("install", "--dry-run", home=home, hub=hub_bundle(home, scheduler=False))
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{LEGACY}.plist" in result.stdout


def test_a_catalogued_capability_named_scheduler_is_not_the_hub_role(home):
    """`services.json` holds both, under bare names. Only the plist tells them
    apart, and a Hub carrying a private catalog really does have this key."""
    hub = hub_bundle(home, scheduler=False)
    catalog = hub / "Contents/Resources/services.json"
    value = json.loads(catalog.read_text())
    catalog.write_text(json.dumps({**value, "scheduler": "live.jstack.automation.scheduler.plist"}))
    result = tool("install", "--dry-run", home=home, hub=hub)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{LEGACY}.plist" in result.stdout
    assert app_services.sealed_roles(hub) == ("host", "menu", "updater")


def test_status_reports_the_hub_service_and_flags_a_surviving_plist(home):
    hub = hub_bundle(home, role_status="enabled")
    result = tool("status", home=home, hub=hub)
    assert "live.jstack.hub.scheduler" in result.stdout and "enabled" in result.stdout
    assert "legacy:" not in result.stdout
    (home / "Library/LaunchAgents" / f"{LEGACY}.plist").write_bytes(
        plistlib.dumps({"Label": LEGACY}))
    result = tool("status", home=home, hub=hub)
    assert result.returncode == 1, result.stdout
    assert "legacy:" in result.stdout and f"{LEGACY}.plist" in result.stdout


def test_status_says_so_where_the_hub_registers_no_daemon(home):
    result = tool("status", home=home, hub=hub_bundle(home))
    assert result.returncode == 3, result.stdout
    assert "not registered" in result.stdout


def test_the_log_path_comes_from_the_bundle_not_a_reconstructed_default(home):
    """A Hub installed against its own state dir keeps its logs there, and a
    guessed path would report a running daemon as one that never started."""
    elsewhere = home / "Volumes/work/state"
    hub = hub_bundle(home, role_status="enabled", state=elsewhere)
    log = elsewhere / "logs/scheduler.log"
    log.parent.mkdir(parents=True)
    log.write_text("scheduler up\n")
    result = tool("logs", home=home, hub=hub)
    assert result.returncode == 0, result.stdout + result.stderr
    assert str(log) in result.stdout and "scheduler up" in result.stdout


def test_the_plugin_never_names_the_hubs_private_state_layout():
    """`plugins/jstack/tests/scrub.sh` gates this; the reason is here.

    The plugin is the public half of the repo. It asks the bundle where things
    are, which is both why it may not spell that directory and why a
    non-default state dir works.
    """
    source = TOOL.read_text()
    assert ".local/state" not in source
    assert "JStackCLI" in source and "JStackHub" in source


def test_an_explicit_label_is_never_taken_over_by_the_hub(home):
    result = tool("status", "--label", "com.someone.else", home=home, hub=hub_bundle(home))
    assert "live.jstack.hub.scheduler" not in result.stdout
    assert "com.someone.else" in result.stdout


# ----------------------------------------------------------------- the doctor


def test_the_doctor_fails_on_a_legacy_plist_beside_the_hub(home):
    """Not a warning: two rows in Login Items, and two claims on one port."""
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("jstack_doctor_under_test", str(DOCTOR))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    doctor = importlib.util.module_from_spec(spec)
    loader.exec_module(doctor)

    hub_line = f"definition: {doctor.HUB_SERVICE} — sealed inside jStack Hub.app"
    legacy_line = f"legacy:     {Path.home()}/Library/LaunchAgents/{LEGACY}.plist survives"

    def status(output, code):
        return lambda argv, **kwargs: subprocess.CompletedProcess(argv, code, output, "")

    import unittest.mock
    with unittest.mock.patch.object(doctor.subprocess, "run", status(hub_line + "\n" + legacy_line + "\n", 1)):
        graded = doctor.check_scheduler()
    assert graded["grade"] == doctor.FAIL and "uninstall" in graded["hint"]
    with unittest.mock.patch.object(doctor.subprocess, "run", status(hub_line + "\nprocess: running\n", 0)):
        assert doctor.check_scheduler()["grade"] == doctor.OK
    with unittest.mock.patch.object(doctor.subprocess, "run", status(hub_line + "; declined\n", 3)):
        graded = doctor.check_scheduler()
    assert graded["grade"] == doctor.WARN and "--no-scheduler" in graded["hint"]
    # A machine with no Hub keeps the old grading: an absent daemon is honest.
    with unittest.mock.patch.object(doctor.subprocess, "run", status("definition: not installed\n", 3)):
        graded = doctor.check_scheduler()
    assert graded["grade"] == doctor.WARN and "jstack-scheduler install" in graded["hint"]


# --------------------------------------------------------------------- parity


def test_the_two_launchers_agree_on_how_the_daemon_is_started():
    """The sealed service and the standalone tool run the same command shape.

    Both put the import roots on the command line rather than in PYTHONPATH,
    because the Hub's interpreter is an isolated build that ignores it. One side
    changing alone is a daemon that starts under one shape and dies under the
    other with "No module named scheduler".
    """
    from jstack_host.local_service import scheduler_command
    plugin = REPO / "plugins/jstack"
    sealed = scheduler_command("/usr/bin/python3", plugin)
    assert sealed[:2] == ["/usr/bin/python3", "-c"]
    assert str(plugin) in sealed[2] and "runpy.run_module('scheduler'" in sealed[2]
    standalone = TOOL.read_text().split("def launch_argv", 1)[1].split("\ndef ", 1)[0]
    assert "sys.path[:0]" in standalone and "runpy.run_module('scheduler'" in standalone
    assert "sys.path[:0]" in sealed[2]


def test_every_file_naming_the_hub_service_names_the_same_label():
    label = build_hub.service_plist("scheduler")["Label"]
    assert label == "live.jstack.hub.scheduler"
    for path in (TOOL, DOCTOR, REPO / "install.sh", REPO / "verify/scenarios/hub/install.sh"):
        assert label in path.read_text(), path
    # And the standalone label stays distinct, or uninstalling one would unload
    # the other.
    assert LEGACY in TOOL.read_text() and not label.startswith(LEGACY)


def test_the_installer_no_longer_writes_a_launch_agent_for_this_daemon():
    installer = (REPO / "install.sh").read_text()
    # The deferral is what wrote the second Login Items row: it waited for the
    # Hub to land and then pointed a hand-written plist at the Hub's own second
    # interpreter, so the row was named after that binary.
    assert "SCHED_PENDING" not in installer
    # The Hub's installer is told where the daemon runs from instead.
    assert "--scheduler-root" in installer
    # `jstack-scheduler install` survives for the machine that has no Hub to own
    # the daemon, and only there — asserted as a reachable branch of the
    # WANT_HOST test rather than as a forbidden string, because the string being
    # absent everywhere is how the Hub-less install lost its scheduler once.
    # `uninstall` carries `install` as a substring and belongs to the teardown
    # path, which is allowed to sweep a standalone daemon from anywhere.
    hubless = [line for line in installer.splitlines()
               if "jstack-scheduler" in line and "install" in line
               and "uninstall" not in line and not line.lstrip().startswith("#")]
    assert hubless, "a Hub-less install has no way left to get a scheduler"
    branch = installer.split('elif [ "$WANT_HOST" = "0" ]', 1)
    assert len(branch) == 2, "the Hub-less scheduler install is not gated on WANT_HOST"
    guarded = branch[1].split("\nfi\n", 1)[0]
    for line in hubless:
        assert line in guarded, (
            f"install.sh installs the standalone daemon outside the Hub-less "
            f"branch, so a Hub machine would get two: {line.strip()}")
    # And an upgraded Mac gets the old row swept rather than keeping it forever.
    assert f"{LEGACY}.plist" in installer and f'bootout "gui/$(id -u)/{LEGACY}"' in installer


def test_no_scenario_still_asserts_the_legacy_plist_on_a_hub_machine():
    for name in ("install", "uninstall", "full-reset"):
        text = (REPO / f"verify/scenarios/hub/{name}.sh").read_text()
        assert f"{LEGACY}.plist" not in text, name
        assert "parent bundle identifier = live.jstack.hub" in text or "LaunchAgents" in text, name
    # The Hub-less scenario is the one place the standalone plist is still the
    # right answer, and it must keep asserting it.
    assert f"{LEGACY}.plist" in (REPO / "verify/scenarios/plugin/scheduler.sh").read_text()


# --------------------------------------- the machine that updates into this build


def legacy_job(root: Path, *, python: str, plugin: str, port: str | None = None) -> Path:
    """The plist `jstack-scheduler install` wrote before the Hub owned the role."""
    path = root / "Library/LaunchAgents" / f"{LEGACY}.plist"
    environment = {"PYTHONPATH": f"{plugin}:{plugin}/vendor", "JSTACK_ROOT": str(root / "root"),
                   "PATH": "/opt/homebrew/bin:/usr/bin:/bin"}
    if port:
        environment["SCHEDULER_API_PORT"] = port
    code = (f"import sys, runpy; sys.path[:0] = ['{plugin}', '{plugin}/vendor']; "
            "runpy.run_module('scheduler', run_name='__main__')")
    path.write_bytes(plistlib.dumps({
        "Label": LEGACY, "ProgramArguments": [python, "-c", code],
        "RunAtLoad": True, "KeepAlive": True, "EnvironmentVariables": environment,
        "StandardOutPath": str(root / "Library/Logs/jstack-scheduler/daemon.out")}))
    return path


def test_the_declaration_is_read_off_the_job_being_retired(home):
    plugin = "/Users/admin/jStack/plugins/jstack"
    python = "/Applications/jStack Hub.app/Contents/MacOS/JStackPython"
    declared = install_signed.scheduler_declaration(
        plistlib.loads(legacy_job(home, python=python, plugin=plugin).read_bytes()))
    assert declared["python"] == python and declared["plugin_root"] == plugin
    # Kept, because the daemon derives its whole tree from it.
    assert declared["environment"]["JSTACK_ROOT"] == str(home / "root")
    assert declared["environment"]["PATH"] == "/opt/homebrew/bin:/usr/bin:/bin"
    # Dropped: a sealed service is not a way to set import paths on a child.
    assert "PYTHONPATH" not in declared["environment"]
    assert install_signed.scheduler_port(declared) == 9091
    ported = install_signed.scheduler_declaration(plistlib.loads(
        legacy_job(home, python=python, plugin=plugin, port="9399").read_bytes()))
    assert install_signed.scheduler_port(ported) == 9399


def test_an_unreadable_legacy_job_fails_the_cutover_loudly(home):
    path = home / "Library/LaunchAgents" / f"{LEGACY}.plist"
    for definition in ({"Label": LEGACY},
                       {"Label": LEGACY, "ProgramArguments": ["/usr/bin/python3", "-m", "scheduler"]},
                       {"Label": LEGACY, "ProgramArguments": ["/usr/bin/python3", "-c", "runpy.run_module('scheduler')"]}):
        path.write_bytes(plistlib.dumps(definition))
        with pytest.raises(ValueError, match="legacy scheduler job"):
            install_signed.scheduler_declaration(plistlib.loads(path.read_bytes()))


@pytest.fixture
def updating(home, monkeypatch):
    """A Mac carrying the current release's legacy agent, taking this build."""
    hub = hub_bundle(home)
    settings_path = home / "state/service-settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({
        "schema": 1, "app": str(hub), "port": 9090, "bind": "0.0.0.0",
        "environment": {"JREMOTE_STATE_DIR": str(home / "state"), "JREMOTE_HOST_PROFILE": "default"}}))
    monkeypatch.setattr(service_settings, "path", lambda: settings_path)
    monkeypatch.setattr(install_signed.Path, "home", staticmethod(lambda: home))
    statuses = {"host": "enabled", "menu": "enabled", "updater": "enabled",
                "scheduler": "not_registered"}
    registered = []

    def control(owner, action, role=None):
        if action == "status":
            return dict(statuses)
        registered.append((action, role))
        statuses[role] = "enabled"
        return {"status": "enabled"}

    monkeypatch.setattr(install_signed, "control", control)
    events = []
    monkeypatch.setattr(install_signed.install_host, "port_answers", lambda port: True)
    monkeypatch.setattr(install_signed.install_host, "wait_unloaded",
                        lambda label, seconds=10: events.append(("unloaded", label)) or True)
    return hub, settings_path, statuses, registered, events


def test_an_update_carries_the_legacy_agent_onto_the_sealed_service(updating, home, monkeypatch):
    hub, settings_path, statuses, registered, events = updating
    plugin = str(home / "jStack/plugins/jstack")
    legacy = legacy_job(home, python=str(hub / "Contents/MacOS/JStackPython"), plugin=plugin)
    booted = []
    monkeypatch.setattr(install_signed, "legacy_scheduler_plist", lambda: legacy)
    import subprocess as sub
    monkeypatch.setattr(sub, "run", lambda argv, **kwargs: booted.append(argv)
                        or sub.CompletedProcess(argv, 0, "", ""))
    result = install_signed.adopt_scheduler(hub)
    assert result["state"] == "adopted" and result["retired"] == str(legacy)
    # The declaration is now the installation's own, read off the retired job.
    declared = json.loads(settings_path.read_text())["scheduler"]
    assert declared["python"] == str(hub / "Contents/MacOS/JStackPython")
    assert declared["plugin_root"] == plugin
    assert install_signed.registered(json.loads(settings_path.read_text()))[-1] == "scheduler"
    assert registered == [("register", "scheduler")]
    # Registered and confirmed serving BEFORE the second copy is retired: both
    # bind one port, and deleting first leaves a window with nothing serving.
    assert [argv[1] for argv in booted] == ["bootout"]
    assert f"gui/{os.getuid()}/{LEGACY}" in booted[0]
    assert events == [("unloaded", LEGACY)]
    # And the row that started all of this is gone.
    assert not legacy.exists()


def test_the_cutover_refuses_to_retire_a_daemon_that_stopped_answering(updating, home, monkeypatch):
    hub, _, _, _, _ = updating
    legacy = legacy_job(home, python="/usr/bin/python3", plugin=str(home / "p"))
    monkeypatch.setattr(install_signed, "legacy_scheduler_plist", lambda: legacy)
    monkeypatch.setattr(install_signed.install_host, "port_answers", lambda port: False)
    with pytest.raises(ValueError, match="stopped answering"):
        install_signed.adopt_scheduler(hub)
    assert legacy.exists()


def test_a_machine_that_declined_the_daemon_is_not_given_one(updating, home, monkeypatch):
    hub, settings_path, _, registered, _ = updating
    monkeypatch.setattr(install_signed, "legacy_scheduler_plist",
                        lambda: home / "Library/LaunchAgents" / f"{LEGACY}.plist")
    assert install_signed.adopt_scheduler(hub)["state"] == "declined"
    assert registered == [] and "scheduler" not in json.loads(settings_path.read_text())


def test_a_bundle_that_seals_no_scheduler_adopts_nothing(updating, home, monkeypatch):
    hub = hub_bundle(home / "old", scheduler=False)
    assert install_signed.adopt_scheduler(hub)["state"] == "unsealed"


def test_a_denied_role_is_reported_rather_than_retried(updating, home, monkeypatch):
    hub, _, statuses, _, _ = updating
    legacy = legacy_job(home, python="/usr/bin/python3", plugin=str(home / "p"))
    monkeypatch.setattr(install_signed, "legacy_scheduler_plist", lambda: legacy)
    monkeypatch.setattr(install_signed, "control",
                        lambda owner, action, role=None: dict(statuses) if action == "status"
                        else {"status": "requires_approval"})
    result = install_signed.adopt_scheduler(hub)
    assert result == {"state": "approval_required", "status": "requires_approval"}
    # Nothing retired behind a denial: that would leave the machine with no
    # scheduler at all until someone answered a dialog.
    assert legacy.exists()


def test_the_update_path_performs_the_cutover():
    """An update is the only thing that reaches every machine that has the row."""
    source = (REPO / "host/jstack_host/update_app.py").read_text()
    body = source.split("def apply(self, job: dict):", 1)[1].split("\n    def ", 1)[0]
    assert "adopt_scheduler" in body
    assert body.index("_restore_services") < body.index("adopt_scheduler")


def test_the_daemon_port_matches_the_package_that_serves_it():
    config = (REPO / "plugins/jstack/scheduler/config.py").read_text()
    assert f'"SCHEDULER_API_PORT", "{install_signed.SCHEDULER_PORT}"' in config


def test_a_catalogued_capability_cannot_shadow_a_sealed_role():
    """The release machine's private catalog declares `scheduler`, so the build
    has to say what to do about it rather than only refuse."""
    for role in build_hub.ROLES:
        with pytest.raises(ValueError, match="sealed Hub service") as raised:
            build_hub.refuse_reserved({role: {"plist": "x", "job_sha256": "y"}})
        assert "--scheduler-root" in str(raised.value)
        assert repr(role) in str(raised.value)
    build_hub.refuse_reserved({"worker": {}, "indexer": {}})
