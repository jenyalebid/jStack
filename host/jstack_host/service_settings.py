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
            not isinstance(value.get("port"), int) or not 1 <= value["port"] <= 65535 or
            not Path(value.get("app", "")).is_absolute()):
        raise ValueError("invalid signed service installation settings; refusing to guess another host")
    return value
