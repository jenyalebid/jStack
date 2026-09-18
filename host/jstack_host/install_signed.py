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
    app_services.verify(app, identifier)
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


def install(app: Path, state: Path, *, port=9090, bind="0.0.0.0") -> dict:
    if os.geteuid() == 0:
        raise PermissionError("install user services as the login user")
    if not all(path.is_absolute() for path in (app, state)) or not 1 <= port <= 65535:
        raise ValueError("absolute installation paths and a valid port are required")
    app, state = app.resolve(), state.resolve()
    main_identity = identity(app, "live.jstack.hub")
    settings = {"schema": 1, "app": str(app),
                "port": port, "bind": bind, "environment": {
                    "JREMOTE_STATE_DIR": str(state), "JREMOTE_HOST_PROFILE": "default"}}
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
        for role in ("host", "updater", "menu"):
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
        journal["state"] = "installed"
        atomic_json(path, journal)
        return {"state": "installed", "services": observations(app), "journal": str(path)}


def observations(app: Path) -> dict:
    main = control(app, "status")
    return {role: main[role] for role in ("host", "menu", "updater")}


def provision() -> None:
    """Called only after runtime_entry has applied the saved environment."""
    from . import devices, hostenv, release_channel, releases
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
        configuration["github_repo"] = release_channel.repository(source["github_repo"])
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
            if any(value != "enabled" for value in statuses.values()):
                raise ValueError("service approval changed during verification")
            for role in ("host", "menu", "updater"):
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=9090)
    parser.add_argument("--bind", default="0.0.0.0")
    args = parser.parse_args()
    result = install(args.app, args.state_dir, port=args.port, bind=args.bind)
    print(json.dumps(result))
    return 0 if result["state"] == "installed" else 1
