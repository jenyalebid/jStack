"""Journalled migration of exact user jobs into a sealed capability catalog.

No prefix deletion, root jobs, implicit enable, or new host identity. Originals
are retained outside LaunchAgents and can be restored after interruption.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import tempfile
import time

from . import install_host, service_catalog, service_settings, service_inventory
from .update_app import control
from .update_macos import atomic_bytes, command
from .update_supervisor import atomic_json


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def settings_path() -> Path:
    return service_settings.automation_path()


def migration_root() -> Path:
    return Path(service_settings.read().get("migration_dir", Path.home() / ".local/state/jremote/migrations"))


def journal_settings(value: dict) -> Path:
    current = settings_path()
    if value.get("settings_path", str(current)) != str(current):
        raise ValueError("private configuration location changed during migration")
    return current


@contextmanager
def exclusive(root: Path | None = None):
    root = root or migration_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(root / "migration.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def approved_app(app: Path):
    requirement = 'anchor apple generic and certificate leaf[subject.OU] = "MZ95H77RQQ" and identifier "live.jstack.hub.services"'
    command(["/usr/bin/codesign", "--verify", "--deep", "--strict", "-R", "=" + requirement, str(app)])
    command(["/usr/sbin/spctl", "--assess", "--type", "execute", str(app)])


def disabled_labels() -> set[str]:
    result = install_host._launchctl("print-disabled", install_host._domain())
    if result.returncode:
        raise ValueError("cannot observe launchd disabled state")
    entries = re.findall(r'"([^"\n]+)"\s*=>\s*([A-Za-z]+)', result.stdout)
    if "disabled services = {" not in result.stdout or any(value not in {"true", "false", "enabled", "disabled"} for _, value in entries):
        raise ValueError("cannot interpret launchd disabled state")
    return {label for label, value in entries if value in {"true", "disabled"}}


def loaded(label: str) -> bool:
    observed = service_inventory.launch_state(install_host._domain(), label)["status"]
    if observed not in {"loaded", "not_loaded"}:
        raise ValueError("cannot observe launchd registration")
    return observed == "loaded"


def wait_unloaded(label: str, seconds: float = 30) -> bool:
    deadline = time.monotonic() + seconds
    while loaded(label):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)
    return True


def legacy_status(app: Path, path: Path) -> str:
    return control(app, "legacy-status", str(path))["status"]


def prepare(app: Path, catalog: dict) -> Path:
    if os.geteuid() == 0:
        raise PermissionError("user service migration must not run as root")
    app = app.resolve(strict=True)
    approved_app(app)
    _, expected = service_catalog.definitions(catalog)
    manifest = json.loads((app / "Contents/Resources/automation-catalog.json").read_text())
    if any(manifest.get(slug) != value for slug, value in expected.items()):
        raise ValueError("requested migration differs from signed catalog")
    statuses = control(app, "status")
    disabled = disabled_labels()
    records = []
    for slug, job in catalog.items():
        if statuses.get(slug) not in {"not_found", "not_registered"}:
            raise ValueError("destination capability already registered or awaiting approval")
        path = install_host.plist_path(job["Label"])
        if path.is_symlink() or path.stat().st_uid != os.getuid():
            raise ValueError("legacy definition ownership changed")
        if plistlib.loads(path.read_bytes()) != job:
            raise ValueError("legacy definition differs from reviewed catalog")
        was_loaded = loaded(job["Label"])
        approval = legacy_status(app, path)
        if approval not in {"enabled", "requires_approval", "not_registered", "not_found"}:
            raise ValueError("legacy approval status is unknown")
        if was_loaded and approval != "enabled":
            raise ValueError("loaded legacy job has ambiguous approval; explicit review required")
        records.append({"slug": slug, "label": job["Label"], "path": str(path),
                        "sha256": digest(path), "loaded": was_loaded, "approval": approval,
                        "destination_status": statuses[slug],
                        "enabled": was_loaded and job["Label"] not in disabled and approval == "enabled"})
    root = migration_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    journal = Path(tempfile.mkdtemp(prefix="services-", dir=root))
    settings = settings_path()
    previous = settings.read_bytes() if settings.exists() else None
    merged = json.loads(previous) if previous is not None else {}
    for slug, job in catalog.items():
        if slug in merged and merged[slug] != job:
            raise ValueError("existing local capability configuration conflicts")
        merged[slug] = job
    if previous is not None:
        atomic_bytes(journal / "settings.before", previous, mode=0o600)
    for record in records:
        original = Path(record["path"]).read_bytes()
        if hashlib.sha256(original).hexdigest() != record["sha256"]:
            raise ValueError("legacy definition changed while preparing backup")
        atomic_bytes(journal / (record["slug"] + ".original.plist"), original, mode=0o600)
    atomic_json(journal / "journal.json", {"schema": 1, "app": str(app), "state": "prepared",
        "records": records, "settings_existed": previous is not None,
        "settings_path": str(settings),
        "settings_before_sha256": hashlib.sha256(previous).hexdigest() if previous is not None else None,
        "settings": merged, "attempted": []})
    return journal


def load(journal: Path) -> dict:
    if journal.is_symlink() or journal.stat().st_uid != os.getuid() or journal.stat().st_mode & 0o077:
        raise ValueError("unsafe migration journal")
    value = json.loads((journal / "journal.json").read_text())
    if value.get("schema") != 1:
        raise ValueError("unsupported migration journal")
    return value


def _rollback(journal: Path):
    value = load(journal)
    settings = journal_settings(value)
    app = Path(value["app"])
    approved_app(app)
    held = []
    for record in reversed(value["records"]):
        if record["slug"] not in value["attempted"]:
            continue
        statuses = control(app, "status")
        status = statuses[record["slug"]]
        if status == "requires_approval":
            # Restoring an enabled legacy plist here would defeat the new
            # denial at next login, even if we refrained from bootstrap now.
            held.append(record["slug"])
            continue
        if status == "enabled":
            control(app, "unregister", record["slug"])
            if not wait_unloaded("live.jstack.automation." + record["slug"]):
                raise ValueError("replacement has not stopped; refusing duplicate startup")
        path = Path(record["path"])
        if path.exists() and digest(path) != record["sha256"]:
            raise ValueError("legacy definition changed during migration; refusing overwrite")
        if not path.exists():
            atomic_bytes(path, (journal / (record["slug"] + ".original.plist")).read_bytes(), mode=0o600)
        # A new denial wins over the old snapshot. Never call enable.
        if (record["enabled"] and status != "requires_approval" and
                record["label"] not in disabled_labels() and
                legacy_status(app, path) != "requires_approval" and
                not loaded(record["label"])):
            result = install_host.bootstrap(record["label"], path)
            if result.returncode:
                raise ValueError("legacy service could not be restored")
    value["state"] = "approval_required" if held else "rolled_back"
    value["held"] = held
    if not held and settings.exists() and json.loads(settings.read_text()) == value["settings"]:
        if value["settings_existed"]:
            atomic_bytes(settings, (journal / "settings.before").read_bytes(), mode=0o600)
        else:
            settings.unlink()
    atomic_json(journal / "journal.json", value)


def _apply(journal: Path):
    value = load(journal)
    if value["state"] != "prepared":
        raise ValueError("migration already attempted; recover it before retrying")
    app = Path(value["app"])
    approved_app(app)
    settings = journal_settings(value)
    current = digest(settings) if settings.exists() else None
    if current != value["settings_before_sha256"]:
        raise ValueError("local settings changed since preparation")
    statuses = control(app, "status")
    for record in value["records"]:
        if statuses.get(record["slug"]) != record["destination_status"]:
            raise ValueError("destination approval changed since preparation")
        if digest(Path(record["path"])) != record["sha256"]:
            raise ValueError("legacy definition changed since preparation")
    value["state"] = "applying"
    atomic_json(journal / "journal.json", value)
    atomic_json(settings, value["settings"])
    try:
        for record in value["records"]:
            path = Path(record["path"])
            if (digest(path) != record["sha256"] or
                    legacy_status(app, path) != record["approval"] or
                    (record["enabled"] and record["label"] in disabled_labels())):
                raise ValueError("legacy approval or definition changed during migration")
            value["attempted"].append(record["slug"])
            atomic_json(journal / "journal.json", value)
            if loaded(record["label"]):
                install_host._launchctl("bootout", f"{install_host._domain()}/{record['label']}")
                if not wait_unloaded(record["label"]):
                    raise ValueError("legacy job has not stopped")
            # A recoverable move, not deletion. No job can return at login.
            os.replace(path, journal / (record["slug"] + ".retired.plist"))
            if record["enabled"]:
                result = control(app, "register", record["slug"])
                if result["status"] != "enabled" or not loaded("live.jstack.automation." + record["slug"]):
                    raise ValueError("replacement needs approval or did not load")
        value["state"] = "migrated"
        atomic_json(journal / "journal.json", value)
    except Exception:
        _rollback(journal)
        raise


def apply(journal: Path):
    with exclusive():
        _apply(journal)


def rollback(journal: Path):
    with exclusive():
        _rollback(journal)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--app", type=Path, required=True)
    prepare_parser.add_argument("--catalog", type=Path, required=True)
    for action in ("apply", "rollback"):
        commands.add_parser(action).add_argument("journal", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        print(prepare(args.app, json.loads(args.catalog.read_text())))
    elif args.action == "apply":
        apply(args.journal)
    else:
        rollback(args.journal)


if __name__ == "__main__":
    main()
