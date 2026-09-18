"""Installed signed-service control; never recreate interpreter LaunchAgents."""
from pathlib import Path
import plistlib
import json
import re
import hashlib
import os

from . import install_host
from .update_app import control
from .update_macos import command


def bundled() -> bool:
    for parent in Path(__file__).resolve().parents:
        if parent.suffix == ".app":
            info = plistlib.loads((parent / "Contents/Info.plist").read_bytes())
            return info.get("CFBundleIdentifier") in {"live.jstack.hub", "live.jstack.hub.services"}
    return False


def verify(app: Path, identifier="live.jstack.hub"):
    requirement = f'=anchor apple generic and certificate leaf[subject.OU] = "MZ95H77RQQ" and identifier "{identifier}"'
    command(["/usr/bin/codesign", "--verify", "--deep", "--strict", "-R", requirement, str(app)])


def specification(app: Path, configuration: dict, role: str) -> tuple[Path, str, str]:
    capability = configuration.get("host_capability") if role == "host" else None
    if capability is None:
        return app, role, "live.jstack.hub." + role
    if not isinstance(capability, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", capability):
        raise ValueError("invalid embedding host capability")
    services = Path(configuration["services_app"])
    if not services.is_absolute():
        raise ValueError("services owner must be an absolute bundle path")
    verify(services, "live.jstack.hub.services")
    manifest = json.loads((services / "Contents/Resources/automation-catalog.json").read_text())
    if capability not in manifest:
        raise ValueError("embedding host is not in the sealed capability catalog")
    return services, capability, "live.jstack.automation." + capability


def observe(app: Path, configuration: dict) -> dict:
    statuses = control(app, "status")
    owner, capability, _ = specification(app, configuration, "host")
    if owner != app:
        statuses["host"] = control(owner, "status")[capability]
    return statuses


def repair(configuration: dict, *, port: int, bind: str, state_dir: Path | None, out) -> int:
    app = Path(configuration["app"])
    verify(app)
    if (port != install_host.DEFAULT_PORT and port != configuration["port"] or
            bind != install_host.DEFAULT_BIND and bind != configuration.get("bind", install_host.DEFAULT_BIND) or
            state_dir is not None and str(state_dir.resolve()) != configuration["environment"].get("JREMOTE_STATE_DIR")):
        raise ValueError("repair cannot silently change the installed host identity or endpoint")
    statuses = observe(app, configuration)
    if statuses["host"] != "enabled":
        print(f"host is {statuses['host']}; repair did not enable it; use the explicit Start control", file=out)
        return 1
    served = install_host.wait_for_health(configuration["port"])
    healthy = (install_host.api_answers(configuration["port"]) if configuration.get("host_capability") else
               bool(served and served.get("service") == "jremote-host"))
    if not healthy:
        print("signed host is registered but its API has not become healthy", file=out)
        return 1
    print(f"signed host serving {configuration['port']}; identity and settings retained", file=out)
    return 0


def uninstall_all(configuration: dict, out) -> int:
    """Remove every sealed user-service registration, retaining recovery data."""
    if os.geteuid() == 0:
        raise PermissionError("remove user registrations as the login user")
    from . import service_settings
    from .migrate_services import exclusive, migration_root
    from .update_supervisor import atomic_json
    owners = [(Path(configuration["app"]), "live.jstack.hub"),
              (Path(configuration["services_app"]), "live.jstack.hub.services")]
    if owners[0][0].resolve() == owners[1][0].resolve():
        raise ValueError("service owners must be independent bundles")
    root = migration_root()
    path = root / "uninstall-journal.json"
    previous_path = service_settings.path().with_name("uninstall-journal.json")
    if previous_path != path and previous_path.exists():
        raise ValueError("earlier removal journal requires recovery before changing its storage")
    configuration_digest = hashlib.sha256(json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with exclusive(root):
        records, identities = [], {}
        for app, identifier in owners:
            verify(app, identifier)
            identities[str(app)] = hashlib.sha256((app / "Contents/_CodeSignature/CodeResources").read_bytes()).hexdigest()
            catalog = json.loads((app / "Contents/Resources/services.json").read_text())
            observed = control(app, "status")
            if not isinstance(catalog, dict) or set(observed) != set(catalog):
                raise ValueError("service ownership is unobservable")
            required = {"host", "menu"} if identifier == "live.jstack.hub" else {"updater"}
            if not required <= set(catalog):
                raise ValueError("sealed service owner is incomplete")
            for role, filename in catalog.items():
                if (not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", role) or
                        not isinstance(filename, str) or
                        not re.fullmatch(r"live\.jstack\.[A-Za-z0-9_.-]+\.plist", filename)):
                    raise ValueError("invalid sealed service definition")
                definition = plistlib.loads((app / "Contents/Library/LaunchAgents" / filename).read_bytes())
                label = filename.removesuffix(".plist")
                if definition.get("Label") != label or observed[role] not in {
                        "enabled", "requires_approval", "not_registered", "not_found"}:
                    raise ValueError("service approval or definition is unobservable")
                records.append({"app": str(app), "role": role, "label": label})
        if len({r["label"] for r in records}) != len(records):
            raise ValueError("service owners declare overlapping labels")
        # Stop automatic update first, then the menu, then other capabilities.
        records.sort(key=lambda r: (0 if r["role"] == "updater" else 1 if r["role"] == "menu" else 2, r["label"]))
        if path.exists():
            journal = json.loads(path.read_text())
            if (journal.get("configuration_sha256") != configuration_digest or journal.get("identities") != identities
                    or journal.get("records") != records):
                raise ValueError("service ownership changed during removal")
        else:
            journal = {"schema": 1, "configuration_sha256": configuration_digest, "identities": identities,
                       "records": records, "state": "removing", "attempted": [], "stopped": []}
            atomic_json(path, journal)
        # Re-observe even a completed journal: it is not evidence of current absence.
        for record in records:
            app, role, label = Path(record["app"]), record["role"], record["label"]
            current = control(app, "status").get(role)
            if current not in {"enabled", "requires_approval", "not_registered", "not_found"}:
                raise ValueError("service approval became unobservable during removal")
            if label not in journal["attempted"]:
                journal["attempted"].append(label)
            journal["state"] = "removing"
            atomic_json(path, journal)
            if current in {"enabled", "requires_approval"}:
                control(app, "unregister", role)
            if not install_host.wait_unloaded(label):
                raise ValueError(f"{label} is still loaded")
            if control(app, "status").get(role) not in {"not_registered", "not_found"}:
                raise ValueError(f"{label} registration remains")
            if label not in journal["stopped"]:
                journal["stopped"].append(label)
            atomic_json(path, journal)
        for record in records:
            if (control(Path(record["app"]), "status").get(record["role"]) not in {"not_registered", "not_found"}
                    or not install_host.wait_unloaded(record["label"])):
                raise ValueError("service ownership changed before removal completed")
        journal["state"] = "unregistered"
        atomic_json(path, journal)
    print("All Hub and Services user registrations removed; bundles, Network and private data retained", file=out)
    return 0


def uninstall(configuration: dict, out, *, all_services: bool = False) -> int:
    if all_services:
        return uninstall_all(configuration, out)
    app = Path(configuration["app"])
    verify(app)
    statuses = observe(app, configuration)
    if any(statuses.get(role) not in {"enabled", "requires_approval", "not_registered", "not_found"}
           for role in ("menu", "host")):
        raise ValueError("service approval is unobservable; refusing partial removal")
    for role in ("menu", "host"):
        owner, service, label = specification(app, configuration, role)
        if statuses[role] in {"enabled", "requires_approval"}:
            control(owner, "unregister", service)
        if not install_host.wait_unloaded(label):
            raise ValueError(f"{role} is still registered with launchd")
    print("Hub host and menu unregistered; pairing, state and recovery services retained", file=out)
    return 0


def status(configuration: dict, *, port: int | None, out) -> int:
    app = Path(configuration["app"])
    verify(app)
    observed = observe(app, configuration)
    print(f"owner      {app}", file=out)
    for role in ("host", "menu"):
        print(f"{role:10} {observed[role]}", file=out)
    loaded = install_host.is_loaded(specification(app, configuration, "host")[2])
    print(f"loaded     {'yes' if loaded else 'no'}", file=out)
    probed = configuration["port"] if port is None else port
    served = install_host.health(probed)
    healthy = (install_host.api_answers(probed) if configuration.get("host_capability") else
               bool(served and served.get("service") == "jremote-host"))
    print(f"API        {'answering' if healthy else 'not observed'} on {probed}", file=out)
    return 0 if healthy and loaded else 1
