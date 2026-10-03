"""Where this instance may place work: itself, plus every machine jStack's own
access already lets it ssh to, minus its parent.

The managed ssh block is the access model: a hub carries its leaves, a leaf
carries only the leaves its shell grants name. A leaf never places work on its
parent, so the parent is dropped even if a block ever lists it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def ssh_config() -> Path:
    return Path(os.environ.get("JSTACK_SANDBOX_SSH_CONFIG", "~/.ssh/config")).expanduser()


def peers() -> list[dict]:
    from ..shell_access import read_config_block
    found, current = [], None
    for line in read_config_block(ssh_config()):
        key, _, value = line.strip().partition(" ")
        if key == "Host":
            current = {"name": value.strip(), "address": ""}
            found.append(current)
        elif key == "HostName" and current is not None:
            current["address"] = value.strip()
    return found


def parent() -> dict:
    override = os.environ.get("JSTACK_SANDBOX_PARENT")
    if override is not None:
        return json.loads(override) if override else {}
    from .. import hostenv
    from ..attach_parent import PARENT_RECORD
    try:
        return json.loads((hostenv.state_dir() / PARENT_RECORD).read_text())
    except (OSError, ValueError):
        return {}


def candidates(self_name: str) -> list[dict]:
    """[{name, ssh}] in no particular order; ssh is None for this machine."""
    up = parent()
    banned = {up.get("parent_name"), up.get("parent_address")} - {None, ""}
    out = [{"name": self_name, "ssh": None}]
    for peer in peers():
        if peer["name"] in banned or peer["address"] in banned:
            continue
        out.append({"name": peer["name"], "ssh": peer["name"]})
    return out
