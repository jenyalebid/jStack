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
    for key in ("automation_settings", "migration_dir", "services_app"):
        if key in value and (not isinstance(value[key], str) or not Path(value[key]).is_absolute()):
            raise ValueError(f"{key} must be an absolute private data path")
    if "bind" in value and (not isinstance(value["bind"], str) or not value["bind"] or "\x00" in value["bind"]):
        raise ValueError("invalid service bind address")
    if "host_capability" in value:
        capability, services = value["host_capability"], value.get("services_app")
        if (not isinstance(capability, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", capability) or
                not isinstance(services, str) or not Path(services).is_absolute()):
            raise ValueError("invalid embedding service binding")
    return value


def automation_path(configuration: dict | None = None) -> Path:
    configuration = read() if configuration is None else configuration
    return Path(configuration.get("automation_settings", Path.home() / ".local/state/jremote/automation-settings.json"))
