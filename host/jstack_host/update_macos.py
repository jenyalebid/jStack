"""Mac installation backend for the shared updater.

Targets and service labels are pinned during local bootstrap. A release can
supply signed artifacts, not paths, launchd labels, or commands to execute.
The recovery journal records each original before anything is switched.
"""
from __future__ import annotations

import json
import os
import platform
import plistlib
import shutil
import shlex
import signal
import subprocess
import tarfile
import time
import zipfile
from pathlib import Path

import httpx
import psutil

from . import release_manifest as releases
from .update_supervisor import atomic_json


def command(argv: list[str], *, timeout=120, **kwargs) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, **kwargs)
    if result.returncode:
        raise releases.ReleaseError(f"{Path(argv[0]).name} failed: {(result.stdout + result.stderr)[-2000:]}")
    return result.stdout


def atomic_bytes(path: Path, value: bytes, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".update-tmp")
    with temp.open("wb") as stream:
        os.fchmod(stream.fileno(), mode if mode is not None else
                  (path.stat().st_mode & 0o777 if path.exists() else 0o600))
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def safe_tar(archive: Path, target: Path) -> None:
    with tarfile.open(archive) as bundle:
        members = bundle.getmembers()
        total = 0
        for member in members:
            path = (target / member.name).resolve()
            if not path.is_relative_to(target.resolve()) or not (member.isfile() or member.isdir()):
                raise releases.ReleaseError("unsafe stack archive member")
            total += member.size
            if total > 8 * 1024**3:
                raise releases.ReleaseError("expanded stack artifact exceeds limit")
        bundle.extractall(target, members=members, filter="data")


def unpack_app(archive: Path, target: Path) -> Path:
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            if not (target / member.filename).resolve().is_relative_to(target.resolve()):
                raise releases.ReleaseError("unsafe app archive path")
        if sum(m.file_size for m in bundle.infolist()) > 8 * 1024**3:
            raise releases.ReleaseError("expanded app artifact exceeds limit")
    command(["/usr/bin/ditto", "-x", "-k", str(archive), str(target)])
    apps = list(target.glob("*.app"))
    if len(apps) != 1:
        raise releases.ReleaseError("artifact must contain exactly one app")
    return apps[0]


def bundle_info(path: Path) -> dict:
    with (path / "Contents/Info.plist").open("rb") as stream:
        return plistlib.load(stream)


def client_distribution(path: Path, config: dict) -> str:
    """Only hub-distributed bundles and previously enrolled clients are ours."""
    if not path.exists():
        return "missing"
    if (path / "Contents/_MASReceipt/receipt").exists():
        return "app_store"
    info = bundle_info(path)
    if info.get("JStackDistribution") == "hub":
        return "hub"
    # Old bootstrap explicitly enrolled the installed client. Do not extend
    # that enrollment to a different bundle, or to any Store installation.
    if (config.get("client_managed", True) and config.get("client_bundle_id")
            and info.get("CFBundleIdentifier") == config["client_bundle_id"]):
        return "hub"
    return "external"


