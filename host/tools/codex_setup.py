#!/usr/bin/env python3
"""Install jStack in Codex and share this user's existing skill directories.

No permission/model defaults are changed. Existing native skills/config win.
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path


def link_skills(source, target):
    target.mkdir(parents=True, exist_ok=True)
    for skill in sorted(source.glob("*/SKILL.md")):
        dest = target / skill.parent.name
        if not dest.exists() and not dest.is_symlink():
            dest.symlink_to(skill.parent.resolve(), target_is_directory=True)
            print(f"linked skill {dest.name}")


def link_commands(source, target):
    for command in sorted(source.glob("*.md")):
        if "commands-stage" in command.resolve().parts:
            continue  # already supplied by the native jStack plugin
        dest = target / command.stem / "SKILL.md"
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        desc = f"Use when the user invokes /{command.stem} or requests that workspace command."
        dest.write_text(f"---\nname: {command.stem}\ndescription: {json.dumps(desc)}\n---\n\n"
                        f"Read and follow the existing command at [{command.name}]({command.resolve()}).\n"
                        "Use the user's supplied arguments wherever it refers to `$ARGUMENTS`.\n")
        print(f"shared workspace command {command.stem}")


def shell_config(text, plugin, path):
    header = "[shell_environment_policy.set]"
    owned = "# BEGIN jstack shell" in text
    if header in text and not owned:
        return text  # an existing user-owned policy takes precedence
    # Codex's TOML editor can move our end comment beyond a newly added MCP
    # table. Remove marker lines independently, never the text between them.
    text = text.replace("# BEGIN jstack shell\n", "").replace("# END jstack shell\n", "")
    values = ("PATH = " + json.dumps(path) + "\n"
              + "CLAUDE_PLUGIN_ROOT = " + json.dumps(str(plugin)) + "\n"
              + "PLUGIN_ROOT = " + json.dumps(str(plugin)) + "\n")
    block = "# BEGIN jstack shell\n" + header + "\n"
    if header in text:
        start = text.index(header)
        match = re.search(r"^\[", text[start + len(header):], flags=re.M)
        end = start + len(header) + match.start() if match else len(text)
        existing = text[start + len(header):end].lstrip("\n")
        existing = re.sub(r"^(?:PATH|CLAUDE_PLUGIN_ROOT|PLUGIN_ROOT)\s*=.*\n?", "", existing, flags=re.M)
        block += existing.strip() + "\n" if existing.strip() else ""
        return text[:start] + block + values + "# END jstack shell\n\n" + text[end:]
    return text + "\n" + block + values + "# END jstack shell\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path)
    args = parser.parse_args()
    checkout = Path(__file__).resolve().parents[2]
    plugin = checkout / "plugins/jstack"
    home = Path.home()
    codex = Path(os.environ.get("CODEX_HOME", home / ".codex"))
    codex.mkdir(parents=True, exist_ok=True)
    subprocess.run(["codex", "plugin", "marketplace", "add", str(checkout)], check=True)
    subprocess.run(["codex", "plugin", "add", "jstack@jstack"], check=True)
    link_skills(home / ".claude/skills", home / ".agents/skills")
    link_commands(home / ".claude/commands", home / ".agents/skills")
    if args.workspace:
        cwd = args.workspace.resolve()
        for directory in [*reversed(cwd.parents), cwd]:
            if (directory / ".claude/skills").is_dir():
                link_skills(directory / ".claude/skills", directory / ".agents/skills")
            link_commands(directory / ".claude/commands", directory / ".agents/skills")
    # Config's shell policy also applies to noninteractive commands, which do
    # not source .zshrc. Only manage our own block; never replace another table.
    config = codex / "config.toml"
    text = config.read_text() if config.exists() else ""
    entries = [str(plugin / "bin")] + os.environ.get("PATH", os.defpath).split(os.pathsep)
    entries = [p for p in entries if "/.codex/tmp/" not in p and not p.endswith("/codex-path")]
    config.write_text(shell_config(text, plugin, os.pathsep.join(dict.fromkeys(entries))))
    # Preserve local post-write tooling (for example a workspace's Swift lint).
    settings = home / ".claude/settings.json"
    claude_settings = json.loads(settings.read_text()) if settings.exists() else {}
    local = claude_settings.get("hooks", {})
    hooks_file = codex / "hooks.json"
    hooks = json.loads(hooks_file.read_text()) if hooks_file.exists() else {"hooks": {}}
    post = hooks.setdefault("hooks", {}).setdefault("PostToolUse", [])
    for group in local.get("PostToolUse", []):
        if not any(re.search(group.get("matcher") or ".*", name) for name in ("Edit", "Write")):
            continue
        for handler in group.get("hooks", []):
            if handler.get("type") != "command":
                continue
            command = shlex.join(["python3", str(checkout / "host/tools/codex_write_hook.py"), handler["command"]])
            entry = {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": command,
                                                         "timeout": handler.get("timeout", 30)}]}
            if entry not in post:
                post.append(entry)
    hooks_file.write_text(json.dumps(hooks, indent=2) + "\n")
    if any(name.startswith("swift-lsp@") and enabled
           for name, enabled in claude_settings.get("enabledPlugins", {}).items()):
        exists = subprocess.run(["codex", "mcp", "get", "swift-lsp", "--json"],
                                capture_output=True).returncode == 0
        if not exists:
            subprocess.run(["codex", "mcp", "add", "swift-lsp", "--", sys.executable,
                            str(checkout / "host/tools/swift_lsp_mcp.py")], check=True)


if __name__ == "__main__":
    main()
