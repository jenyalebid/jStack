"""Installed signed-service control; never recreate interpreter LaunchAgents."""
from pathlib import Path
import plistlib
import json
import re

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


def specification(app: Path, configuration: dict, role: str) -> tuple[Path, str, str]:
    capability = configuration.get("host_capability") if role == "host" else None
    if capability is None:
        return app, role, "live.jstack.hub." + role
    if not isinstance(capability, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", capability):
        raise ValueError("invalid embedding host capability")
    services = Path(configuration["services_app"])
    if not services.is_absolute():
        raise ValueError("services owner must be an absolute bundle path")
    verify(services, "live.jstack.hub.services")
    manifest = json.loads((services / "Contents/Resources/automation-catalog.json").read_text())
    if capability not in manifest:
        raise ValueError("embedding host is not in the sealed capability catalog")
    return services, capability, "live.jstack.automation." + capability


def observe(app: Path, configuration: dict) -> dict:
    statuses = control(app, "status")
    owner, capability, _ = specification(app, configuration, "host")
    if owner != app:
        statuses["host"] = control(owner, "status")[capability]
    return statuses


def repair(configuration: dict, *, port: int, bind: str, state_dir: Path | None, out) -> int:
    app = Path(configuration["app"])
    verify(app)
    if (port != install_host.DEFAULT_PORT and port != configuration["port"] or
            bind != install_host.DEFAULT_BIND and bind != configuration.get("bind", install_host.DEFAULT_BIND) or
            state_dir is not None and str(state_dir.resolve()) != configuration["environment"].get("JREMOTE_STATE_DIR")):
        raise ValueError("repair cannot silently change the installed host identity or endpoint")
    statuses = observe(app, configuration)
    if statuses["host"] != "enabled":
        print(f"host is {statuses['host']}; repair did not enable it; use the explicit Start control", file=out)
        return 1
    served = install_host.wait_for_health(configuration["port"])
    healthy = (install_host.api_answers(configuration["port"]) if configuration.get("host_capability") else
               bool(served and served.get("service") == "jremote-host"))
    if not healthy:
        print("signed host is registered but its API has not become healthy", file=out)
        return 1
    print(f"signed host serving {configuration['port']}; identity and settings retained", file=out)
    return 0


def uninstall(configuration: dict, out) -> int:
    app = Path(configuration["app"])
    verify(app)
    statuses = observe(app, configuration)
    for role in ("menu", "host"):
        owner, service, label = specification(app, configuration, role)
        if statuses[role] in {"enabled", "requires_approval"}:
            control(owner, "unregister", service)
        if not install_host.wait_unloaded(label):
            raise ValueError(f"{role} is still registered with launchd")
    print("Hub host and menu unregistered; pairing, state and recovery services retained", file=out)
    return 0


def status(configuration: dict, *, port: int | None, out) -> int:
    app = Path(configuration["app"])
    verify(app)
    observed = observe(app, configuration)
    print(f"owner      {app}", file=out)
    for role in ("host", "menu"):
        print(f"{role:10} {observed[role]}", file=out)
    loaded = install_host.is_loaded(specification(app, configuration, "host")[2])
    print(f"loaded     {'yes' if loaded else 'no'}", file=out)
    probed = configuration["port"] if port is None else port
    served = install_host.health(probed)
    healthy = (install_host.api_answers(probed) if configuration.get("host_capability") else
               bool(served and served.get("service") == "jremote-host"))
    print(f"API        {'answering' if healthy else 'not observed'} on {probed}", file=out)
    return 0 if healthy and loaded else 1
