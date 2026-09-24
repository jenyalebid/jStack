"""`~/.claude/settings.json` — the settings jStack has to write itself.

Claude Code loads `statusLine` and `hooks` from **user scope only**, so a
plugin cannot deliver them however complete the release is. Everything else
jStack ships rides the plugin or the host package and is live the moment a
machine updates; these two do not, and nothing wrote them. That is the whole
of jStack #133: `allowance.py`'s Claude reader shipped in every release, its
writer (`statusline.py`) sat in the same release unwired, and the bars drew
whatever the Claude CLI's `/usage` screen had last left in its cache — on a
fresh hub, nothing.

So this module is the seam where a shipped system gets **turned on** for a
machine, kept deliberately small and declarative: what the setting should be,
whether it already is, and the one edit that makes it so.

Two rules, because the file belongs to the user as much as to us:

1. **Never take a setting that is already someone else's.** A `statusLine`
   jStack did not write is left exactly as it is and reported. Our own entry —
   recognised by the script it points at, whatever checkout or release stage
   that currently resolves to — is refreshed in place.
2. **Never rewrite the file for nothing.** `install()` compares before and
   after and skips the write when they match, so running the installer twice
   is not an edit, and a settings file nobody touched keeps its mtime.

Managed updates keep the written path current without anything here running
again: `~/.claude/settings.json` is the Claude provider's `config` in
`update_plugins.discover()`, and `replace_references` rewrites paths into the
new stack root on apply.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import hostenv

SETTINGS = Path.home() / ".claude" / "settings.json"

#: The sampler's path inside the checkout. Doubles as the marker: a statusLine
#: command ending in this is ours, from whichever root it was installed.
SAMPLER = "host/jstack_host/statusline.py"


def checkout_root() -> Path:
    """The tree this running copy of the package belongs to.

    Asked of `hostenv`, not counted out in directory levels: the answer has to
    move when the package does, and a release stage moves it on every update.
    """
    return hostenv.package_root().parent


def read(path: Path | None = None) -> dict:
    try:
        return json.loads((path or SETTINGS).read_text())
    except (OSError, ValueError):
        return {}


def statusline_command(checkout: Path | None = None,
                       state_dir: str | None = None) -> str:
    """The command line to install.

    A state dir is carried as an env prefix only where it differs from what
    this machine resolves on its own. A hub with an embedded host reaches a
    different state root, and a sampler that recorded into the default would
    be faithful and useless — writing into a store nothing on that machine
    reads. Compared against `profile().state_dir()`, not `state_dir()`: the
    latter answers with the very override being tested, so it would call every
    machine's setting the default and drop the prefix on exactly the hosts
    that need it.
    """
    script = str((checkout or checkout_root()) / SAMPLER)
    state_dir = os.environ.get("JREMOTE_STATE_DIR", "") if state_dir is None else state_dir
    if state_dir and Path(state_dir) != hostenv.profile().state_dir():
        return f"JREMOTE_STATE_DIR={state_dir} {script}"
    return script


def is_ours(command) -> bool:
    return isinstance(command, str) and command.rstrip().endswith(SAMPLER)


def statusline_state(path: Path | None = None) -> tuple[bool, str]:
    """`(sampler is wired, what is there instead)` — the doctor's question.

    Answered off the file, never off a memory of having installed it: the
    installer runs once and the user edits this file for years.
    """
    current = (read(path) or {}).get("statusLine")
    if not isinstance(current, dict) or current.get("type") != "command":
        if current is None:
            return False, "no statusLine in ~/.claude/settings.json"
        return False, f"statusLine is {current!r}, not a command"
    command = current.get("command")
    if is_ours(command):
        return True, str(command)
    return False, f"statusLine runs something else: {command!r}"


def plan_statusline(settings: dict, command: str) -> tuple[dict, str]:
    """The settings to write, and one line saying what happened.

    Returns the mapping unchanged when nothing needs changing, so the caller
    can tell "already right" from "edited" without diffing twice.
    """
    current = settings.get("statusLine")
    if isinstance(current, dict) and current.get("type") == "command":
        if is_ours(current.get("command")):
            if current.get("command") == command:
                return settings, "status line already samples the allowance"
            settings["statusLine"] = {"type": "command", "command": command}
            return settings, "status line sampler repointed at this install"
        return settings, ("left your own status line alone — the Usage bars "
                          "get no Claude reading until something records one "
                          f"(see {SAMPLER})")
    if current is not None:
        return settings, f"left an unrecognised statusLine alone: {current!r}"
    settings["statusLine"] = {"type": "command", "command": command}
    return settings, "status line now samples the Claude allowance (prints nothing)"


def install(path: Path | None = None, checkout: Path | None = None,
            state_dir: str | None = None, dry_run: bool = False) -> str:
    """Bring the settings file to where it should be. Returns the note."""
    path = path or SETTINGS
    settings = read(path)
    before = json.dumps(settings, sort_keys=True)
    settings, note = plan_statusline(
        settings, statusline_command(checkout, state_dir))
    if dry_run or json.dumps(settings, sort_keys=True) == before:
        return note
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    os.replace(tmp, path)
    return note
