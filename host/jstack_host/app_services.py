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
            return info.get("CFBundleIdentifier") == "live.jstack.hub"
    return False


def source_built(app: Path) -> bool:
    """Whether this bundle records that the machine reading it built it.

    The marker is `origin` in `release-identity.json`, which sits under
    `Contents/Resources` and is therefore sealed: a bundle that acquires the
    claim after signing breaks the signature, and the requirement `verify`
    then picks is checked against a seal that no longer holds. A publisher's
    release carries no `origin` at all, so it never reaches this path.
    """
    from . import release_manifest as releases
    try:
        identity = json.loads(
            (app / "Contents/Resources/packages/release-identity.json").read_text())
    except (OSError, ValueError):
        return False
    origin = identity.get("origin") if isinstance(identity, dict) else None
    return isinstance(origin, dict) and origin.get("kind") == releases.SOURCE_BUILD


def verify(app: Path, identifier="live.jstack.hub"):
    """Refuse a bundle that is not intact, or not one this machine trusts.

    Two questions, and only the second has ever had more than one answer. The
    seal has to hold over every byte, on every path, and `--verify --deep
    --strict` below is that check whichever requirement it carries — which is
    also why reading the marker off an unverified bundle is safe: a forged one
    fails the very call it selected. Then: who applied the seal. A published
    Hub answers with the publisher's Developer ID team. A Hub this Mac
    compiled for itself cannot — there is no such identity on it — and answers
    the way its manifest already does: the key this machine minted and pinned
    signed the release naming these bytes. The bundle identifier is demanded
    either way; only the signing identity moves.
    """
    requirement = f'=identifier "{identifier}"' if source_built(app) else (
        f'=anchor apple generic and certificate leaf[subject.OU] = "MZ95H77RQQ" '
        f'and identifier "{identifier}"')
    command(["/usr/bin/codesign", "--verify", "--deep", "--strict", "-R", requirement, str(app)])


def specification(app: Path, configuration: dict, role: str) -> tuple[Path, str, str]:
    capability = configuration.get("host_capability") if role == "host" else None
    if capability is None:
        return app, role, "live.jstack.hub." + role
    if not isinstance(capability, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", capability):
        raise ValueError("invalid embedding host capability")
    manifest = json.loads((app / "Contents/Resources/automation-catalog.json").read_text())
    if capability not in manifest:
        raise ValueError("embedding host is not in the sealed capability catalog")
    return app, capability, "live.jstack.automation." + capability


def sealed_roles(app: Path) -> tuple:
    """The bundle's own service roles, told apart from catalogued capabilities.

    Both live in `services.json` under a bare name, and a private catalog may
    legitimately name a capability `scheduler` — the machine this grew up on
    does. The plist filename is what separates them: a sealed role is
    `live.jstack.hub.<role>.plist`, a capability `live.jstack.automation.
    <slug>.plist`. Keyed on the name alone, that capability reads as the Hub's
    scheduler role, and every status lookup answers about the wrong job.
    """
    try:
        catalog = json.loads((app / "Contents/Resources/services.json").read_text())
        roles = [role for role, filename in catalog.items()
                 if filename == f"live.jstack.hub.{role}.plist"]
    except (OSError, ValueError, AttributeError):
        # The floor every Hub has ever sealed. Answering nothing here would
        # under-check a bundle whose catalog is momentarily unreadable, and the
        # callers that act on a bundle verify its seal before asking anyway.
        return ("host", "menu", "updater")
    return tuple(roles)


def observe(app: Path, configuration: dict) -> dict:
    statuses = control(app, "status")
    _, capability, _ = specification(app, configuration, "host")
    if capability != "host":
        statuses["host"] = statuses[capability]
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
    owners = [(Path(configuration["app"]), "live.jstack.hub")]
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
            # The three roles every Hub has ever sealed, and deliberately not
            # the scheduler: the loop below removes whatever the catalog
            # declares, so naming a newer role here would only refuse to
            # uninstall an older bundle that seals no plist for it.
            if not {"host", "menu", "updater"} <= set(catalog):
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
        # Stop automatic update first, then the menu, then the daemon that
        # spawns work, then the services it was booking against.
        order = {"updater": 0, "menu": 1, "scheduler": 2}
        records.sort(key=lambda r: (order.get(r["role"], 3), r["label"]))
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
    print("All Hub user registrations removed; bundle, Network and private data retained", file=out)
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
    """What the Hub says about each role, CROSS-EXAMINED against launchd.

    `control(app, "status")` reads a registration record. A record is a statement of
    intent — it says this role is meant to be running — and on 2026-09-24 the menu's
    record said `enabled` twice while launchd held no such job and no menu bar item
    existed on the screen. Nothing in this command disagreed, so the Hub reported a
    healthy menu at the exact moment the user was looking at its absence, and the only
    way the fault was found was a person noticing an icon missing.

    So every role is asked of launchd as well, not just the host. When the record and
    launchd disagree the line says so and the command exits non-zero, because a
    disagreement is precisely the state that needs a human: the record being `enabled`
    is also what makes a plain `register` a no-op, so the repair is to unregister the
    stale record first and register again.
    """
    app = Path(configuration["app"])
    verify(app)
    observed = observe(app, configuration)
    print(f"owner      {app}", file=out)
    stale = []
    for role in ("host", "menu"):
        registered = observed[role]
        if registered == "enabled" and not install_host.is_loaded(
                specification(app, configuration, role)[2]):
            stale.append(role)
            registered += "  (record only — launchd holds no such job; unregister then "
            registered += "register)"
        print(f"{role:10} {registered}", file=out)
    loaded = install_host.is_loaded(specification(app, configuration, "host")[2])
    print(f"loaded     {'yes' if loaded else 'no'}", file=out)
    probed = configuration["port"] if port is None else port
    served = install_host.health(probed)
    healthy = (install_host.api_answers(probed) if configuration.get("host_capability") else
               bool(served and served.get("service") == "jremote-host"))
    print(f"API        {'answering' if healthy else 'not observed'} on {probed}", file=out)
    return 0 if healthy and loaded and not stale else 1
