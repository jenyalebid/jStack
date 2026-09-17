"""Machine-local settings, separate from the signed executable bundle."""
import json
from pathlib import Path


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
    return value
