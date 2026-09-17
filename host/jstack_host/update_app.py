"""App-owned service updates, executed by a separately installed recovery app.

State and pairing never move. No interpreter installation, legacy plist writes
or ad-hoc signing are part of application or rollback.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile

from . import release_manifest as releases
from .update_macos import MacBackend, client_distribution, command, safe_tar, stop_app, unpack_app


def control(app: Path, action: str, role: str | None = None) -> dict:
    import json
    arguments = [str(app / "Contents/MacOS/JStackHub"), action]
    if role is not None:
        arguments.append(role)
    return json.loads(command(arguments, timeout=30))


class AppBackend(MacBackend):
    def _app_running(self, kind: str, path: Path) -> list[int]:
        if kind != "menubar":
            return super()._app_running(kind, path)
        import psutil
        executable = str(path / "Contents/MacOS/JStackHostBar")
        result = []
        for pid in psutil.pids():
            try:
                if psutil.Process(pid).exe() == executable:
                    result.append(pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return result

    def _running_required(self, kind: str, record: dict, transaction: dict) -> bool:
        if kind == "menubar":
            return transaction.get("services", {}).get("menu", "enabled") == "enabled"
        return record["was_running"]

    def verify(self, job: dict) -> bool:
        if not super().verify(job):
            return False
        try:
            observed = self._statuses(Path(self.config["menubar_path"]))
            return all((observed[role] == "enabled") == (expected == "enabled")
                       for role, expected in job["transaction"]["services"].items())
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            return False

    def activate_runtime(self, job: dict) -> bool:
        # The recovery application's sealed modules must never be redirected
        # into a mutable source/dependency folder from a previous installer.
        return False

    def _statuses(self, app: Path) -> dict:
        observed = control(app, "status")
        from .app_services import specification
        owner, capability, _ = specification(app, self.config, "host")
        if owner != app:
            observed["host"] = control(owner, "status")[capability]
        return observed

    def stage(self, manifest: dict, directory: Path) -> dict:
        from . import update_plugins
        stage = Path(tempfile.mkdtemp(prefix="app-stage-", dir=directory))
        stack = stage / "stack"
        stack.mkdir()
        safe_tar(directory / manifest["components"]["stack"]["file"], stack)
        app = Path(self.config["menubar_path"])
        statuses = self._statuses(app)
        if set(statuses) != {"host", "menu"} or any(value not in {
                "enabled", "requires_approval", "not_registered"} for value in statuses.values()):
            raise releases.ReleaseError("cannot observe current app service approvals")
        apps = {}
        for kind in ("menubar", "client"):
            if kind == "client" and client_distribution(Path(self.config["client_path"]), self.config) != "hub":
                continue
            folder = stage / kind
            folder.mkdir()
            component = manifest["components"][kind]
            candidate = unpack_app(directory / component["file"], folder)
            self._check_app(candidate, component, kind)
            if kind == "menubar":
                import json
                from .sourcestamp import fingerprint
                packages = candidate / "Contents/Resources/packages"
                identity = json.loads((packages / "release-identity.json").read_text())
                if (identity.get("sha") != manifest["sources"]["stack"] or
                        identity.get("release") != manifest["release"] or
                        identity.get("package_sha256") != fingerprint(packages / "jstack_host")):
                    raise releases.ReleaseError("signed app source identity differs from release")
            target = Path(self.config[kind + "_path"])
            if not os.access(target.parent, os.W_OK):
                raise releases.ReleaseError("app destination is not writable")
            backup = target.with_name(target.name + ".previous-" + stage.name)
            if backup.exists():
                raise releases.ReleaseError("recovery destination already exists")
            from .update_macos import running
            apps[kind] = {"source": str(candidate), "target": str(target), "backup": str(backup),
                          "existed": target.exists(), "was_running": bool(target.exists() and running(target))}
        return {"stage": str(stage), "stack": str(stack), "apps": apps, "services": statuses,
                "release": manifest["release"], "manifest": manifest,
                "providers": update_plugins.prepare()}

    def _stop_services(self, app: Path, statuses: dict):
        from .install_host import wait_unloaded
        from .app_services import specification
        for role in ("menu", "host"):
            if statuses[role] != "enabled":
                continue
            owner, service, label = specification(app, self.config, role)
            control(owner, "unregister", service)
            if not wait_unloaded(label):
                raise releases.ReleaseError(f"{role} service has not stopped")

    def _restore_services(self, app: Path, statuses: dict):
        from .app_services import specification
        for role in ("host", "menu"):
            if statuses[role] != "enabled":
                continue
            owner, service, _ = specification(app, self.config, role)
            current = control(owner, "status")[service]
            if current == "requires_approval":
                continue  # A later user denial takes precedence over the snapshot.
            result = control(owner, "register", service)
            if result["status"] not in {"enabled", "requires_approval"}:
                raise releases.ReleaseError(f"{role} service could not be restored")

    def apply(self, job: dict):
        from . import update_plugins
        transaction = job["transaction"]
        app = Path(self.config["menubar_path"])
        if self._statuses(app) != transaction["services"]:
            raise releases.ReleaseError("service approvals changed since staging")
        update_plugins.install(transaction["providers"], Path(transaction["stack"]))
        self._stop_services(app, transaction["services"])
        for kind, record in transaction["apps"].items():
            target, backup = Path(record["target"]), Path(record["backup"])
            if kind == "client":
                stop_app(target)
            incoming = target.with_name(target.name + ".incoming-" + transaction["release"])
            if incoming.exists():
                raise releases.ReleaseError("unfinished incoming app requires recovery")
            command(["/usr/bin/ditto", record["source"], str(incoming)])
            self._check_app(incoming, transaction["manifest"]["components"][kind], kind)
            if target.exists():
                os.replace(target, backup)
            os.replace(incoming, target)
        self._restore_services(app, transaction["services"])
        client = transaction["apps"].get("client")
        if client and client["was_running"]:
            command(["/usr/bin/open", "-a", client["target"]])

    def rollback(self, job: dict):
        from . import update_plugins
        transaction = job["transaction"]
        app = Path(self.config["menubar_path"])
        # If interrupted between renames there may be no Hub at all. Recovery
        # still runs from its independent bundle and can put the old one back.
        current = self._statuses(app) if app.exists() else {}
        if current:
            self._stop_services(app, current)
        for kind, record in transaction["apps"].items():
            target, backup = Path(record["target"]), Path(record["backup"])
            if not backup.exists():
                continue
            if kind == "client":
                stop_app(target)
            if target.exists():
                failed = target.with_name(target.name + ".failed-" + job["id"])
                if failed.exists():
                    raise releases.ReleaseError("failed candidate archive already exists")
                os.replace(target, failed)
            os.replace(backup, target)
        desired = dict(transaction["services"])
        for role, status in current.items():
            if status == "requires_approval":
                desired[role] = status
        self._restore_services(app, desired)
        client = transaction["apps"].get("client")
        if client and client["was_running"]:
            command(["/usr/bin/open", "-a", client["target"]])
        update_plugins.rollback(transaction["providers"], Path(transaction["stack"]))
