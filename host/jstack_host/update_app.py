"""App-owned service updates, executed by the Hub's own updater service.

State and pairing never move. No interpreter installation, legacy plist writes
or ad-hoc signing are part of an application. The updater replaces the bundle
it runs from: its module closure is preloaded at startup, the replaced bundle
survives only until the transaction closes, and the supervisor process exits
so launchd relaunches it from whichever bundle is installed when the job ends.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile

from . import release_manifest as releases
from .update_macos import (MacBackend, client_distribution, command, safe_tar, stop_app,
                           sweep, unpack_app)

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
        """Exit a supervisor whose own bundle was replaced under it.

        launchd KeepAlive relaunches it from the bundle now at the target; the
        relaunched process sees its own sha equal to the installed release and
        keeps running, which is what ends the loop.

        A settled failure counts, not only a confirmed success: the swap can
        already have happened when the job failed, and the old rollback left
        the machine executing the release it had just rejected until someone
        rebooted it (#119).
        """
        settled = (job.get("state") == "current" and job.get("finalized")) or (
            job.get("state") == "failed" and job.get("applied"))
        if not settled:
            return False
        if "menubar" not in job.get("transaction", {}).get("apps", {}):
            return False
        from . import sourcestamp
        running = sourcestamp.capture().get("sha")
        released = job.get("envelope", {}).get("manifest", {}).get("sources", {}).get("stack")
        return bool(running) and bool(released) and running != released

    def recovery_status(self, job: dict) -> str:
        """Judge an interrupted application by the transaction's own files.

        apply() replaces each app in two renames: target -> backup, then
        incoming -> target. Only those paths say how far it got. The installed
        bundle's version cannot: two releases built on the same day carry the
        same CFBundleVersion, so an untouched prior would pass as the copy.
        """
        transaction = job.get("transaction", {})
        apps = transaction.get("apps", {})
        if "menubar" not in apps or not job.get("id"):
            return "unknown"
        for kind, record in apps.items():
            target, backup = Path(record["target"]), Path(record["backup"])
            incoming = target.with_name(target.name + ".incoming-" + job["id"])
            if incoming.exists():
                return "unknown"  # the copy never finished, or the second rename never ran
            if record.get("existed", True) and not backup.exists():
                return "unknown"  # the swap never began; the running release is untouched
            if not target.exists():
                return "unknown"  # cut between the two renames
            try:
                self._check_app(target, transaction["manifest"]["components"][kind], kind,
                                source_build=self._built_here(job))
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                return "unknown"
        # Every replacement finished before the journal advanced; verification
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

    def _check_ownership(self, candidate: Path, installed: Path):
        """What may change between the installed bundle and the candidate.

        A sealed role the candidate adds is the update itself: `apply` ends by
        adopting the scheduler, which is how a machine gains that role. A private
        capability that vanishes from the catalog while the candidate seals a
        role of the same name is the same cutover seen from the machine whose
        scheduler was catalogued — the one this grew up on — and the snapshot
        carries it: the capability is stopped under its old label and the role
        registered under its new one. Every other change to the catalog, and
        any role the candidate drops, is a separate migration.
        """
        from .app_services import sealed_roles
        sealed = set(sealed_roles(candidate))
        installed_roles, candidate_roles = set(self._definitions(installed)), set(self._definitions(candidate))
        if installed_roles - candidate_roles or (candidate_roles - installed_roles) - sealed:
            raise releases.ReleaseError("changing service ownership requires a separate migration")
        before = json.loads(self._automation_catalog(installed))
        after = json.loads(self._automation_catalog(candidate))
        cutover = {name for name in before if name in sealed and name not in after}
        if {name: job for name, job in before.items() if name not in cutover} != after:
            raise releases.ReleaseError("changing private capabilities requires a separate migration")

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
        apps = {}
        built_here = self.built_here(manifest)
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
            self._check_app(candidate, component, kind, source_build=built_here)
            if kind == "menubar":
                from .sourcestamp import fingerprint
                packages = candidate / "Contents/Resources/packages"
                identity = json.loads((packages / "release-identity.json").read_text())
                if (identity.get("sha") != manifest["sources"]["stack"] or
                        identity.get("release") != manifest["release"] or
                        identity.get("package_sha256") != fingerprint(packages / "jstack_host")):
                    raise releases.ReleaseError("signed app source identity differs from release")
                self._check_ownership(candidate, app)
            target = Path(self.config[kind + "_path"])
            if not os.access(target.parent, os.W_OK):
                raise releases.ReleaseError("app destination is not writable")
            backup = target.with_name(target.name + ".previous-" + stage.name)
            if backup.exists():
                raise releases.ReleaseError("a bundle already stands where this swap displaces the running one")
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
            if current == "not_found":
                # SMAppService answers not_found for a plist this bundle has
                # never registered, exactly as for one it does not carry. For
                # a role the bundle seals that is the cutover — the capability
                # was stopped under its old label and the role has yet to be
                # registered under its new one — so register, and let a plist
                # that truly is not there fail the register call below. For a
                # catalogued capability it means the bundle dropped it, and a
                # register call must not be guessed at.
                from .app_services import sealed_roles
                if role not in sealed_roles(app):
                    raise releases.ReleaseError(f"{role} service could not be restored")
            result = control(app, "register", role)
            if result["status"] not in {"enabled", "requires_approval"}:
                raise releases.ReleaseError(f"{role} service could not be restored")

    def apply(self, job: dict):
        from . import update_plugins
        transaction = job["transaction"]
        app = Path(self.config["menubar_path"])
        if self._statuses(app) != transaction["services"]:
            raise releases.ReleaseError("service approvals changed since staging")
        update_plugins.install(transaction["providers"], Path(transaction["stack"]),
                               transaction["manifest"].get("sources", {}).get("stack"))
        self._stop_services(app, transaction["services"])
        built_here = self._built_here(job)
        for kind, record in transaction["apps"].items():
            target, backup = Path(record["target"]), Path(record["backup"])
            if kind == "client":
                stop_app(target)
            # A copy an earlier job left behind is never resumed, and refusing
            # the release over it stranded the machine until someone deleted it
            # by hand (#117). Clear the ground on entry.
            sweep(target)
            incoming = target.with_name(target.name + ".incoming-" + job["id"])
            command(["/usr/bin/ditto", record["source"], str(incoming)])
            self._check_app(incoming, transaction["manifest"]["components"][kind], kind,
                            source_build=built_here)
            if target.exists():
                os.replace(target, backup)
            os.replace(incoming, target)
        self._restore_services(app, transaction["services"])
        # A role this bundle seals that the previous one did not is absent from
        # the snapshot, so `_restore_services` cannot bring it up — and for the
        # scheduler the machine is also still running the LaunchAgent that
        # preceded it, on the same port. Adopting is what makes an update
        # finish the move instead of leaving both copies installed.
        client = transaction["apps"].get("client")
        if client and client["was_running"]:
            command(["/usr/bin/open", "-a", client["target"]])
        # Last, and allowed to raise. A half-finished cutover is two jobs racing
        # one port or none serving it, which is not an update anyone should read
        # as done — but the window the user is looking at comes back first, so a
        # scheduler fault costs them a diagnosis and not their app.
        from .install_signed import adopt_scheduler
        adopt_scheduler(app)

    def settle(self, job: dict) -> dict:
        """Close a failed transaction: nothing restored, nothing retained.

        The bundle that is at the target when a job fails is the one this
        machine runs — this updater does not put another one there. What is
        owed is the services this transaction stopped, started again against
        that bundle, and an /Applications with none of its copies left in it.
        """
        transaction = job.get("transaction", {})
        app = Path(self.config["menubar_path"])
        problems = []
        for record in transaction.get("apps", {}).values():
            sweep(Path(record["target"]))
        try:
            current = self._statuses(app)
            desired = dict(transaction.get("services", current))
            for role, status in current.items():
                # A denial recorded after the snapshot wins over the snapshot.
                if status == "requires_approval":
                    desired[role] = status
            self._restore_services(app, desired)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            problems.append(f"app services did not start: {exc}")
        client = transaction.get("apps", {}).get("client")
        if client and client["was_running"] and Path(client["target"]).exists():
            try:
                command(["/usr/bin/open", "-a", client["target"]])
            except (releases.ReleaseError, OSError, subprocess.SubprocessError) as exc:
                problems.append(f"the client did not reopen: {exc}")
        return {"error": "; ".join(problems)}
