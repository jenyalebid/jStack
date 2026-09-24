"""Our hooks, in Codex's spelling, installed where root's word is trust enough.

Holds the rules so `jstack-doctor` can grade them; `host/tools/codex_setup.py`
is the CLI over it. The hook definitions themselves live in the plugin's
`hooks/hooks.json` and are not restated here — one definition, two engines.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

# Hooks, and the trust they need. Codex skips a hook it has not been told to
# trust, and it skips it in SILENCE — no warning, no error, exit 0. Every
# surface is gated the same way: the plugin's own manifest, `[[hooks.*]]` in
# config.toml, and the hooks.json beside it. So a machine this script installed
# carried all fifteen of our hooks and ran none of them: no walk-up, no seat
# timeline, no path rules, no stop guards, no session review. This Mac only
# ever worked because a human approved them here once, by hand.
#
# A trust entry is a hash Codex computes and persists after that approval, so
# it is not something an installer can write. What an installer CAN do is
# install the hooks as the machine's operator, which is what
# /etc/codex/managed_config.toml is for: hooks declared there are trusted
# because root put them there, and need no per-machine approval at all.
MANAGED_CONFIG = Path("/etc/codex/managed_config.toml")

# Codex's own event names. A name it does not know is not an error either — the
# hook simply never runs, which is the same silence again, so we translate only
# what it will actually honour and say out loud what we dropped.
CODEX_EVENTS = ("PreToolUse", "PostToolUse", "PreCompact", "PostCompact",
                "SessionStart", "SessionEnd", "UserPromptSubmit", "Stop",
                "SubagentStart", "SubagentStop", "PermissionRequest", "Interrupt")

# Where an event one engine has lands on the other. Not a convenience: a hook
# dropped here is a hook that never runs on Codex, silently, and both of these
# are halves of one mechanism — the waiting dot goes up when a session needs a
# person and comes down when it gets one. Ship the "up" without the "down" and
# the dot is a lie for the rest of the session's life.
#
#   Notification    -> PermissionRequest. Claude's is "I need you"; Codex asks
#                      the same question by asking for approval.
#   PermissionDenied -> Interrupt. Not the same words, the same fact: the wait
#                      ended without the session getting what it asked for, so
#                      whatever went up comes down.
#
# Named, not guessed at the call site: an alias is a claim about two engines'
# semantics, and it belongs where the event list it edits is written.
CODEX_ALIASES = {"Notification": "PermissionRequest", "PermissionDenied": "Interrupt"}

# Claude tool names with no counterpart on Codex. A matcher is matched against
# the tool name, so an alternative naming a tool Codex does not have widens a
# group to nothing — `Bash|ExitPlanMode|Agent` selected two tools and claimed
# three. Both of these are dialogs Claude's client owns: Codex asks its question
# through the permission request, and it ends plan mode by flipping
# `permission_mode` rather than by calling a tool.
#
# Only the alternatives are removed, never the group. A group whose matcher
# names nothing else is left registered on a tool Codex will never send, which
# costs nothing at runtime and reads in the file as though it were wired;
# removing it is blocked by `test_managed_config_carries_every_hook_the_plugin
# _declares`, which asserts that every declared hook translates.
CODEX_ABSENT_TOOLS = frozenset({"AskUserQuestion", "ExitPlanMode"})

# Per-hook fields Codex reads, in its spelling. PascalCase is load-bearing here
# exactly as the event names are.
HOOK_FIELDS = ("timeout", "statusMessage", "additionalContextLimit", "async")


def managed_hooks(manifest, plugin):
    """Translate the plugin's hook manifest into an operator-owned TOML block.

    One implementation, two engines: these are the same scripts Claude Code
    runs, named here so Codex can find them. The logic — which rules match a
    path, which timeline window a seat injects — exists once. A second copy
    would be a second truth, and the copy is the one that goes stale.
    """
    lines, dropped = [], []
    for event, groups in (manifest.get("hooks") or {}).items():
        event = CODEX_ALIASES.get(event, event)
        if event not in CODEX_EVENTS:
            dropped.append(event)
            continue
        for group in groups:
            matcher = group.get("matcher") or ""
            kept = [t for t in matcher.split("|") if t not in CODEX_ABSENT_TOOLS]
            if matcher and kept:
                matcher = "|".join(kept)
            lines.append(f"[[hooks.{event}]]")
            if matcher:
                lines.append("matcher = " + json.dumps(matcher))
            for handler in group.get("hooks") or []:
                if handler.get("type") != "command":
                    dropped.append(f"{event}:{handler.get('type')}")
                    continue
                lines.append(f"[[hooks.{event}.hooks]]")
                lines.append('type = "command"')
                command = handler["command"].replace("${CLAUDE_PLUGIN_ROOT}", str(plugin))
                lines.append("command = " + json.dumps(command))
                for field in HOOK_FIELDS:
                    if handler.get(field) is not None:
                        lines.append(f"{field} = " + json.dumps(handler[field]))
            lines.append("")
    return "\n".join(lines).strip() + "\n", dropped


def managed_config(plugin, manifest_path=None):
    """The whole file, header and all. Written by root, read by every session."""
    manifest = json.loads((manifest_path or (plugin / "hooks/hooks.json")).read_text())
    body, dropped = managed_hooks(manifest, plugin)
    header = ("# Managed by jStack. Rewritten on every install — edit the plugin's\n"
              "# hooks/hooks.json instead, which is the one definition both engines read.\n"
              "#\n"
              "# These hooks are trusted because root installed them. Codex skips an\n"
              "# untrusted hook in silence, so the alternative was fifteen hooks that\n"
              "# looked wired on every machine and fired on none.\n\n")
    return header + body, dropped


def install_managed_config(plugin, path=None, runner=None, manifest_path=None):
    """Put our hooks where root's word is trust enough, and say what happened.

    Not writable by us on a normal machine, which is the whole point of the
    location — so the write goes through sudo, once, during an install a person
    is already sitting in front of. An unchanged file is not rewritten, so a
    re-install asks for nothing.
    """
    path = MANAGED_CONFIG if path is None else path
    runner = subprocess.run if runner is None else runner
    text, dropped = managed_config(plugin, manifest_path)
    note = f" (skipped: {', '.join(dropped)})" if dropped else ""
    try:
        if path.read_text() == text:
            return f"hooks already active for every Codex session{note}"
    except OSError:
        pass
    staged = path.parent / f".{path.name}.jstack" if os.access(path.parent, os.W_OK) \
        else Path(tempfile.mkdtemp()) / path.name
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(text)
    if os.access(path.parent, os.W_OK) or os.geteuid() == 0:
        staged.replace(path)
    else:
        print("Codex hooks need one sudo write to /etc/codex — without it a "
              "session gets no timeline, no rules and no session review.")
        for command in (["sudo", "mkdir", "-p", str(path.parent)],
                        ["sudo", "cp", str(staged), str(path)],
                        ["sudo", "chmod", "644", str(path)]):
            if runner(command).returncode != 0:
                return f"hooks NOT installed — {' '.join(command)} failed{note}"
    return f"hooks active for every Codex session on this machine{note}"
