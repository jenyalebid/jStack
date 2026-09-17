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
    value = json.loads(target.read_text())
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
    if "host_capability" in value:
        capability, services = value["host_capability"], value.get("services_app")
        if (not isinstance(capability, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", capability) or
                not isinstance(services, str) or not Path(services).is_absolute()):
            raise ValueError("invalid embedding service binding")
    return value
