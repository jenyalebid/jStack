"""One durable switch for every jStack-owned service and permission identity."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import uuid

from . import app_services, network_admin, service_settings
from .update_supervisor import atomic_json

BUNDLE_IDS = {"live.jstack.hub", "live.jstack.hub.services", "live.jstack.network",
              "com.jremote.menubar"}
NETWORK_APP = Path("/Library/PrivilegedHelperTools/jStack Network.app")


def path(configuration: dict | None = None) -> Path:
    configuration = service_settings.read() if configuration is None else configuration
    state = Path(configuration["environment"]["JREMOTE_STATE_DIR"])
    return state / "emergency-stop.json"


def active(configuration: dict | None = None) -> bool:
    try:
        value = json.loads(path(configuration).read_text())
        return value.get("schema") == 1 and value.get("active") is True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _run(arguments: list[str], *, accepted=(0,)) -> subprocess.CompletedProcess:
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=300)
    if result.returncode not in accepted:
        raise ValueError(f"emergency stop command failed: {Path(arguments[0]).name}: {result.stderr[-500:]}")
    return result


def _network(configuration: dict, journal: dict) -> None:
    installed = NETWORK_APP
    if not installed.exists():
        journal["network"] = "absent"
        return
    transaction = configuration.get("network_transaction")
    if (not isinstance(transaction, str) or len(transaction) != 32
            or any(character not in "0123456789abcdef" for character in transaction)):
        raise ValueError("installed Network owner lacks its reviewed transaction identity")
    result = network_admin.approve(installed, {"schema": 1, "action": "uninstall",
                                        "transaction": transaction}, path(configuration).parent / "network-requests")
    if result != {"state": "uninstalled", "transaction": transaction}:
        raise ValueError("Network uninstall did not reach its terminal state")
    journal["network"] = "uninstalled"


def _tmux(configuration: dict, journal: dict) -> None:
    executable = Path(configuration["app"]) / "Contents/MacOS/tmux"
    if not executable.is_file():
        raise ValueError("signed Hub does not contain its managed-session controller")
    socket = configuration["environment"].get("JREMOTE_TMUX_SOCK", "jremote")
    if not isinstance(socket, str) or not socket or "/" in socket or "\x00" in socket:
        raise ValueError("invalid product tmux socket")
    _run([str(executable), "-L", socket, "kill-server"], accepted=(0, 1))
    observed = _run([str(executable), "-L", socket, "list-sessions"], accepted=(0, 1))
    if observed.returncode == 0:
        raise ValueError("product managed sessions remain after shutdown")
    journal["managed_sessions"] = "stopped"


def _permissions(configuration: dict, journal: dict) -> None:
    identifiers = set(BUNDLE_IDS)
    config = Path(configuration["environment"]["JREMOTE_STATE_DIR"]) / "updates/config.json"
    if config.exists():
        value = json.loads(config.read_text())
        identifier = value.get("client_bundle_id")
        if isinstance(identifier, str) and identifier:
            identifiers.add(identifier)
    for identifier in sorted(identifiers):
        _run(["/usr/bin/tccutil", "reset", "All", identifier])
    journal["permissions_reset"] = sorted(identifiers)


def stop(*, out) -> int:
    if os.geteuid() == 0:
        raise PermissionError("run the emergency stop as the login user")
    configuration = service_settings.read()
    if not configuration:
        raise ValueError("signed installation settings are absent")
    app_services.verify(Path(configuration["app"]), "live.jstack.hub")
    app_services.verify(Path(configuration["services_app"]), "live.jstack.hub.services")
    target = path(configuration)
    if target.exists():
        journal = json.loads(target.read_text())
        if journal.get("schema") != 1 or journal.get("active") is not True:
            raise ValueError("existing emergency-stop record requires review")
    else:
        journal = {"schema": 1, "active": True, "id": uuid.uuid4().hex}
    journal["state"] = "stopping"
    atomic_json(target, journal)
    _network(configuration, journal)
    atomic_json(target, journal)
    _tmux(configuration, journal)
    atomic_json(target, journal)
    app_services.uninstall_all(configuration, out)
    journal["user_services"] = "unregistered"
    atomic_json(target, journal)
    _permissions(configuration, journal)
    journal["state"] = "stopped"
    atomic_json(target, journal)
    print("jStack emergency stop complete; services are off and app permissions were reset", file=out)
    return 0
