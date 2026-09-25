"""Machine-local settings, separate from the signed executable bundle."""
import json
from pathlib import Path
import re


def path() -> Path:
    return Path.home() / ".local/state/jremote/service-settings.json"


def read() -> dict:
    target = path()
    if not target.exists():
        return {}
    return validate(json.loads(target.read_text()))


def validate(value: dict) -> dict:
    if (not isinstance(value, dict) or value.get("schema") != 1 or
            not isinstance(value.get("environment"), dict) or
            type(value.get("port")) is not int or not 1 <= value["port"] <= 65535 or
            not isinstance(value.get("app"), str) or
            not Path(value.get("app", "")).is_absolute()):
        raise ValueError("invalid signed service installation settings; refusing to guess another host")
    if any(not isinstance(key, str) or not key or "=" in key or "\x00" in key or
           not isinstance(item, str) or "\x00" in item for key, item in value["environment"].items()):
        raise ValueError("invalid service environment")
    state = value["environment"].get("JREMOTE_STATE_DIR")
    if state is not None and not Path(state).is_absolute():
        raise ValueError("service state directory must be absolute")
    for key in ("automation_settings", "migration_dir"):
        if key in value and (not isinstance(value[key], str) or not Path(value[key]).is_absolute()):
            raise ValueError(f"{key} must be an absolute private data path")
    if "scheduler" in value:
        declared = value["scheduler"]
        if not isinstance(declared, dict) or set(declared) - {"python", "plugin_root", "environment"}:
            raise ValueError("the scheduler service declares an interpreter, a plugin root and its environment")
        for key in ("python", "plugin_root"):
            if not isinstance(declared.get(key), str) or not Path(declared[key]).is_absolute():
                raise ValueError(f"scheduler {key} must be an absolute path")
        environment = declared.get("environment", {})
        # The sealed service spawns this daemon with a clean environment, so
        # whatever was resolving the tree for the installing shell is frozen
        # here or lost. Scoped to the families the daemon reads plus PATH: this
        # block reaches a child process by name, and a sealed service must not
        # become a way to set DYLD_* or PYTHONPATH on one.
        if not isinstance(environment, dict) or any(
                not isinstance(key, str) or not isinstance(item, str) or "\x00" in item or
                not (key.startswith(("SCHEDULER_", "JSTACK_")) or key == "PATH")
                for key, item in environment.items()):
            raise ValueError("the scheduler environment carries only PATH and SCHEDULER_/JSTACK_ settings")
    if "network_transaction" in value and (not isinstance(value["network_transaction"], str)
            or not re.fullmatch(r"[a-f0-9]{32}", value["network_transaction"])):
        raise ValueError("network_transaction must identify a reviewed transaction")
    if "bind" in value and (not isinstance(value["bind"], str) or not value["bind"] or "\x00" in value["bind"]):
        raise ValueError("invalid service bind address")
    if "host_capability" in value:
        capability = value["host_capability"]
        if not isinstance(capability, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", capability):
            raise ValueError("invalid embedding service binding")
    return value


def scheduler(configuration: dict | None = None) -> dict:
    """What this machine declared its scheduler daemon to be, or nothing.

    Nothing is the honest answer for `--no-scheduler`: the sealed plist ships
    in every Hub, and this block is what decides whether the role is ever
    registered. A default here would install a daemon the operator declined.
    """
    configuration = read() if configuration is None else configuration
    return configuration.get("scheduler") or {}


def automation_path(configuration: dict | None = None) -> Path:
    configuration = read() if configuration is None else configuration
    return Path(configuration.get("automation_settings", Path.home() / ".local/state/jremote/automation-settings.json"))
