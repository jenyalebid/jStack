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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jstack_host.codex_hooks import install_managed_config  # noqa: E402


_SKIP_TREES = {"pad", "git", "scratch", "node_modules", "__pycache__", ".venv",
               "venv", "build", "DerivedData", ".build", "dist", ".git", ".agents",
               ".claude"}


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


def workspace_directories(workspace):
    """Directories whose local Claude commands/skills Codex must also see.

    The installer receives the Agents root, not one leaf seat.  Walking only
    the root's ancestors therefore misses every command scoped to a real seat.
    A seat is a directory carrying CLAUDE.md; include its ancestor chain so an
    agent-level .claude also reaches nested seats, while pruning pad/checkouts
    and machine trees by the same boundaries the seat resolver uses.
    """
    workspace = workspace.resolve()
    found = {workspace, *workspace.parents}
    if not workspace.is_dir():
        return sorted(found, key=lambda path: (len(path.parts), str(path)))
    for current, dirs, files in os.walk(workspace):
        dirs[:] = [name for name in dirs
                   if not name.startswith(".") and name not in _SKIP_TREES]
        if "CLAUDE.md" not in files:
            continue
        directory = Path(current)
        while directory == workspace or workspace in directory.parents:
            found.add(directory)
            if directory == workspace:
                break
            directory = directory.parent
    return sorted(found, key=lambda path: (len(path.parts), str(path)))


def share_workspace(workspace):
    for directory in workspace_directories(workspace):
        if (directory / ".claude/skills").is_dir():
            link_skills(directory / ".claude/skills", directory / ".agents/skills")
        link_commands(directory / ".claude/commands", directory / ".agents/skills")


# The walk-up, in Codex's words. Codex reads AGENTS.md and nothing else by
# default, so a machine this script installed handed a Codex session no org, no
# agent and no seat — the identity chain a Claude session gets for free. Codex
# concatenates every fallback-named doc from the project root down to cwd,
# closest wins, which is exactly our walk-up order, so one key buys the whole
# chain with no second copy of it to rot.
DOC_SETTINGS = {
    "project_doc_fallback_filenames": '["CLAUDE.md"]',
    # The chain is ~10KB at a chat seat and a product seat adds the app's own
    # doc on top. A half-loaded identity is worse than none, because nothing
    # reports it.
    "project_doc_max_bytes": "262144",
}


def doc_config(text):
    """Point Codex at the CLAUDE.md walk-up, above the first table.

    These are bare keys: TOML only reads them before the first table header, so
    this block goes at the top of the file and not, like the shell policy, at
    the end. A value the user has already chosen is left alone — including our
    own from a previous install, which is what makes this idempotent.
    """
    if any(re.search(r"^\s*" + key + r"\s*=", text, flags=re.M) for key in DOC_SETTINGS):
        return text
    block = "# BEGIN jstack docs\n"
    block += "".join(f"{key} = {value}\n" for key, value in DOC_SETTINGS.items())
    block += "# END jstack docs\n"
    table = re.search(r"^\[", text, flags=re.M)
    cut = table.start() if table else len(text)
    return text[:cut] + block + ("\n" if text[cut:cut + 1] not in ("", "\n") else "") + text[cut:]


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
    parser.add_argument("--no-managed-hooks", action="store_true",
                        help="Leave /etc/codex alone; hooks then need per-machine approval")
    parser.add_argument("--job-monitor", action="store_true",
                        help="Install the opt-in background shell job MCP server")
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
        share_workspace(args.workspace)
    # Config's shell policy also applies to noninteractive commands, which do
    # not source .zshrc. Only manage our own block; never replace another table.
    config = codex / "config.toml"
    text = config.read_text() if config.exists() else ""
    entries = [str(plugin / "bin")] + os.environ.get("PATH", os.defpath).split(os.pathsep)
    entries = [p for p in entries if "/.codex/tmp/" not in p and not p.endswith("/codex-path")]
    text = shell_config(text, plugin, os.pathsep.join(dict.fromkeys(entries)))
    config.write_text(doc_config(text))
    if not args.no_managed_hooks:
        print(install_managed_config(plugin))
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
    if args.job_monitor:
        exists = subprocess.run(["codex", "mcp", "get", "job_monitor", "--json"],
                                capture_output=True).returncode == 0
        if not exists:
            subprocess.run(["codex", "mcp", "add", "job_monitor", "--", sys.executable,
                            str(checkout / "host/tools/job_monitor.py"), "mcp"], check=True)
    if any(name.startswith("swift-lsp@") and enabled
           for name, enabled in claude_settings.get("enabledPlugins", {}).items()):
        exists = subprocess.run(["codex", "mcp", "get", "swift-lsp", "--json"],
                                capture_output=True).returncode == 0
        if not exists:
            subprocess.run(["codex", "mcp", "add", "swift-lsp", "--", sys.executable,
                            str(checkout / "host/tools/swift_lsp_mcp.py")], check=True)


if __name__ == "__main__":
    main()
