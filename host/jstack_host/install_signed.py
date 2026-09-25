"""Fresh installation of a verified Hub already placed on disk.

Run by the sealed runtime, with provisioning in a new process so no module
can retain paths imported before the installation environment was selected.
Existing installations require migration; this entry point never adopts them.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import time

from . import app_services, embed, install_host, service_settings
from .migrate_services import exclusive
from .update_app import control
from .update_macos import command
from .update_supervisor import atomic_json


def journal_path() -> Path:
    return service_settings.path().with_name("install-journal.json")


def identity(app: Path, identifier: str) -> dict:
    """The sealed identity of a bundle this machine is willing to adopt.

    `verify` has already refused anything whose seal does not hold, and has
    already decided which signing identity that seal had to carry. What is
    left here is Gatekeeper, and Gatekeeper is a question about a notarised
    bundle from a Developer ID: a Hub compiled on this Mac has neither, and
    asking anyway is the exact refusal that shut every non-publisher machine
    out of its own build. The fingerprint below is asked of both paths.
    """
    app_services.verify(app, identifier)
    if not app_services.source_built(app):
        command(["/usr/sbin/spctl", "--assess", "--type", "execute", str(app)])
    from .sourcestamp import fingerprint
    packages = app / "Contents/Resources/packages"
    value = json.loads((packages / "release-identity.json").read_text())
    if value.get("package_sha256") != fingerprint(packages / "jstack_host"):
        raise ValueError("installed package differs from its sealed source identity")
    return value


def legacy_present() -> bool:
    return bool(embed.read()) or any(
        install_host.plist_path(label).exists() or install_host.is_loaded(label)
        for label in (install_host.LABEL, "com.jremote.menubar", "com.jremote.updater"))


def install(app: Path, state: Path, *, port=9090, bind="0.0.0.0", scheduler: dict | None = None) -> dict:
    if os.geteuid() == 0:
        raise PermissionError("install user services as the login user")
    if not all(path.is_absolute() for path in (app, state)) or not 1 <= port <= 65535:
        raise ValueError("absolute installation paths and a valid port are required")
    app, state = app.resolve(), state.resolve()
    main_identity = identity(app, "live.jstack.hub")
    settings = {"schema": 1, "app": str(app),
                "port": port, "bind": bind, "environment": {
                    "JREMOTE_STATE_DIR": str(state), "JREMOTE_HOST_PROFILE": "default"}}
    if scheduler:
        settings["scheduler"] = scheduler
    # Refused here rather than at the service's first launch: the journal and
    # the settings file are written below, and a block this machine cannot read
    # back leaves an installation whose own descriptor is invalid.
    service_settings.validate(settings)
    initial_entries = set(state.iterdir()) if state.exists() else set()
    with exclusive():
        path = journal_path()
        if path.exists():
            journal = json.loads(path.read_text())
            if journal.get("settings") != settings or journal.get("identity") != main_identity:
                raise ValueError("another installation transaction requires recovery")
            saved = service_settings.read()
            if not saved and journal["state"] == "prepared" and not journal["attempted"]:
                atomic_json(service_settings.path(), settings)
            elif saved != settings:
                raise ValueError("installation settings changed during the transaction")
            if journal["state"] == "installed":
                # Reinstallation is observation, never an implicit Start.
                return {"state": "installed", "services": observations(app), "journal": str(path)}
        else:
            if service_settings.read() or legacy_present():
                raise ValueError("an existing host requires the migration installer")
            # Do not infer identity ownership from a directory name.
            allowed = {service_settings.path().parent / "migrations"} if state == service_settings.path().parent else set()
            current_entries = set(state.iterdir()) if state.exists() else set()
            if initial_entries or current_entries - allowed:
                raise ValueError("existing state requires reviewed migration")
            if install_host.port_answers(port):
                raise ValueError("the requested endpoint already has a listener")
            before = observations(app)
            if any(value not in {"not_registered", "not_found"} for value in before.values()):
                raise ValueError("existing registrations or approvals require reviewed migration")
            journal = {"schema": 1, "settings": settings, "identity": main_identity,
                       "state": "prepared", "attempted": []}
            atomic_json(path, journal)
            atomic_json(service_settings.path(), settings)
        if legacy_present():
            raise ValueError("legacy ownership appeared during installation; reviewed migration is required")
        if "host" not in journal["attempted"] and install_host.port_answers(port):
            raise ValueError("the requested endpoint acquired a listener during installation")
        command([str(app / "Contents/MacOS/JStackRuntime"), "provision"])
        for role in registered(settings):
            observed = control(app, "status")[role]
            if role not in journal["attempted"]:
                if observed not in {"not_registered", "not_found"}:
                    raise ValueError("approval changed during installation")
                journal["attempted"].append(role)
                atomic_json(path, journal)
                observed = control(app, "register", role)["status"]
            if observed != "enabled":
                journal["state"] = "approval_required" if observed == "requires_approval" else "stopped"
                atomic_json(path, journal)
                return {"state": journal["state"], "service": role, "status": observed, "journal": str(path)}
        command([str(app / "Contents/MacOS/JStackRuntime"), "verify-install"], timeout=45)
        # A fresh install on a machine that still carries the pre-Hub
        # LaunchAgent: the settings already declare the daemon, so what is owed
        # is retiring the second copy.
        adopt_scheduler(app)
        journal["state"] = "installed"
        atomic_json(path, journal)
        return {"state": "installed", "services": observations(app), "journal": str(path)}


#: Every role a Hub bundle seals a plist for, in the order a fresh machine
#: brings them up: the daemon that books work starts after the host it books
#: against. `build_hub.ROLES` writes the plists; nothing here may name one that
#: `Contents/Resources/services.json` does not carry.
ROLES = ("host", "updater", "menu", "scheduler")


def registered(settings: dict) -> tuple:
    """The roles THIS installation registers, in start order.

    Every Hub seals a scheduler plist; whether the OS is ever asked to run it
    is the operator's `--no-scheduler` choice, recorded once in the settings.
    So the sealed catalog is not the answer to "what should be enabled here",
    and a machine that declined the daemon must not read as a failed install.
    """
    return tuple(role for role in ROLES if role != "scheduler" or settings.get("scheduler"))


#: The LaunchAgent `plugins/jstack/bin/jstack-scheduler` writes on a Mac with no
#: Hub, and the one an install from before the scheduler was a Hub role left
#: behind. Registered by no app, so macOS gives it a Login Items row of its own
#: named after whatever interpreter it points at.
LEGACY_SCHEDULER = "com.jstack.scheduler"

#: The daemon's control API port, mirroring `scheduler.config.API_PORT`. Not
#: imported: that module is unsealed code in a checkout, and this runs in the
#: signed process. `host/tests/test_hub_scheduler.py` reads both copies.
SCHEDULER_PORT = 9091


def legacy_scheduler_plist() -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{LEGACY_SCHEDULER}.plist"


#: Legacy environment the sealed role replaces rather than carries. PYTHONPATH
#: and PYTHONHOME are superseded by the import roots on the daemon's argv; the
#: sealed interpreter ignores them anyway.
SUPERSEDED_ENV = frozenset({"PYTHONPATH", "PYTHONHOME"})


def scheduler_declaration(definition: dict) -> dict:
    """The sealed service's settings, read off the LaunchAgent being retired.

    Neither the interpreter nor the plugin root is re-derived. This plist is
    what that machine resolved when it installed, and a fresh derivation would
    move a working daemon onto a different tree — or onto an interpreter that
    cannot import what it needs. `jstack-scheduler` writes the job as
    [python, "-c", "import sys, runpy; sys.path[:0] = [...]; ..."], so both
    facts are in the one string.

    The environment is filtered rather than trusted: the legacy job carries a
    PYTHONPATH the sealed interpreter ignores anyway, and a sealed service is
    not a way to set import paths on a child process.

    What it may not do is drop a setting quietly. This machine's own legacy job
    declared GIT_AUTHOR_* and GIT_COMMITTER_*, and an earlier version of this
    filter discarded all four without a word — which would have moved every
    commit a scheduled job makes onto whatever identity global config happens
    to supply, on the one machine nobody would think to check. A variable this
    function does not recognise stops the adoption and gets named.
    """
    import ast
    argv = definition.get("ProgramArguments") or []
    if len(argv) != 3 or argv[1] != "-c" or not isinstance(argv[0], str):
        raise ValueError("the legacy scheduler job carries no interpreter and import roots to adopt")
    found = re.search(r"sys\.path\[:0\] = (\[[^\]]*\])", argv[2])
    if not found:
        raise ValueError("the legacy scheduler job declares no plugin root")
    try:
        roots = ast.literal_eval(found.group(1))
    except (ValueError, SyntaxError):
        raise ValueError("the legacy scheduler job's import roots are unreadable") from None
    if not roots or not all(isinstance(item, str) for item in roots):
        raise ValueError("the legacy scheduler job's import roots are unreadable")
    environment = definition.get("EnvironmentVariables") or {}
    kept, dropped = {}, []
    for key, value in environment.items():
        if not isinstance(value, str):
            dropped.append(key)
        elif key == "PATH" or key.startswith(("SCHEDULER_", "JSTACK_", "GIT_")):
            kept[key] = value
        elif key in SUPERSEDED_ENV or key.startswith(("DYLD_", "LD_")):
            # Deliberate, and each for a stated reason: the import roots now
            # ride on the command line (`scheduler_command`), and the loader
            # families are what a sealed service must never hand a child.
            continue
        else:
            dropped.append(key)
    if dropped:
        raise ValueError("the legacy scheduler job declares settings this Hub "
                         "cannot carry across: " + ", ".join(sorted(dropped)))
    return {"python": argv[0], "plugin_root": roots[0], "environment": kept}


def scheduler_port(declared: dict) -> int:
    port = (declared.get("environment") or {}).get("SCHEDULER_API_PORT")
    return int(port) if port and str(port).isdigit() else SCHEDULER_PORT


def adopt_scheduler(app: Path) -> dict:
    """Carry a machine off its own scheduler LaunchAgent onto the Hub's service.

    Every Mac that updates into this release has the legacy agent, and nothing
    else would ever write the settings block that registers the sealed role —
    so the update performs the cutover rather than merely permitting it. A
    machine with no such plist and no declaration declined the daemon and stays
    declined; adopting one there would install a daemon nobody asked for.

    The order is the correctness argument. Both copies bind the daemon's single
    port, so the sealed role is registered and the daemon confirmed still
    serving BEFORE the legacy job is retired; the sealed job loses that first
    port race and KeepAlive brings it back once the port is free, which is the
    one window this cutover has and why it ends by waiting for the port rather
    than at the delete.
    """
    if "scheduler" not in app_services.sealed_roles(app):
        return {"state": "unsealed"}
    settings = service_settings.read()
    if not settings or settings.get("app") != str(app):
        return {"state": "not_installed"}
    legacy = legacy_scheduler_plist()
    declared = settings.get("scheduler")
    if not declared:
        if not legacy.exists():
            return {"state": "declined"}
        declared = scheduler_declaration(plistlib.loads(legacy.read_bytes()))
        # Through validate, so an unreadable legacy job fails the cutover here
        # rather than leaving an installation whose own descriptor is invalid.
        settings = service_settings.validate({**settings, "scheduler": declared})
        atomic_json(service_settings.path(), settings)
    observed = control(app, "status")["scheduler"]
    if observed in {"not_registered", "not_found"}:
        observed = control(app, "register", "scheduler")["status"]
    if observed != "enabled":
        return {"state": "approval_required", "status": observed}
    port = scheduler_port(declared)
    if not legacy.exists():
        return {"state": "adopted", "retired": None, "port": port}
    if not install_host.port_answers(port):
        raise ValueError("the scheduler stopped answering before its legacy job could be retired")
    import subprocess
    subprocess.run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{LEGACY_SCHEDULER}"],
                   capture_output=True)
    if not install_host.wait_unloaded(LEGACY_SCHEDULER, seconds=10):
        raise ValueError("the legacy scheduler job has not stopped")
    legacy.unlink(missing_ok=True)
    deadline = time.monotonic() + 60
    while not install_host.port_answers(port):
        if time.monotonic() >= deadline:
            raise ValueError("the Hub's scheduler service did not take the daemon's port over")
        time.sleep(0.5)
    return {"state": "adopted", "retired": str(legacy), "port": port}


def observations(app: Path) -> dict:
    """What the OS says about every sealed role, declined ones included.

    Scoped to what the owner answered rather than to ROLES: an older Hub seals
    no scheduler plist, and asking it for that role raises a KeyError in the
    middle of uninstalling the machine that has one. Whether a role is one this
    installation should have RUNNING is `registered`, from the settings — the
    two questions were one, and a declined daemon read as a failed install.
    """
    main = control(app, "status")
    return {role: main[role] for role in ROLES if role in main}


def provision() -> None:
    """Called only after runtime_entry has applied the saved environment."""
    from . import build_source, devices, hostenv, releases
    from .update_macos import bundle_info, client_distribution
    settings = service_settings.read()
    journal = json.loads(journal_path().read_text())
    if journal.get("settings") != settings:
        raise ValueError("provisioning does not match the installation transaction")
    app = Path(settings["app"])
    source = identity(app, "live.jstack.hub")
    if source != journal["identity"]:
        raise ValueError("installation source changed before provisioning")
    public = json.loads((app / "Contents/Resources/packages/jstack_host/release-trust.json").read_text())["public_key"]
    state = hostenv.state_dir()
    if str(state) != settings["environment"]["JREMOTE_STATE_DIR"]:
        raise ValueError("provisioning resolved a different state directory")
    config_path = state / "updates/config.json"
    if config_path.exists():
        from .install_updater import repair_native
        repair_native(public, settings, state_dir=state, candidate_test=False)
        return
    token, minted = install_host.mint_token(hostenv.token_path())
    if minted:
        devices.adopt_master_token(token)
    if not devices.internal_token():
        raise ValueError("the local administrative credential is revoked")
    client = Path("/Applications/jRemote.app")
    info = bundle_info(client) if client.exists() else {}
    configuration = {"public_key": public, "team_id": "MZ95H77RQQ", "service_model": "app",
                     "machine": hostenv.host_id(), "managed": False, "candidate_test": False,
                     "local_url": f"http://127.0.0.1:{settings['port']}",
                     "token_path": str(devices._credential_dir() / "internal-token"),
                     "menubar_path": str(app), "menubar_bundle_id": "live.jstack.hub",
                     "client_path": str(client),
                     "client_bundle_id": info.get("CFBundleIdentifier", ""),
                     "client_managed": client_distribution(client, {"client_managed": False}) == "hub",
                     "feed_dir": str(releases.RELEASE_DIR.parent / "fleet")}
    if source.get("github_repo"):
        configuration["github_repo"] = build_source.repository(source["github_repo"])
    # The ref this bundle was built from, read out of the bundle's own sealed
    # release identity. Nothing else here knows it: not the caller, not the
    # state dir a fresh install refuses to find anything in. While the bundle
    # did not record it this line was a constant — every branch install, and
    # every reinstall of a hub already moved onto a branch, was provisioned to
    # follow stable and rebuilt itself off main from its next update onward.
    configuration["channel"] = build_source.channel_ref(source)
    atomic_json(config_path, configuration)


def verify_install() -> None:
    import httpx
    from . import hostenv
    settings = service_settings.read()
    config = json.loads((hostenv.state_dir() / "updates/config.json").read_text())
    token = Path(config["token_path"]).read_text().strip()
    expected = json.loads(journal_path().read_text())["identity"]
    deadline = time.monotonic() + 30
    while True:
        try:
            with httpx.Client(timeout=2, trust_env=False) as client:
                base = config["local_url"] + "/api/jremote/v1"
                headers = {"Authorization": "Bearer " + token}
                response = client.get(base + "/host", headers=headers)
                response.raise_for_status()
                observed = response.json()
                if (observed["host_id"] != config["machine"] or observed.get("source", {}).get("sha") != expected["sha"] or
                        observed.get("source", {}).get("release") != expected["release"] or observed.get("source", {}).get("dirty")):
                    raise ValueError("running host identity differs from the installation")
                response = client.get(base + "/sessions/active", headers=headers)
                response.raise_for_status()
                if not isinstance(response.json().get("sessions"), list):
                    raise ValueError("authenticated sessions response is invalid")
                if client.get(base + "/sessions/active").status_code != 401:
                    raise ValueError("host authentication gate is not enforcing credentials")
            statuses = observations(Path(settings["app"]))
            expect = registered(settings)
            if any(statuses[role] != "enabled" for role in expect):
                raise ValueError("service approval changed during verification")
            for role in expect:
                live = install_host._launchctl("print", f"{install_host._domain()}/live.jstack.hub.{role}")
                if live.returncode or not re.search(r"\bstate = running\b", live.stdout):
                    raise OSError("an installed service has not started")
            observation = json.loads((hostenv.state_dir() / "updates/observed.json").read_text())
            updater = observation.get("updater_source", {})
            if updater.get("sha") != expected["sha"] or updater.get("release") != expected["release"] or updater.get("dirty"):
                raise ValueError("running recovery identity differs from the installation")
            return
        except (httpx.HTTPError, OSError):
            if time.monotonic() >= deadline:
                raise ValueError("installed host did not become healthy") from None
            time.sleep(0.25)


def uninstall(app: Path, *, purge: bool = False) -> dict:
    """Remove everything this bundle's install placed, tolerating wreckage.

    The installer's refusal gates protect a healthy machine from a blind
    install; an uninstall faces the opposite duty — a half-broken machine is
    exactly where it runs, so no unreadable settings file, missing journal or
    dead service may stop it. Every step attempts, the end state is what gets
    verified.
    """
    import shutil
    problems = []
    # The daemon that spawns work stops before the services it books against.
    for role in ("scheduler", "updater", "menu", "host"):
        try:
            if control(app, "status").get(role) in {"enabled", "requires_approval"}:
                control(app, "unregister", role)
        except Exception as error:
            problems.append(f"{role}: {error}")
    # Derived from ROLES, not written out: a label missing from this list is a
    # service still loaded after its bundle is gone, which the next install
    # then refuses over. Booting out a label that never loaded costs nothing.
    for label in (f"live.jstack.hub.{role}" for role in ROLES):
        import subprocess
        subprocess.run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
                       capture_output=True)
        if not install_host.wait_unloaded(label, seconds=10):
            problems.append(f"{label} is still loaded")
    state = service_settings.path().parent
    removed = {"services": not problems}
    wrapper = Path.home() / ".local/bin/jstack-host"
    if wrapper.exists():
        wrapper.unlink(missing_ok=True)
    if purge:
        shutil.rmtree(state, ignore_errors=True)
        shutil.rmtree(Path.home() / ".local/share/jremote", ignore_errors=True)
        removed["state"] = not state.exists()
    else:
        # An uninstall keeps state and credentials, but the settings and
        # journal describe an installation that no longer exists.
        for name in (service_settings.path(), journal_path()):
            name.unlink(missing_ok=True)
    shutil.rmtree(app, ignore_errors=True)
    removed["bundle"] = not app.exists()
    return {"state": "uninstalled" if not problems and all(removed.values()) else "incomplete",
            "removed": removed, "problems": problems}


def uninstall_main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--purge", action="store_true")
    args = parser.parse_args()
    result = uninstall(args.app, purge=args.purge)
    print(json.dumps(result))
    return 0 if result["state"] == "uninstalled" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=9090)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--scheduler-root", type=Path,
                        help="the jStack plugin directory the scheduler daemon runs from; "
                             "omitted, this Hub registers no scheduler service")
    parser.add_argument("--scheduler-python", type=Path,
                        help="interpreter for the scheduler daemon (default: this bundle's own)")
    parser.add_argument("--scheduler-env", action="append", default=[], metavar="KEY=VALUE",
                        help="a setting frozen into the scheduler service, repeatable")
    args = parser.parse_args()
    scheduler = None
    if args.scheduler_root is not None:
        environment = {}
        for item in args.scheduler_env:
            key, separator, value = item.partition("=")
            if not separator or not key:
                parser.error("--scheduler-env takes KEY=VALUE")
            environment[key] = value
        python = args.scheduler_python or args.app / "Contents/MacOS/JStackPython"
        scheduler = {"python": str(Path(python).absolute()),
                     "plugin_root": str(args.scheduler_root.absolute()),
                     "environment": environment}
    result = install(args.app, args.state_dir, port=args.port, bind=args.bind, scheduler=scheduler)
    print(json.dumps(result))
    return 0 if result["state"] == "installed" else 1
