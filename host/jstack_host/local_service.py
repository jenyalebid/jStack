"""Run one child process below the signed owner: a catalogued user capability,
or the scheduler daemon the Hub owns as a sealed role of its own."""
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from . import service_catalog, service_settings


def run(resources: Path, slug: str) -> int:
    if os.geteuid() == 0:
        raise PermissionError("local capabilities must never run privileged")
    manifest = json.loads((resources / "automation-catalog.json").read_text())
    approved = manifest[slug]
    installation = service_settings.read()
    configuration = service_settings.automation_path(installation)
    job = json.loads(configuration.read_text())[slug]
    service_catalog.validate(slug, job)
    if service_catalog.digest(job) != approved["job_sha256"]:
        raise ValueError("capability definition differs from the signed catalog")
    # Do not export the installing agent session, app bootstrap variables, or
    # Python import roots to unrelated local capabilities.
    environment = {key: os.environ[key] for key in ("HOME", "USER", "LOGNAME", "TMPDIR") if key in os.environ}
    environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    environment.update(job.get("EnvironmentVariables", {}))
    environment["XPC_SERVICE_NAME"] = "live.jstack.automation." + slug
    if installation.get("host_capability") == slug:
        # Preserve an embedding application's own interpreter, dependencies
        # and profile, but load the Hub API from the checked signed release.
        # This path is stable across updates; no sealed job definition changes.
        from .app_services import verify
        app = Path(installation["app"])
        verify(app)
        packages = app / "Contents/Resources/packages"
        environment["PYTHONPATH"] = str(packages) + (os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else "")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PATH"] = str(app / "Contents/MacOS") + os.pathsep + environment["PATH"]
    default = Path.home() / ".local/state/jremote/logs" / f"{slug}.log"
    return supervise(job["ProgramArguments"], executable=job.get("Program"),
                     cwd=job.get("WorkingDirectory", str(Path.home())), env=environment,
                     out_path=Path(job.get("StandardOutPath") or default),
                     err_path=Path(job.get("StandardErrorPath") or default))


def supervise(arguments: list, *, executable: str | None, cwd: str, env: dict,
              out_path: Path, err_path: Path) -> int:
    """Hold one child for launchd, and finish its process group when stopped."""
    streams = []
    handlers = {}
    try:
        for target in (out_path, err_path):
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            streams.append(os.fdopen(descriptor, "ab", buffering=0))
        process = subprocess.Popen(arguments, executable=executable, cwd=cwd, env=env,
                                   stdout=streams[0], stderr=streams[1])

        stopping = False

        def forward(signum, _frame):
            nonlocal stopping
            if stopping:
                return
            stopping = True
            if process.poll() is None:
                process.send_signal(signum)
            deadline = time.monotonic() + 5
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            # launchd signals the runtime, not every descendant. A child such
            # as uvicorn may accept SIGTERM and then wait forever on an SSE
            # connection; launchd eventually kills only this supervisor and
            # leaves that child reparented to pid 1. The runtime is the group
            # leader in a sealed service, so finish the whole group. Refuse to
            # kill an interactive caller if this function is invoked directly.
            if os.getpgrp() == os.getpid():
                os.killpg(os.getpgrp(), signal.SIGKILL)
            if process.poll() is None:
                process.kill()
                process.wait()
            raise SystemExit(128 + signum)

        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, forward)
        result = process.wait()
        return result if result >= 0 else 128 - result
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        for stream in streams:
            stream.close()


def scheduler_command(python: str, plugin_root: Path) -> list:
    """The daemon's argv, with its import roots on the command line.

    Not `-m scheduler` with PYTHONPATH: the Hub's interpreter is an isolated
    build and ignores PYTHONPATH, so that form finds the plugin under no
    sealed service. `vendor/` beside the plugin carries python-dateutil for an
    interpreter that has no site-packages of its own. Kept identical to
    `plugins/jstack/bin/jstack-scheduler:launch_argv`, which is what runs on a
    machine with no Hub; `host/tests/test_hub_scheduler.py` reads both.
    """
    paths = [str(plugin_root)]
    vendor = plugin_root / "vendor"
    if vendor.is_dir():
        paths.append(str(vendor))
    return [python, "-c", f"import sys, runpy; sys.path[:0] = {paths!r}; "
                          "runpy.run_module('scheduler', run_name='__main__')"]


def run_scheduler(logs: Path) -> int:
    """The scheduler daemon as a sealed Hub role.

    None of the plugin is imported: it is unsealed code in a checkout the user
    edits, and this process is the one the Hub's signature covers. The daemon
    is a child, spawned by absolute path out of the machine-local settings —
    the same separation `run()` keeps for a catalogued capability.

    The working directory is the home folder rather than the root the daemon
    serves. launchd opens the log files and chdirs BEFORE the process exists,
    so a root under a TCC-protected folder (~/Desktop, ~/Documents) denies the
    spawn with nothing to prompt; the root reaches the daemon through
    JSTACK_ROOT instead, where a refusal can be written to a log and read.
    """
    if os.geteuid() == 0:
        raise PermissionError("the scheduler daemon must never run privileged")
    declared = service_settings.scheduler(service_settings.read())
    if not declared:
        raise SystemExit("this installation declares no scheduler daemon")
    python, plugin_root = declared["python"], Path(declared["plugin_root"])
    if not Path(python).is_file() or not (plugin_root / "scheduler").is_dir():
        raise SystemExit("the declared scheduler interpreter or plugin root is not on this disk")
    environment = {key: os.environ[key] for key in ("HOME", "USER", "LOGNAME", "TMPDIR") if key in os.environ}
    environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    environment.update(declared.get("environment", {}))
    environment["XPC_SERVICE_NAME"] = "live.jstack.hub.scheduler"
    target = logs / "scheduler.log"
    return supervise(scheduler_command(python, plugin_root), executable=None,
                     cwd=str(Path.home()), env=environment,
                     out_path=target, err_path=target)
