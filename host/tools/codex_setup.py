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

from jstack_host.codex_hooks import (  # noqa: E402
    MANAGED_CONFIG, install_managed_config, silence_plugin_hooks)


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
    A seat is a directory carrying AGENTS.md; include its ancestor chain so an
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
        if "AGENTS.md" not in files and "CLAUDE.md" not in files:
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


# The walk-up is AGENTS.md, which Codex reads natively from the project root
# down to cwd — the same order Claude walks — so no filename needs pointing.
# What it does need is room: the chain is ~10KB at a chat seat and a product
# seat adds the app's own doc on top. A half-loaded identity is worse than
# none, because nothing reports it.
DOC_SETTINGS = {
    "project_doc_max_bytes": "262144",
}

#: What installs before the AGENTS.md rename wrote inside our block, pointing
#: Codex at the CLAUDE.md walk-up. Dropped on sight; a user's own is not ours.
_RETIRED = re.compile(r'^project_doc_fallback_filenames = \["CLAUDE\.md"\]\n', re.M)


def doc_config(text):
    """Size Codex's instruction budget for the walk-up, above the first table.

    These are bare keys: TOML only reads them before the first table header, so
    this block goes at the top of the file and not, like the shell policy, at
    the end. A value the user has already chosen is left alone — including our
    own from a previous install, which is what makes this idempotent.
    """
    begin, end = "# BEGIN jstack docs\n", "# END jstack docs\n"
    if begin in text and end in text:
        start, stop = text.index(begin), text.index(end)
        text = text[:start] + _RETIRED.sub("", text[start:stop]) + text[stop:]
    if any(re.search(r"^\s*" + key + r"\s*=", text, flags=re.M) for key in DOC_SETTINGS):
        return text
    block = begin
    block += "".join(f"{key} = {value}\n" for key, value in DOC_SETTINGS.items())
    block += end
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


def tool_timeout_config(text, server, seconds):
    """Give one MCP server's table a `tool_timeout_sec`, replacing ours if present.

    Codex cuts every MCP call off at this timeout, and its default is far
    shorter than a job: job_monitor's `wait` blocks for as long as a job runs,
    so without this line the session gets a tool error instead of a result and
    falls back to ending the turn to be woken — the expensive path `wait`
    exists to remove. A server table that is not there is left alone.
    """
    header = f"[mcp_servers.{server}]"
    if header not in text:
        return text
    start = text.index(header) + len(header)
    match = re.search(r"^\[", text[start:], flags=re.M)
    end = start + match.start() if match else len(text)
    body = re.sub(r"^tool_timeout_sec\s*=.*\n?", "", text[start:end], flags=re.M)
    body = "\n" + f"tool_timeout_sec = {seconds}\n" + body.lstrip("\n")
    return text[:start] + body + text[end:]


def direct_only_config(text, namespace):
    """Add `namespace` to `[features.code_mode] direct_only_tool_namespaces`.

    Codex 0.160 runs MCP tools inside a code cell that yields every few seconds,
    and each yield is a model call that pays the whole context to ask whether
    the cell is done. A `wait` on a job is the one call that should cost
    nothing while it blocks, so its namespace is called directly. A config
    whose `features.code_mode` is not a table, or that this edit would make
    unparseable, is left alone.
    """
    import tomllib
    try:
        mode = tomllib.loads(text).get("features", {}).get("code_mode", {})
    except tomllib.TOMLDecodeError:
        return text
    if not isinstance(mode, dict):
        return text
    names = list(mode.get("direct_only_tool_namespaces") or [])
    if namespace in names:
        return text
    line = f"direct_only_tool_namespaces = {json.dumps(names + [namespace])}\n"
    header = "[features.code_mode]"
    if header in text:
        start = text.index(header) + len(header)
        match = re.search(r"^\[", text[start:], flags=re.M)
        end = start + match.start() if match else len(text)
        body = re.sub(r"^direct_only_tool_namespaces\s*=.*\n?", "", text[start:end], flags=re.M)
        out = text[:start] + "\n" + line + body.lstrip("\n") + text[end:]
    else:
        out = (text.rstrip("\n") + "\n\n" if text.strip() else "") + f"{header}\n{line}"
    try:
        tomllib.loads(out)
    except tomllib.TOMLDecodeError:
        return text
    return out


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
    # First, and not last: the hooks are the layer everything else on this
    # machine leans on, and a marketplace that will not answer must not take
    # them down with it.
    if not args.no_managed_hooks:
        print(install_managed_config(plugin))
    subprocess.run(["codex", "plugin", "marketplace", "add", str(checkout)], check=True)
    subprocess.run(["codex", "plugin", "add", "jstack@jstack"], check=True)
    # `plugin add` just unpacked a second copy of hooks/hooks.json, and Codex
    # reads it as a hook source beside the operator file written above — two
    # registrations of every hook, both of which run. That is what made one
    # `/takeover` open two sessions. The plugin stays installed for its skills
    # and commands; only its hook manifest goes quiet, and only while the
    # operator file is live and current.
    silenced = silence_plugin_hooks(plugin, codex)
    if silenced:
        copies = "copy" if len(silenced) == 1 else "copies"
        print(f"plugin hook manifest silenced in {len(silenced)} installed "
              f"{copies} — {MANAGED_CONFIG} is the one registration")
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
        sys.path.insert(0, str(checkout / "host/tools"))
        from job_monitor import WAIT_TOOL_TIMEOUT
        text = tool_timeout_config(config.read_text(), "job_monitor", WAIT_TOOL_TIMEOUT)
        config.write_text(direct_only_config(text, "mcp__job_monitor"))
    if any(name.startswith("swift-lsp@") and enabled
           for name, enabled in claude_settings.get("enabledPlugins", {}).items()):
        exists = subprocess.run(["codex", "mcp", "get", "swift-lsp", "--json"],
                                capture_output=True).returncode == 0
        if not exists:
            subprocess.run(["codex", "mcp", "add", "swift-lsp", "--", sys.executable,
                            str(checkout / "host/tools/swift_lsp_mcp.py")], check=True)


if __name__ == "__main__":
    main()
