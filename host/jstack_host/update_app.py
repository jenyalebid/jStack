"""App-owned service updates, executed by the Hub's own updater service.

State and pairing never move. No interpreter installation, legacy plist writes
or ad-hoc signing are part of application or rollback. The updater replaces
the bundle it runs from: its module closure is preloaded at startup, the
replaced bundle is retained until the hub confirms the release, and the
supervisor process exits after finalization so launchd relaunches it from
the new bundle.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile

from . import release_manifest as releases
from .update_macos import MacBackend, client_distribution, command, safe_tar, stop_app, unpack_app

OBSERVABLE = {"enabled", "requires_approval", "not_registered", "not_found"}


def control(app: Path, action: str, role: str | None = None) -> dict:
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

    def _host_role(self) -> str:
        capability = self.config.get("host_capability")
        return capability if capability else "host"

    def _host_required(self, job: dict) -> bool:
        return job["transaction"]["services"].get(self._host_role()) == "enabled"

    def verify(self, job: dict) -> bool:
        if not super().verify(job):
            return False
        try:
            observed = self._statuses(Path(self.config["menubar_path"]))
            if any(observed.get(role) != expected
                   for role, expected in job["transaction"]["services"].items()):
                return False
            if not self._host_required(job):
                from .app_services import specification
                from .install_host import is_loaded
                _, _, label = specification(Path(self.config["menubar_path"]), self.config, "host")
                if is_loaded(label):
                    return False
            return True
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            return False

    def observe(self, job: dict) -> dict:
        result = super().observe(job)
        try:
            result["services"] = self._statuses(Path(self.config["menubar_path"]))
            if job.get("transaction") and not self._host_required(job):
                # Installed and stopped is distinct from a running release.
                # Report completion only after checking artifacts and OFF now.
                result["verified"] = (bool(job.get("verified")) and
                                      job.get("state") in {"current", "verifying"} and self.verify(job))
                if result["verified"]:
                    result["release"] = job["release"]
                result["host_running"] = False if result["verified"] else None
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            result["verified"] = False
            result["services"] = {"error": "could not observe service approvals"}
        return result

    def activate_runtime(self, job: dict) -> bool:
        # The sealed bundle's modules must never be redirected into a mutable
        # source/dependency folder from a previous installer.
        return False

    def restart_required(self, job: dict) -> bool:
        """Exit the confirmed, finalized supervisor whose bundle was replaced.

        launchd KeepAlive relaunches it from the new bundle; the relaunched
        process sees its own sha equal to the release and keeps running.
        """
        if job.get("state") != "current" or not job.get("finalized"):
            return False
        if "menubar" not in job.get("transaction", {}).get("apps", {}):
            return False
        from . import sourcestamp
        running = sourcestamp.capture().get("sha")
        released = job.get("envelope", {}).get("manifest", {}).get("sources", {}).get("stack")
        return bool(running) and bool(released) and running != released

    def recovery_status(self, job: dict) -> str:
        """Judge an interrupted application by the installed artifact itself."""
        transaction = job.get("transaction", {})
        record = transaction.get("apps", {}).get("menubar")
        if not record:
            return "unknown"
        try:
            self._check_app(Path(record["target"]),
                            transaction["manifest"]["components"]["menubar"], "menubar")
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            return "unknown"
        # The replacement finished before the journal advanced; verification
        # decides whether the release stays.
        return "applied"

    def _definitions(self, app: Path) -> dict[str, str]:
        """role → launchd label, from the bundle's sealed service catalog."""
        import re
        data = json.loads((app / "Contents/Resources/services.json").read_text())
        result = {}
        for role, filename in data.items():
            if (not isinstance(role, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", role)
                    or not isinstance(filename, str)
                    or not re.fullmatch(r"live\.jstack\.[A-Za-z0-9_.-]+\.plist", filename)):
                raise releases.ReleaseError("invalid sealed service definition")
            result[role] = filename.removesuffix(".plist")
        return result

    def _automation_catalog(self, app: Path) -> bytes:
        path = app / "Contents/Resources/automation-catalog.json"
        if not path.exists():
            return b"{}"
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise releases.ReleaseError("automation capability catalog is unreadable")
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

    def _statuses(self, app: Path) -> dict:
        observed = control(app, "status")
        # The updater's own registration is never part of a transaction: this
        # process runs under it, and it must survive the bundle swap so that
        # launchd can relaunch the supervisor from the new bundle.
        observed.pop("updater", None)
        return observed

    def _validate_statuses(self, statuses: dict):
        required = {"host", "menu", self._host_role()}
        if (not isinstance(statuses, dict) or not required <= set(statuses)
                or any(value not in OBSERVABLE for value in statuses.values())):
            raise releases.ReleaseError("cannot observe current app service approvals")

    def stage(self, manifest: dict, directory: Path) -> dict:
        from . import update_plugins
        stage = Path(tempfile.mkdtemp(prefix="app-stage-", dir=directory))
        stack = stage / "stack"
        stack.mkdir()
        safe_tar(directory / manifest["components"]["stack"]["file"], stack)
        app = Path(self.config["menubar_path"])
        statuses = self._statuses(app)
        self._validate_statuses(statuses)
        installed_catalog = self._automation_catalog(app)
        installed_roles = set(self._definitions(app))
        apps = {}
        for kind in ("menubar", "client"):
            if kind == "client" and client_distribution(Path(self.config["client_path"]), self.config) != "hub":
                continue
            folder = stage / kind
            folder.mkdir()
            component = manifest["components"][kind]
            archive = directory / component["file"]
            if kind == "menubar" and installed_catalog != b"{}":
                # Private capability plists never leave this machine, so the
                # published bundle cannot carry them. A locally built variant
                # with the exact release identity replaces the feed artifact.
                variants = self.config.get("local_components")
                if not variants:
                    raise releases.ReleaseError(
                        "installed Hub carries private capabilities; configure local_components")
                archive = Path(variants) / manifest["release"] / "hub-catalog.zip"
                if not archive.is_file():
                    raise releases.ReleaseError(
                        f"private capability build for {manifest['release']} is missing: {archive}")
            candidate = unpack_app(archive, folder)
            self._check_app(candidate, component, kind)
            if kind == "menubar":
                from .sourcestamp import fingerprint
                packages = candidate / "Contents/Resources/packages"
                identity = json.loads((packages / "release-identity.json").read_text())
                if (identity.get("sha") != manifest["sources"]["stack"] or
                        identity.get("release") != manifest["release"] or
                        identity.get("package_sha256") != fingerprint(packages / "jstack_host")):
                    raise releases.ReleaseError("signed app source identity differs from release")
                if self._automation_catalog(candidate) != installed_catalog:
                    raise releases.ReleaseError("changing private capabilities requires a separate migration")
                if set(self._definitions(candidate)) != installed_roles:
                    raise releases.ReleaseError("changing service ownership requires a separate migration")
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
        self._validate_statuses(statuses)
        labels = self._definitions(app)
        # Menu and host first; capabilities after, in a stable order. The
        # updater is not in the snapshot and is never stopped.
        for role in sorted(statuses, key=lambda role: (role != "menu", role != "host", role)):
            if statuses[role] != "enabled":
                continue
            control(app, "unregister", role)
            if not wait_unloaded(labels[role]):
                raise releases.ReleaseError(f"{role} service has not stopped")

    def _restore_services(self, app: Path, statuses: dict):
        self._validate_statuses(statuses)
        for role in sorted(statuses, key=lambda role: (role != "host", role != "menu", role)):
            if statuses[role] != "enabled":
                continue
            current = control(app, "status")[role]
            if current not in OBSERVABLE:
                raise releases.ReleaseError(f"cannot observe {role} service approval during recovery")
            if current == "requires_approval":
                continue  # A later user denial takes precedence over the snapshot.
            result = control(app, "register", role)
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
        # If interrupted between renames there may be no Hub at all; nothing
        # is observable or stoppable until its bundle is back in place.
        if app.exists():
            self._stop_services(app, self._statuses(app))
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
        current = self._statuses(app)
        desired = dict(transaction["services"])
        for role, status in current.items():
            if status == "requires_approval":
                desired[role] = status
        self._restore_services(app, desired)
        client = transaction["apps"].get("client")
        if client and client["was_running"]:
            command(["/usr/bin/open", "-a", client["target"]])
        update_plugins.rollback(transaction["providers"], Path(transaction["stack"]))
