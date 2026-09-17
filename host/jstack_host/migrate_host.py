"""Exact, journaled legacy host/menu/updater cutover to signed owners.

The request contains reviewed definitions and source observations, not label
prefixes. Keep the request and journal in the machine's private storage.
No identity, pairing credential or release trust is minted by migration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import tempfile
import time

from . import app_services, embed, install_host, install_signed, migrate_services as migration
from . import service_catalog, service_inventory, service_settings
from .update_app import control
from .update_macos import atomic_bytes
from .update_supervisor import atomic_json


ROLES = ("host", "updater", "menu")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hash(path: Path) -> str | None:
    return sha(path.read_bytes()) if path.exists() else None


def target(settings: dict, role: str) -> tuple[Path, str, str]:
    if role == "updater":
        return Path(settings["services_app"]), role, "live.jstack.hub.updater"
    return app_services.specification(Path(settings["app"]), settings, role)


def statuses(settings: dict) -> dict:
    return {role: control(owner, "status")[service]
            for role in ROLES for owner, service, _ in [target(settings, role)]}


def provenance(path: Path) -> dict:
    """Stable code evidence, excluding process IDs and transient launch state."""
    row = service_inventory.inspect_job(path, install_host._domain())
    fields = ("definition", "executable", "executable_file", "signature", "scripts")
    value = {key: row[key] for key in fields}
    if "code_file_not_observed" in row["findings"]:
        raise ValueError("legacy source is unobserved; review module resolution before migration")
    if "module_source" in row:
        value["module_source"] = row["module_source"]
    if any("error" in item or "unobserved" in item
           for item in [value["definition"], value["executable_file"], *value["scripts"]]):
        raise ValueError("legacy code provenance is unobserved; review permissions before migration")
    return value


def verified_pair(settings: dict) -> dict:
    main = install_signed.identity(Path(settings["app"]), "live.jstack.hub")
    recovery = install_signed.identity(Path(settings["services_app"]), "live.jstack.hub.services")
    if any(main.get(key) != recovery.get(key) for key in ("sha", "release", "build", "github_repo")):
        raise ValueError("migration bundles differ in release identity")
    return main


def prepare(request: dict, root: Path) -> Path:
    if os.geteuid() == 0 or not root.is_absolute():
        raise ValueError("user migration requires absolute private storage")
    with migration.exclusive(root):
        return _prepare(request, root)


def _prepare(request: dict, root: Path) -> Path:
    if os.geteuid() == 0:
        raise PermissionError("user migration must not run as root")
    if not root.is_absolute():
        raise ValueError("migration storage must be absolute")
    settings = service_settings.validate(request["settings"])
    if service_settings.read():
        raise ValueError("signed installation already exists; recover its journal")
    if set(request["jobs"]) != set(ROLES) or set(request["provenance"]) != set(ROLES):
        raise ValueError("review all three legacy roles and their code provenance")
    if settings.get("migration_dir") != str(root):
        raise ValueError("installation must retain the selected private migration storage")
    state_value = settings["environment"].get("JREMOTE_STATE_DIR")
    if not state_value or not Path(state_value).is_absolute():
        raise ValueError("existing state must be explicit")
    state = Path(state_value)
    configuration_path = state / "updates/config.json"
    previous_bytes = configuration_path.read_bytes()
    previous = json.loads(previous_bytes)
    snapshots = {"installation": None, "updater": previous_bytes}
    from .install_updater import LABEL as updater_label
    expected_labels = {"host": previous["host_label"], "menu": previous["menubar_label"], "updater": updater_label}
    if any(request["jobs"][role].get("Label") != label for role, label in expected_labels.items()):
        raise ValueError("reviewed jobs do not belong to the existing updater installation")
    if (previous.get("service_model") == "app" or previous.get("team_id") != "MZ95H77RQQ" or
            previous.get("local_url") != f"http://127.0.0.1:{settings['port']}" or
            (state / "host-id").read_text().strip() != previous.get("machine")):
        raise ValueError("existing updater identity or endpoint does not match the request")
    source = verified_pair(settings)
    public = json.loads((Path(settings["app"]) / "Contents/Resources/packages/jstack_host/release-trust.json").read_text())["public_key"]
    if public != previous.get("public_key"):
        raise ValueError("migration cannot rotate release trust")
    host_job = request["jobs"]["host"]
    for key, value in host_job.get("EnvironmentVariables", {}).items():
        if install_host._carries(key) and settings["environment"].get(key) != value:
            raise ValueError("migration cannot change the existing host environment")
    marker = embed.read()
    if marker:
        if (not settings.get("host_capability") or marker.get("agent_label") != host_job["Label"] or
                marker.get("state_dir") != str(state) or marker.get("port") != settings["port"] or
                settings["environment"].get("JREMOTE_TOKEN_PATH") != marker.get("token_path")):
            raise ValueError("embedded identity or owner does not match the request")
    elif settings.get("host_capability"):
        raise ValueError("embedded migration requires its existing declaration")
    else:
        argv = host_job["ProgramArguments"]
        try:
            old_port, old_bind = argv[argv.index("--port") + 1], argv[argv.index("--host") + 1]
        except (ValueError, IndexError):
            raise ValueError("standalone endpoint cannot be observed") from None
        if (argv.count("--port") != 1 or argv.count("--host") != 1 or old_port != str(settings["port"]) or
                old_bind != settings.get("bind", "0.0.0.0")):
            raise ValueError("standalone endpoint differs from its legacy definition")
    new_configuration = {**previous, "service_model": "app", "menubar_path": settings["app"],
                         "menubar_bundle_id": "live.jstack.hub", "services_app": settings["services_app"]}
    for key in ("dispatcher", "runtime_imports", "python", "host_plist", "host_label", "menubar_plist", "menubar_label"):
        new_configuration.pop(key, None)
    if settings.get("host_capability"):
        new_configuration["host_capability"] = settings["host_capability"]
    files = {"installation": (service_settings.path(), settings), "updater": (configuration_path, new_configuration)}
    if settings.get("host_capability"):
        capability = settings["host_capability"]
        service_catalog.validate(capability, host_job)
        manifest = json.loads((Path(settings["services_app"]) / "Contents/Resources/automation-catalog.json").read_text())
        if manifest[capability]["job_sha256"] != service_catalog.digest(host_job):
            raise ValueError("embedded definition differs from the sealed capability")
        path = service_settings.automation_path(settings)
        snapshots["automation"] = path.read_bytes() if path.exists() else None
        automation = json.loads(snapshots["automation"]) if snapshots["automation"] is not None else {}
        if capability in automation and automation[capability] != host_job:
            raise ValueError("private embedding configuration conflicts")
        files["automation"] = (path, {**automation, capability: host_job})
    observed = statuses(settings)
    if any(status not in {"not_found", "not_registered"} for status in observed.values()):
        raise ValueError("new owners already have registration or approval state")
    disabled = migration.disabled_labels()
    records, labels = [], set()
    for role in ROLES:
        job = request["jobs"][role]
        service_catalog.validate("legacy-" + role, job)
        label = job["Label"]
        if label in labels:
            raise ValueError("legacy roles must have distinct jobs")
        labels.add(label)
        path = install_host.plist_path(label)
        if path.is_symlink() or path.stat().st_uid != os.getuid() or plistlib.loads(path.read_bytes()) != job:
            raise ValueError("legacy definition or ownership differs from review")
        evidence = provenance(path)
        if evidence != request["provenance"][role]:
            raise ValueError("legacy code differs from reviewed provenance")
        approval = migration.legacy_status(Path(settings["services_app"]), path)
        loaded = install_host.is_loaded(label)
        if approval not in {"enabled", "not_registered", "not_found", "requires_approval"} or loaded and approval != "enabled":
            raise ValueError("legacy approval is unobservable or ambiguous")
        records.append({"role": role, "label": label, "path": str(path), "sha256": file_hash(path),
                        "provenance": evidence, "approval": approval, "disabled": label in disabled,
                        "loaded": loaded,
                        "enabled": loaded and label not in disabled and approval == "enabled"})
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    journal = Path(tempfile.mkdtemp(prefix="host-", dir=root))
    for record in records:
        data = Path(record["path"]).read_bytes()
        if sha(data) != record["sha256"]:
            raise ValueError("legacy definition changed while preparing")
        atomic_bytes(journal / (record["role"] + ".plist.before"), data, mode=0o600)
    file_records = []
    for name, (path, value) in files.items():
        before = path.read_bytes() if path.exists() else None
        if before != snapshots[name]:
            raise ValueError("configuration changed while preparing")
        if before is not None:
            atomic_bytes(journal / (name + ".before"), before, mode=0o600)
        data = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
        atomic_bytes(journal / (name + ".after"), data, mode=0o600)
        file_records.append({"name": name, "path": str(path), "before": sha(before) if before is not None else None,
                             "after": sha(data)})
    atomic_json(journal / "journal.json", {"schema": 1, "kind": "host", "state": "prepared", "settings": settings,
        "identity": source, "records": records, "files": file_records, "attempted": []})
    return journal


def load(journal: Path) -> dict:
    value = migration.load(journal)
    if value.get("kind") != "host":
        raise ValueError("not a host migration journal")
    if verified_pair(value["settings"]) != value["identity"]:
        raise ValueError("migration artifacts changed")
    return value


def _rollback(journal: Path):
    value = load(journal)
    current = statuses(value["settings"])
    stopped = {record["role"] for record in value["records"]
               if value["state"] == "migrated" and record["enabled"] and
               current[record["role"]] in {"not_registered", "not_found"}}
    if any(status == "requires_approval" for status in current.values()):
        # All three share one configuration. Never restore a legacy owner to
        # evade a denial, or leave old and new updaters using mixed settings.
        value["state"] = "approval_required"
        atomic_json(journal / "journal.json", value)
        return
    if any(status not in {"enabled", "not_registered", "not_found"} for status in current.values()):
        raise ValueError("cannot observe replacement approval during rollback")
    for record in value["files"]:
        if file_hash(Path(record["path"])) not in {record["before"], record["after"]}:
            raise ValueError("configuration changed during migration; refusing overwrite")
        if record["before"] is not None and file_hash(journal / (record["name"] + ".before")) != record["before"]:
            raise ValueError("configuration backup changed")
    for record in value["records"]:
        if file_hash(Path(record["path"])) not in {None, record["sha256"]}:
            raise ValueError("legacy definition changed during migration")
        if file_hash(journal / (record["role"] + ".plist.before")) != record["sha256"]:
            raise ValueError("legacy backup changed")
    for role in reversed(ROLES):
        owner, service, label = target(value["settings"], role)
        if current[role] == "enabled":
            control(owner, "unregister", service)
        if not install_host.wait_unloaded(label):
            raise ValueError("replacement has not stopped")
    for record in reversed(value["files"]):
        path = Path(record["path"])
        if record["before"] is None:
            if path.exists():
                path.unlink()
        else:
            data = (journal / (record["name"] + ".before")).read_bytes()
            if sha(data) != record["before"]:
                raise ValueError("configuration backup changed")
            atomic_bytes(path, data, mode=0o600)
    held = []
    for record in value["records"]:
        if record["role"] not in value["attempted"]:
            continue
        path = Path(record["path"])
        if record["role"] in stopped:
            held.append(record["role"])
            continue
        if not record["enabled"] and not record["disabled"] and record["approval"] != "requires_approval" and not path.exists():
            held.append(record["role"])  # An unregistered original stays OFF at next login too.
            continue
        data = (journal / (record["role"] + ".plist.before")).read_bytes()
        if sha(data) != record["sha256"]:
            raise ValueError("legacy backup changed")
        if not path.exists():
            atomic_bytes(path, data, mode=0o600)
        if record["enabled"] and record["label"] not in migration.disabled_labels():
            approval = migration.legacy_status(Path(value["settings"]["services_app"]), path)
            if approval == "requires_approval":
                held.append(record["role"])
                continue
            if approval not in {"enabled", "not_registered", "not_found"}:
                raise ValueError("legacy approval is unobservable during rollback")
            if not install_host.is_loaded(record["label"]):
                if install_host.bootstrap(record["label"], path).returncode:
                    raise ValueError("legacy job could not be restored")
            if not install_host.is_loaded(record["label"]):
                raise ValueError("restored legacy job is not loaded")
    value.update(state="rolled_back", stopped_originals=held)
    atomic_json(journal / "journal.json", value)


def apply(journal: Path):
    with migration.exclusive(journal.parent):
        value = load(journal)
        if value["state"] != "prepared":
            raise ValueError("migration was attempted; recover it before retrying")
        if any(status not in {"not_registered", "not_found"} for status in statuses(value["settings"]).values()):
            raise ValueError("replacement approval changed after preparation")
        for record in value["files"]:
            if file_hash(Path(record["path"])) != record["before"]:
                raise ValueError("configuration changed after preparation")
            if file_hash(journal / (record["name"] + ".after")) != record["after"]:
                raise ValueError("staged configuration changed")
        for record in value["records"]:
            if provenance(Path(record["path"])) != record["provenance"]:
                raise ValueError("legacy source changed after preparation")
            if install_host.is_loaded(record["label"]) != record["loaded"]:
                raise ValueError("legacy loaded state changed after preparation")
        value["state"] = "applying"
        atomic_json(journal / "journal.json", value)
        try:
            for record in value["records"]:
                path = Path(record["path"])
                if (file_hash(path) != record["sha256"] or
                        migration.legacy_status(Path(value["settings"]["services_app"]), path) != record["approval"] or
                        install_host.is_loaded(record["label"]) != record["loaded"] or
                        (record["label"] in migration.disabled_labels()) != record["disabled"]):
                    raise ValueError("legacy approval or definition changed during cutover")
                value["attempted"].append(record["role"])
                atomic_json(journal / "journal.json", value)
                if install_host.is_loaded(record["label"]):
                    install_host._launchctl("bootout", f"{install_host._domain()}/{record['label']}")
                if not install_host.wait_unloaded(record["label"]):
                    raise ValueError("legacy job has not stopped")
                os.replace(path, journal / (record["role"] + ".retired.plist"))
            for record in value["files"]:
                data = (journal / (record["name"] + ".after")).read_bytes()
                if sha(data) != record["after"]:
                    raise ValueError("staged configuration changed")
                atomic_bytes(Path(record["path"]), data, mode=0o600)
            for record in value["records"]:
                if record["enabled"]:
                    if record["label"] in migration.disabled_labels():
                        raise ValueError("legacy service was disabled during cutover")
                    owner, service, _ = target(value["settings"], record["role"])
                    if control(owner, "register", service)["status"] != "enabled":
                        raise ValueError("replacement requires OS approval")
            verify(value)
            value["state"] = "migrated"
            atomic_json(journal / "journal.json", value)
        except Exception:
            _rollback(journal)
            raise


def verify(value: dict):
    import httpx
    settings = value["settings"]
    config = json.loads((Path(settings["environment"]["JREMOTE_STATE_DIR"]) / "updates/config.json").read_text())
    enabled = {record["role"]: record["enabled"] for record in value["records"]}
    deadline = time.monotonic() + 30
    while True:
        observed = statuses(settings)
        if any(status not in {"enabled", "not_registered", "not_found", "requires_approval"} for status in observed.values()):
            raise ValueError("service approval is unobservable")
        if any((observed[role] == "enabled") != wanted for role, wanted in enabled.items()):
            raise ValueError("service approval differs from the migration snapshot")
        try:
            for role, wanted in enabled.items():
                _, _, label = target(settings, role)
                if install_host.is_loaded(label) != wanted:
                    raise OSError("service lifecycle has not settled")
                if wanted and service_inventory.launch_state(install_host._domain(), label).get("state") != "running":
                    raise OSError("service process has not become running")
            if enabled["host"]:
                token = Path(config["token_path"]).read_text().strip()
                with httpx.Client(timeout=2, trust_env=False) as client:
                    base = config["local_url"] + "/api/jremote/v1"
                    headers = {"Authorization": "Bearer " + token}
                    if client.get(base + "/host").status_code != 401:
                        raise ValueError("migrated host authentication gate is invalid")
                    response = client.get(base + "/host", headers=headers)
                    response.raise_for_status()
                    host = response.json()
                    source = host.get("source", {})
                    if (host.get("host_id") != config["machine"] or source.get("sha") != value["identity"]["sha"] or
                            source.get("release") != value["identity"]["release"] or source.get("dirty")):
                        raise ValueError("migrated host identity or source differs")
                    response = client.get(base + "/sessions/active", headers=headers)
                    response.raise_for_status()
                    if not isinstance(response.json().get("sessions"), list):
                        raise ValueError("migrated sessions API is invalid")
            if enabled["updater"]:
                observation = json.loads((Path(settings["environment"]["JREMOTE_STATE_DIR"]) / "updates/observed.json").read_text())
                source = observation.get("updater_source", {})
                if (source.get("sha") != value["identity"]["sha"] or
                        source.get("release") != value["identity"]["release"] or source.get("dirty")):
                    raise OSError("recovery source has not been observed")
            return
        except (httpx.HTTPError, OSError):
            if time.monotonic() >= deadline:
                raise ValueError("migrated services did not become healthy") from None
            time.sleep(0.25)


def rollback(journal: Path):
    with migration.exclusive(journal.parent):
        _rollback(journal)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    command = commands.add_parser("prepare")
    command.add_argument("--request", type=Path, required=True)
    command.add_argument("--journal-root", type=Path, required=True)
    for action in ("apply", "rollback"):
        commands.add_parser(action).add_argument("journal", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        print(prepare(json.loads(args.request.read_text()), args.journal_root))
    elif args.action == "apply":
        apply(args.journal)
    else:
        rollback(args.journal)


if __name__ == "__main__":
    main()
