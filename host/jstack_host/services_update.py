"""Independent, journaled replacement of the signed recovery owner.

Invoke from the Hub's runtime in a user Terminal, never from Services itself.
This maintenance transaction does not implement automatic supervisor handoff.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import sys
import tempfile
import time
import uuid

from . import service_settings
from .migrate_services import approved_app, exclusive, loaded, migration_root, wait_unloaded
from .update_app import control
from .update_macos import command
from .update_supervisor import atomic_json


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def seal(app: Path) -> str:
    approved_app(app)
    return hashlib.sha256((app / "Contents/_CodeSignature/CodeResources").read_bytes()).hexdigest()


def context() -> tuple[dict, Path]:
    settings = service_settings.read()
    owner = Path(settings["services_app"])
    if os.geteuid() == 0:
        raise PermissionError("maintain user services as the login user")
    if owner.resolve() == Path(settings["app"]).resolve():
        raise ValueError("recovery owner must be independent of Hub")
    if any(path.resolve().is_relative_to(owner.resolve()) for path in (Path(sys.executable), Path(__file__))):
        raise ValueError("run Services maintenance from the independent Hub runtime")
    return settings, owner


def catalog(app: Path) -> dict[str, str]:
    data = json.loads((app / "Contents/Resources/services.json").read_text())
    if not isinstance(data, dict) or "updater" not in data:
        raise ValueError("recovery owner catalog is incomplete")
    result = {}
    for role, filename in data.items():
        if (not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", role) or not isinstance(filename, str)
                or not re.fullmatch(r"live\.jstack\.[A-Za-z0-9_.-]+\.plist", filename)):
            raise ValueError("invalid recovery service definition")
        definition = plistlib.loads((app / "Contents/Library/LaunchAgents" / filename).read_bytes())
        label = filename.removesuffix(".plist")
        if definition.get("Label") != label or label in result.values():
            raise ValueError("inconsistent recovery service ownership")
        result[role] = label
    return result


def observe(app: Path, roles: dict) -> dict:
    states = control(app, "status")
    if set(states) != set(roles) or any(state not in {
            "enabled", "requires_approval", "not_registered"} for state in states.values()):
        raise ValueError("recovery service approval is unobservable")
    return states


def automation_catalog(app: Path, roles: dict) -> bytes:
    path = app / "Contents/Resources/automation-catalog.json"
    if not path.exists() and set(roles) == {"updater"}:
        # Public owners built before this file became unconditional contain
        # no optional capabilities. Absence is not valid for private owners.
        return b"{}"
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or set(value) != set(roles) - {"updater"}:
        raise ValueError("recovery capability catalog is incomplete")
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def prepare(candidate: Path) -> Path:
    settings, owner = context()
    with exclusive():
        root = migration_root()
        for existing in root.glob("services-update-*/journal.json"):
            if json.loads(existing.read_text()).get("state") not in {"updated", "rolled_back"}:
                raise ValueError("unfinished Services update requires recovery")
        old_seal, new_seal = seal(owner), seal(candidate)
        roles = catalog(owner)
        if catalog(candidate) != roles:
            raise ValueError("changing service ownership requires a separate migration")
        if automation_catalog(owner, roles) != automation_catalog(candidate, roles):
            raise ValueError("changing private capabilities requires a separate migration")
        states = observe(owner, roles)
        directory = Path(tempfile.mkdtemp(prefix="services-update-", dir=root))
        if directory.stat().st_dev != owner.parent.stat().st_dev:
            raise ValueError("Services update storage must support atomic owner renames")
        previous, incoming = directory / "previous.app", directory / "candidate.app"
        command(["/usr/bin/ditto", str(owner), str(previous)])
        command(["/usr/bin/ditto", str(candidate), str(incoming)])
        if seal(previous) != old_seal or seal(incoming) != new_seal or seal(owner) != old_seal:
            raise ValueError("service owner changed during staging")
        journal = directory / "journal.json"
        atomic_json(journal, {"schema": 1, "owner": str(owner), "settings": digest(settings),
                             "previous_seal": old_seal, "candidate_seal": new_seal,
                             "roles": roles, "states": states, "denied": [], "state": "prepared"})
        return journal


def load(path: Path) -> tuple[dict, Path]:
    settings, owner = context()
    if (path.name != "journal.json" or path.parent.parent.resolve() != migration_root().resolve()
            or not path.parent.name.startswith("services-update-")):
        raise ValueError("Services update journal is outside selected private storage")
    value = json.loads(path.read_text())
    if value.get("schema") != 1 or value.get("settings") != digest(settings) or value.get("owner") != str(owner):
        raise ValueError("Services update configuration changed")
    for name, key in (("previous.app", "previous_seal"), ("candidate.app", "candidate_seal")):
        app = path.parent / name
        if seal(app) != value[key] or catalog(app) != value["roles"]:
            raise ValueError("staged Services owner changed")
    return value, owner


def stop(path: Path, value: dict, owner: Path):
    states = observe(owner, value["roles"])
    value["denied"] = sorted(set(value["denied"]) | {role for role, state in states.items() if state == "requires_approval"})
    atomic_json(path, value)
    for role in sorted(states, key=lambda role: (role != "updater", role)):
        current = observe(owner, value["roles"])[role]
        if current == "requires_approval":
            value["denied"] = sorted(set(value["denied"]) | {role})
            atomic_json(path, value)
        if current == "enabled":
            control(owner, "unregister", role)
        if not wait_unloaded(value["roles"][role]):
            raise ValueError("recovery service is still running")


def restore(path: Path, value: dict, owner: Path):
    for role in value["roles"]:
        state = observe(owner, value["roles"])[role]
        if state == "requires_approval":
            value["denied"] = sorted(set(value["denied"]) | {role})
            atomic_json(path, value)
        if value["states"][role] == "enabled" and role not in value["denied"]:
            result = control(owner, "register", role)
            if result.get("status") != "enabled":
                raise ValueError("updated recovery owner requires approval or repair")
        elif state == "enabled":
            raise ValueError("an OFF recovery service unexpectedly became enabled")
    observed = observe(owner, value["roles"])
    for role, original in value["states"].items():
        enabled = original == "enabled" and role not in value["denied"]
        if (observed[role] == "enabled") != enabled:
            raise ValueError("recovery service state did not converge")
        deadline = time.monotonic() + 30
        while loaded(value["roles"][role]) != enabled:
            if time.monotonic() >= deadline:
                raise ValueError("recovery launchd state did not converge")
            time.sleep(0.25)


def replace(path: Path, value: dict, owner: Path, *, rollback: bool):
    source = path.parent / ("previous.app" if rollback else "candidate.app")
    expected = value["previous_seal" if rollback else "candidate_seal"]
    incoming = owner.with_name(owner.name + ".incoming-" + path.parent.name)
    if rollback and incoming.exists():
        os.replace(incoming, path.parent / ("interrupted-incoming-" + uuid.uuid4().hex + ".app"))
    if incoming.exists():
        if seal(incoming) != expected:
            # Keep interrupted evidence; never delete an unexpected directory.
            raise ValueError("unfinished incoming recovery owner requires review")
    else:
        command(["/usr/bin/ditto", str(source), str(incoming)])
        if seal(incoming) != expected:
            raise ValueError("copied recovery owner changed")
    if owner.exists():
        if seal(owner) not in {value["previous_seal"], value["candidate_seal"]}:
            raise ValueError("recovery owner changed before replacement")
        archive = path.parent / ("replaced.app" if not rollback else "failed.app")
        if archive.exists():
            raise ValueError("prior recovery owner archive requires review")
        os.replace(owner, archive)
    os.replace(incoming, owner)
    if seal(owner) != expected:
        raise ValueError("installed recovery owner changed")


def apply(path: Path):
    with exclusive():
        value, owner = load(path)
        if value["state"] != "prepared":
            raise ValueError("interrupted Services update requires rollback")
        if seal(owner) != value["previous_seal"] or observe(owner, value["roles"]) != value["states"]:
            raise ValueError("recovery owner or approval changed since preparation")
        value["state"] = "applying"
        atomic_json(path, value)
        stop(path, value, owner)
        replace(path, value, owner, rollback=False)
        restore(path, value, owner)
        value["state"] = "updated"
        atomic_json(path, value)


def rollback(path: Path):
    with exclusive():
        value, owner = load(path)
        if owner.exists() and seal(owner) not in {value["previous_seal"], value["candidate_seal"]}:
            raise ValueError("installed recovery owner changed")
        if value["state"] == "rolled_back":
            if not owner.exists() or seal(owner) != value["previous_seal"]:
                raise ValueError("previous recovery owner is no longer installed")
            observe(owner, value["roles"])
            return  # A completed rollback must not undo a later explicit OFF choice.
        if value["state"] == "updated":
            current = observe(owner, value["roles"])
            value["denied"] = sorted(set(value["denied"]) | {role for role, state in current.items() if state != "enabled"})
        value["state"] = "rolling_back"
        atomic_json(path, value)
        if owner.exists():
            stop(path, value, owner)
        if not owner.exists() or seal(owner) != value["previous_seal"]:
            replace(path, value, owner, rollback=True)
        restore(path, value, owner)
        value["state"] = "rolled_back"
        atomic_json(path, value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "apply", "rollback"))
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        print(prepare(args.path.resolve()))
    elif args.action == "apply":
        apply(args.path.resolve())
    else:
        rollback(args.path.resolve())


if __name__ == "__main__":
    main()
