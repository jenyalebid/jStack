"""Installed signed-service control; never recreate interpreter LaunchAgents."""
from pathlib import Path
import plistlib

from . import install_host
from .update_app import control
from .update_macos import command


def bundled() -> bool:
    for parent in Path(__file__).resolve().parents:
        if parent.suffix == ".app":
            info = plistlib.loads((parent / "Contents/Info.plist").read_bytes())
            return info.get("CFBundleIdentifier") in {"live.jstack.hub", "live.jstack.hub.services"}
    return False


def verify(app: Path, identifier="live.jstack.hub"):
    requirement = f'=anchor apple generic and certificate leaf[subject.OU] = "MZ95H77RQQ" and identifier "{identifier}"'
    command(["/usr/bin/codesign", "--verify", "--deep", "--strict", "-R", requirement, str(app)])


def repair(configuration: dict, *, port: int, bind: str, state_dir: Path | None, out) -> int:
    app = Path(configuration["app"])
    verify(app)
    if (port != install_host.DEFAULT_PORT and port != configuration["port"] or
            bind != install_host.DEFAULT_BIND and bind != configuration.get("bind", install_host.DEFAULT_BIND) or
            state_dir is not None and str(state_dir.resolve()) != configuration["environment"].get("JREMOTE_STATE_DIR")):
        raise ValueError("repair cannot silently change the installed host identity or endpoint")
    statuses = control(app, "status")
    if statuses["host"] != "enabled":
        print(f"host is {statuses['host']}; repair did not enable it; use the explicit Start control", file=out)
        return 1
    served = install_host.wait_for_health(configuration["port"])
    if not served or served.get("service") != "jremote-host":
        print("signed host is registered but its API has not become healthy", file=out)
        return 1
    print(f"signed host serving {configuration['port']}; identity and settings retained", file=out)
    return 0


def uninstall(configuration: dict, out) -> int:
    app = Path(configuration["app"])
    verify(app)
    statuses = control(app, "status")
    for role in ("menu", "host"):
        if statuses[role] in {"enabled", "requires_approval"}:
            control(app, "unregister", role)
        if not install_host.wait_unloaded("live.jstack.hub." + role):
            raise ValueError(f"{role} is still registered with launchd")
    print("Hub host and menu unregistered; pairing, state and recovery services retained", file=out)
    return 0


def status(configuration: dict, *, port: int | None, out) -> int:
    app = Path(configuration["app"])
    verify(app)
    observed = control(app, "status")
    print(f"owner      {app}", file=out)
    for role in ("host", "menu"):
        print(f"{role:10} {observed[role]}", file=out)
    loaded = install_host.is_loaded("live.jstack.hub.host")
    print(f"loaded     {'yes' if loaded else 'no'}", file=out)
    probed = configuration["port"] if port is None else port
    served = install_host.health(probed)
    healthy = bool(served and served.get("service") == "jremote-host")
    print(f"API        {'answering' if healthy else 'not observed'} on {probed}", file=out)
    return 0 if healthy and loaded else 1
