"""Run one explicitly catalogued user capability below the signed owner."""
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
    streams = []
    handlers = {}
    try:
        for field in ("StandardOutPath", "StandardErrorPath"):
            target = Path(job[field]) if job.get(field) else Path.home() / ".local/state/jremote/logs" / f"{slug}.log"
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            streams.append(os.fdopen(descriptor, "ab", buffering=0))
        process = subprocess.Popen(job["ProgramArguments"], executable=job.get("Program"),
            cwd=job.get("WorkingDirectory", str(Path.home())), env=environment,
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
