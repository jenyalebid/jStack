"""Sandbox settings: one JSON file per instance, every key an owner may edit.

Nothing about a particular machine lives in code. A host's mode, its caps, the
images it keeps warm and every timing are read from here, so an owner changes
how a machine is used by editing a value (or `jstack-host sandbox settings`),
never by editing the package. A missing file is the defaults, and the default
mode is `off`: a host takes nothing until its owner says otherwise.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

MODES = ("off", "local", "offload", "free")

DEFAULTS: dict = {
    "mode": "off",
    # The footprint root on this host; every tenant sits under <root>/tenants.
    "root": "~/.jstack-sandbox",
    # Guests this host runs at once. Virtualization.framework allows two.
    "max_guests": 2,
    "guest_cpu": 4,
    "guest_mem_gb": 8,
    # Seats one shared guest takes before another is booted.
    "seats_per_guest": 2,
    # {tenant: max guests}; a tenant absent here is capped by max_guests only.
    "tenant_caps": {},
    # {image: count} of booted, untouched guests kept on a free host.
    "warm": {},
    "grace_minutes": 15,
    "renew_seconds": 60,
    "expire_seconds": 300,
    "queue_poll_seconds": 10,
    "ticket_seconds": 60,
    "boot_seconds": 300,
    # How a peer's sandbox is driven over ssh: the words before `sandbox host`.
    "remote_command": "~/.local/bin/jstack-host",
    # Empty means: found on PATH, then the usual install places.
    "tart": "",
    # Images a purge leaves in place unless asked for everything. Reaps only
    # ever delete guests, never images.
    "keep_images": [],
}

_TART_PLACES = ("~/.local/bin/tart", "/opt/homebrew/bin/tart", "/usr/local/bin/tart")


def state_dir() -> Path:
    override = os.environ.get("JSTACK_SANDBOX_STATE")
    if override:
        return Path(override).expanduser()
    from .. import hostenv
    return hostenv.state_dir() / "sandbox"


def path() -> Path:
    return state_dir() / "settings.json"


def load() -> dict:
    merged = json.loads(json.dumps(DEFAULTS))
    try:
        merged.update(json.loads(path().read_text()))
    except (OSError, ValueError):
        pass
    return merged


def overrides() -> dict:
    try:
        return json.loads(path().read_text())
    except (OSError, ValueError):
        return {}


def set_value(key: str, value) -> dict:
    if key not in DEFAULTS:
        raise KeyError(f"no setting {key!r}; known: {', '.join(sorted(DEFAULTS))}")
    if key == "mode" and value not in MODES:
        raise ValueError(f"mode is one of {', '.join(MODES)}")
    kept = overrides()
    kept[key] = value
    path().parent.mkdir(parents=True, exist_ok=True)
    tmp = path().with_suffix(".tmp")
    tmp.write_text(json.dumps(kept, indent=2, sort_keys=True) + "\n")
    tmp.replace(path())
    return load()


def parse_value(raw: str):
    """A CLI value: JSON when it parses (numbers, lists, objects), else text."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def root(conf: dict | None = None) -> Path:
    return Path((conf or load())["root"]).expanduser()


def tart_bin(conf: dict | None = None) -> str:
    named = (conf or load()).get("tart") or ""
    if named:
        return str(Path(named).expanduser())
    found = shutil.which("tart")
    if found:
        return found
    for place in _TART_PLACES:
        if Path(place).expanduser().exists():
            return str(Path(place).expanduser())
    return "tart"
