"""Entry point inside the sealed app. Never import from cwd or PYTHONPATH."""
import json
import os
from pathlib import Path
import sys

resources = Path(__file__).resolve().parent
sys.path.insert(0, str(resources / "packages"))


def main():
    cli = Path(sys.argv[0]).name == "JStackCLI"
    if not cli and len(sys.argv) < 2:
        raise SystemExit("expected host, updater, cli or self-test")
    role, arguments = ("cli", sys.argv[1:]) if cli else (sys.argv[1], sys.argv[2:])
    # This runs before any host module binds state paths at import time.
    from jstack_host import service_settings
    config = service_settings.read() if role != "self-test" else {}
    if config:
        for key, value in config.get("environment", {}).items():
            if not (key.startswith("JREMOTE_") or key in ("WG_PEER_DIR", "WG_ENDPOINT", "PATH")):
                raise ValueError("unsupported service environment key")
            os.environ[key] = str(value)
        if role == "host":
            arguments = ["--port", str(config["port"]), "--host", config.get("bind", "0.0.0.0"), *arguments]
    os.environ["PATH"] = str(resources.parent / "MacOS") + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    if role in ("host", "updater"):
        state = Path(os.environ.get("JREMOTE_STATE_DIR", str(Path.home() / ".local/state/jremote")))
        logs = state / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(logs / f"{role}.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.dup2(descriptor, 1)
        os.dup2(descriptor, 2)
        os.close(descriptor)
    if role == "self-test":
        import ssl
        import sqlite3
        import fastapi
        import cryptography.hazmat.bindings._rust
        import psutil
        import jstack_host.server
        print(json.dumps({"isolated": bool(sys.flags.isolated), "python": sys.version.split()[0],
                          "prefix": sys.prefix, "package": jstack_host.server.__file__,
                          "ssl": ssl.OPENSSL_VERSION, "sqlite": sqlite3.sqlite_version,
                          "unicode": "jStack — ready", "stdio": sys.stdout.encoding}, ensure_ascii=False))
        return
    sys.argv = [sys.argv[0], *arguments]
    if role == "host":
        from jstack_host.server import main as run
    elif role == "updater":
        state = os.environ.get("JREMOTE_STATE_DIR")
        if not state:
            raise SystemExit("updater requires an explicitly configured state directory")
        sys.argv[1:1] = ["--state-dir", state]
        from jstack_host.update_supervisor import main as run
    elif role == "cli":
        from jstack_host.cli import main as run
    else:
        raise SystemExit("unsupported runtime role")
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