def running(path: Path) -> list[int]:
    executable = str(path / "Contents/MacOS" / bundle_info(path)["CFBundleExecutable"])
    result = []
    for pid in psutil.pids():
        try:
            # launchd first starts xpcproxy, then execs the bundle in that PID.
            # process_iter caches Process.exe forever, so polling its cached
            # objects can miss an app that has already launched successfully.
            if psutil.Process(pid).exe() == executable:
                result.append(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return result


def stop_app(path: Path) -> None:
    if not path.exists():
        return
    processes = [psutil.Process(pid) for pid in running(path)]
    for process in processes:
        try:
            process.send_signal(signal.SIGTERM)
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(processes, timeout=10)
    if alive:
        # Do not kill through an app refusing to quit: keep its existing
        # bundle and report the failed transaction for recovery.
        raise releases.ReleaseError("client did not quit; installation left recoverable")


class MacBackend:
    def __init__(self, root: Path, config: dict):
        self.root, self.config = root, config

    def _app_running(self, kind: str, path: Path) -> list[int]:
        return running(path)

    def _running_required(self, kind: str, record: dict, transaction: dict) -> bool:
        return kind == "menubar" or record["was_running"]

    def activate_runtime(self, job: dict) -> bool:
        """Move the next updater process only after the hub confirms this job."""
        if job.get("state") != "current" or not job.get("verified") or not self.config.get("dispatcher"):
            return False
        transaction = job.get("transaction", {})
        stack = Path(transaction["stack"])
        if not (stack / "host/jstack_host/update_dispatcher.py").is_file():
            return False  # compatibility with candidates predating the dispatcher
        imports = [str(stack / "host"), str(Path(transaction["stage"]) / "dependencies")]
        if Path(__file__).resolve().parent.parent == Path(imports[0]).resolve():
            return False
        configuration = json.loads((self.root / "config.json").read_text())
        configuration["runtime_imports"] = imports
        atomic_json(self.root / "config.json", configuration)
        return True

    def compatible(self, manifest: dict):
        compatibility = manifest["compatibility"]
        if platform.system() != "Darwin":
            raise releases.ReleaseError("this release requires macOS")
        if compatibility["architecture"] not in (platform.machine(), "universal"):
            raise releases.ReleaseError("release architecture does not match this Mac")
        version = lambda value: tuple(int(p) for p in value.split("."))
        if version(platform.mac_ver()[0]) < version(compatibility["minimum_os"]):
            raise releases.ReleaseError("release requires a newer macOS")

    def _check_app(self, path: Path, component: dict, kind: str):
        team = self.config["team_id"]
        identifier = self.config[kind + "_bundle_id"]
        if kind == "client" and not identifier:
            # A host bootstrapped without a client can later discover a signed
            # direct-distribution app. Pin to that installed bundle identity.
            identifier = bundle_info(Path(self.config["client_path"]))["CFBundleIdentifier"]
        command(["/usr/bin/codesign", "--verify", "--deep", "--strict", "-R",
                 f'=anchor apple generic and certificate leaf[subject.OU] = "{team}" and identifier "{identifier}"',
                 str(path)])
        command(["/usr/sbin/spctl", "--assess", "--type", "execute", str(path)])
        if str(bundle_info(path)["CFBundleVersion"]) != component["version"]:
            raise releases.ReleaseError(f"{kind} bundle version differs from release")

    def stage(self, manifest: dict, directory: Path) -> dict:
        # Unique staging attempts preserve a failed attempt for diagnostics;
        # they never unpack over the running release or an earlier stage.
        import tempfile
        stage = Path(tempfile.mkdtemp(prefix="stage-", dir=directory))
        stack = stage / "stack"
        stack.mkdir()
        safe_tar(directory / manifest["components"]["stack"]["file"], stack)
        identity = json.loads((stack / "host/release-identity.json").read_text())
        from .sourcestamp import fingerprint
        if (identity.get("release") != manifest["release"] or
                identity.get("sha") != manifest["sources"]["stack"] or
                identity.get("package_sha256") != fingerprint(stack / "host/jstack_host")):
            raise releases.ReleaseError("packaged host identity differs from release")
        # Compile imports and prepare dependencies without touching the live
        # interpreter. The host may be embedded in a larger Python app, so
        # its existing interpreter is retained; dependencies are staged under
        # this release and precede it only for the restarted host.
        python = self.config["python"]
        dependencies = stage / "dependencies"
        command([python, "-m", "pip", "install", "--disable-pip-version-check",
                 "--target", str(dependencies), str(stack / "host")], timeout=900)
        # Import from the signed unpacked package, not pip's copied version:
        # the identity file and source tree must be inseparable at startup.
        pythonpath = str(stack / "host") + os.pathsep + str(dependencies)
        command([python, "-c", "import jstack_host.server; import jstack_host.update_routes"],
                env={**os.environ, "PYTHONPATH": pythonpath})
        apps = {}
        for kind in ("menubar", "client"):
            if kind == "client" and client_distribution(Path(self.config["client_path"]), self.config) != "hub":
                continue
            folder = stage / kind
            folder.mkdir()
            component = manifest["components"][kind]
            app = unpack_app(directory / component["file"], folder)
            self._check_app(app, component, kind)
            target = Path(self.config[kind + "_path"])
            # Check permission before stopping a single process. A Mac whose
            # application directory is not user-writable needs provisioning,
            # not a password prompt during a remote update.
            if not os.access(target.parent, os.W_OK):
                raise releases.ReleaseError(f"{kind} install directory is not writable")
            apps[kind] = {"source": str(app), "target": str(target),
                          "backup": str(target.with_name(target.name + ".previous-" + manifest["release"] + "-" + stage.name)),
                          "was_running": bool(target.exists() and running(target)),
                          "existed": target.exists()}
            if Path(apps[kind]["backup"]).exists():
                raise releases.ReleaseError("prior recovery bundle must be retained; choose a new release ID")
        plists = {}
        for kind in ("host", "menubar"):
            target = Path(self.config[kind + "_plist"])
            data = target.read_bytes()
            job = plistlib.loads(data)
            if job["Label"] != self.config[kind + "_label"]:
                raise releases.ReleaseError("service identity changed since updater bootstrap")
            updated = dict(job)
            if kind == "host":
                from .install_host import upgraded_environment
                environment = upgraded_environment(job.get("EnvironmentVariables", {}))
                environment["PYTHONPATH"] = pythonpath + (
                    os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else "")
                updated["EnvironmentVariables"] = environment
                # Standalone starts in its package directory; an embedding
                # application's own working directory must stay unchanged.
                if "jstack_host.server" in job.get("ProgramArguments", []):
                    updated["WorkingDirectory"] = str(stack / "host")
            backup = stage / (kind + "-original.plist")
            backup.write_bytes(data)
            new = stage / (kind + "-updated.plist")
            new.write_bytes(plistlib.dumps(updated))
            plists[kind] = {"target": str(target), "original": str(backup), "updated": str(new)}
        launcher = None
        if self.config.get("launcher_path"):
            target = Path(self.config["launcher_path"])
            launcher = {"target": str(target), "symlink": os.readlink(target) if target.is_symlink() else None,
                        "original": str(stage / "launcher-original"), "updated": str(stage / "launcher-updated"),
                        "mode": target.stat().st_mode & 0o777}
            Path(launcher["original"]).write_bytes(target.read_bytes())
            script = ("#!/bin/sh\nexport PYTHONPATH=" + shlex.quote(pythonpath) +
                      '\nexec ' + shlex.quote(python) + ' -m jstack_host.cli "$@"\n')
            Path(launcher["updated"]).write_text(script)
        return {"stage": str(stage), "stack": str(stack), "apps": apps, "plists": plists,
                "launcher": launcher,
                "release": manifest["release"], "manifest": manifest,
                "providers": self._prepare_plugins()}

    def _prepare_plugins(self):
        from . import update_plugins
        return update_plugins.prepare()

    def _unload(self, kind: str):
        label = self.config[kind + "_label"]
        print(f"update: unloading {kind}", flush=True)
        # bootout returns before launchd necessarily removes the service.
        # Wait for that removal before trying to bootstrap its replacement.
        subprocess.run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
                       capture_output=True, timeout=30)
        deadline = time.monotonic() + 30
        while subprocess.run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
                             capture_output=True, timeout=5).returncode == 0:
            if time.monotonic() >= deadline:
                raise releases.ReleaseError(f"{kind} service did not unload")
            time.sleep(0.1)

    def _load(self, kind: str):
        print(f"update: loading {kind}", flush=True)
        try:
            command(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", self.config[kind + "_plist"]])
        except releases.ReleaseError as exc:
            raise releases.ReleaseError(f"{kind} service did not load: {exc}") from exc

    def apply(self, job: dict):
        transaction = job["transaction"]
        from . import update_plugins
        update_plugins.install(transaction["providers"], Path(transaction["stack"]))
        self._unload("menubar")
        for kind, app in transaction["apps"].items():
            print(f"update: replacing {kind}", flush=True)
            target, backup = Path(app["target"]), Path(app["backup"])
            stop_app(target)
            if target.exists():
                os.replace(target, backup)
            # ditto stages on the destination volume, so the final rename is
            # atomic even when /Applications and state live on different disks.
            incoming = target.with_name(target.name + ".incoming-" + transaction["release"])
            command(["/usr/bin/ditto", app["source"], str(incoming)])
            self._check_app(incoming, transaction["manifest"]["components"][kind], kind)
            os.replace(incoming, target)
        self._unload("host")
        for plist in transaction["plists"].values():
            atomic_bytes(Path(plist["target"]), Path(plist["updated"]).read_bytes())
        launcher = transaction.get("launcher")
        if launcher:
            atomic_bytes(Path(launcher["target"]), Path(launcher["updated"]).read_bytes(), mode=0o755)
        self._load("host")
        self._load("menubar")
        client = transaction["apps"].get("client")
        if client and client["was_running"]:
            command(["/usr/bin/open", "-a", client["target"]])

    def rollback(self, job: dict):
        transaction = job["transaction"]
        from . import update_plugins
        plugin_error = None
        try:
            update_plugins.rollback(transaction["providers"], Path(transaction["stack"]))
        except Exception as exc:
            # Recover the host/apps even if a provider CLI is unavailable.
            # Keep the transaction non-terminal until all recovery succeeds.
            plugin_error = exc
        self._unload("menubar")
        self._unload("host")
        for kind, app in transaction["apps"].items():
            target, backup = Path(app["target"]), Path(app["backup"])
            if backup.exists():
                stop_app(target)
                if target.exists():
                    failed = target.with_name(target.name + ".failed-" + job["id"])
                    if failed.exists():
                        raise releases.ReleaseError("recovery destination already exists")
                    os.replace(target, failed)
                os.replace(backup, target)
            elif not app["existed"] and target.exists():
                stop_app(target)
                os.replace(target, target.with_name(target.name + ".failed-" + job["id"]))
        for plist in transaction["plists"].values():
            atomic_bytes(Path(plist["target"]), Path(plist["original"]).read_bytes())
        launcher = transaction.get("launcher")
        if launcher:
            target = Path(launcher["target"])
            if launcher["symlink"]:
                import uuid
                temporary = target.with_name(target.name + ".restore-" + uuid.uuid4().hex)
                temporary.symlink_to(launcher["symlink"])
                os.replace(temporary, target)
            else:
                atomic_bytes(target, Path(launcher["original"]).read_bytes(), mode=launcher["mode"])
        self._load("host")
        self._load("menubar")
        client = transaction["apps"].get("client")
        if client and client["was_running"]:
            command(["/usr/bin/open", "-a", client["target"]])
        if plugin_error:
            raise releases.ReleaseError(f"host/apps restored; plugin recovery pending: {plugin_error}") from plugin_error

    def finalize(self, job: dict):
        """Discard temporary app copies after the hub confirms success."""
        for app in job["transaction"]["apps"].values():
            backup = Path(app["backup"])
            if not backup.exists():
                continue
            target = Path(app["target"])
            if (backup.parent != target.parent or
                    not backup.name.startswith(target.name + ".previous-")):
                raise releases.ReleaseError("refusing to remove an unexpected recovery bundle")
            shutil.rmtree(backup)

    def verify(self, job: dict) -> bool:
        try:
            from . import update_plugins
            plugins = update_plugins.observed(update_plugins.discover())
            expected = job["envelope"]["manifest"]["components"]["stack"]["version"]
            if any(value["version"] != expected for value in plugins.values()):
                return False
            token = Path(self.config["token_path"]).read_text().strip()
            with httpx.Client(timeout=5, trust_env=False) as client:
                base = self.config["local_url"] + "/api/jremote/v1"
                headers = {"Authorization": "Bearer " + token}
                response = client.get(base + "/host", headers=headers)
                response.raise_for_status()
                identity = response.json()
                source = identity.get("source", {})
                if (identity["host_id"] != self.config["machine"] or
                        source.get("release") != job["release"] or source.get("dirty") or
                        source.get("sha") != job["envelope"]["manifest"]["sources"]["stack"]):
                    return False
                response = client.get(base + "/sessions/active", headers=headers)
                response.raise_for_status()
                if not isinstance(response.json().get("sessions"), list):
                    return False
            for kind, app in job["transaction"]["apps"].items():
                target = Path(app["target"])
                self._check_app(target, job["envelope"]["manifest"]["components"][kind], kind)
                if self._running_required(kind, app, job["transaction"]) and not self._app_running(kind, target):
                    return False
            return True
        except (OSError, ValueError, httpx.HTTPError, KeyError, subprocess.SubprocessError):
            return False

    def observe(self, job: dict) -> dict:
        from . import sourcestamp
        components = {}
        for kind in ("client", "menubar"):
            try:
                target = Path(self.config[kind + "_path"])
                info = bundle_info(target)
                components[kind] = {"installed": str(info["CFBundleVersion"]),
                                    "version": info.get("CFBundleShortVersionString"),
                                    "running_pids": self._app_running(kind, target)}
                if kind == "client":
                    components[kind]["distribution"] = client_distribution(target, self.config)
            except (OSError, ValueError, KeyError):
                components[kind] = {"installed": None, "running_pids": []}
        source = {}
        try:
            token = Path(self.config["token_path"]).read_text().strip()
            with httpx.Client(timeout=3, trust_env=False) as client:
                response = client.get(self.config["local_url"] + "/api/jremote/v1/host",
                                      headers={"Authorization": "Bearer " + token})
                response.raise_for_status()
                source = response.json().get("source", {})
        except (httpx.HTTPError, OSError, ValueError):
            pass
        expected = job.get("envelope", {}).get("manifest", {}).get("components", {})
        verified = (bool(job.get("verified")) and job.get("state") in {"current", "verifying"}
                    and source.get("release") == job.get("release") and not source.get("dirty")
                    and all(components[kind]["installed"] == expected.get(kind, {}).get("version")
                            for kind in job.get("transaction", {}).get("apps", {"client": {}, "menubar": {}})))
        try:
            from . import update_plugins
            components["plugins"] = update_plugins.observed(update_plugins.discover())
        except (OSError, ValueError, subprocess.SubprocessError):
            components["plugins"] = {"error": "could not observe installed plugin versions"}
        verified = verified and all(isinstance(value, dict) and value.get("version") ==
                                    expected.get("stack", {}).get("version")
                                    for value in components["plugins"].values())
        if self._running_required("menubar", {}, job.get("transaction", {})):
            verified = verified and bool(components["menubar"]["running_pids"])
        return {"components": components, "host_source": source,
                "updater_source": sourcestamp.capture(),
                "release": source.get("release"),
                "verified": verified, "job": {k: job.get(k) for k in ("id", "state", "detail")}}
