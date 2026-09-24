"""Move future agent sessions to a release, retaining old plugin caches.

Provider CLIs own cache installation. Only jStack source references move;
other plugins, auth, models, permissions and active sessions are untouched.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from .release_manifest import ReleaseError


def tool_path() -> str:
    """Where a provider CLI and its interpreter are looked for.

    The updater runs as a catalogued capability under the signed owner, whose
    environment is a bare `/usr/bin:/bin:/usr/sbin:/sbin` — no Homebrew, no
    `~/.local/bin`. A CLI installed the ordinary way (`npm -g`, the native
    installer) lives exactly there, and a Node CLI's `#!/usr/bin/env node`
    needs the same directories again to find its interpreter. Resolving and
    running providers on the host's spawn path answers both; anything the
    service did inherit stays behind it.
    """
    from . import hostenv
    return hostenv.spawn_path(inherit=os.environ.get("PATH"))


def run(argv: list[str]) -> str:
    env = dict(os.environ, PATH=tool_path())
    result = subprocess.run(argv, capture_output=True, text=True, timeout=120, env=env)
    if result.returncode:
        raise ReleaseError(f"plugin installation failed: {result.stderr[-1000:]}")
    return result.stdout


def serves_a_checkout(root: str) -> bool:
    """Is this marketplace served from a git checkout rather than a shipped copy?

    A machine that DEVELOPS jStack registers its marketplace against the
    checkout, and everything downstream reads that registration: `plugin
    update`, the nightly currency heal, `jstack-doctor`. Moving it to a
    release stage pins all of them to one commit forever — the update re-reads
    a directory that cannot change and correctly reports nothing to do, so the
    drift is both permanent and invisible. A leaf has no checkout to protect
    and moves exactly as before. `.git` is a file in a worktree, so test for
    existence rather than for a directory.
    """
    return (Path(root) / ".git").exists()


def pinned(kind: str, root: str) -> None:
    """Say what was left alone. A release that silently declines half its work
    is indistinguishable from one that did it."""
    print(f"update: leaving the {kind} jStack marketplace at {root} — it serves a "
          f"checkout, which no release stage can stand in for", flush=True)


def discover() -> list[dict]:
    import tomllib
    home = Path.home()
    result = []
    claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude"))
    marketplace_file = claude_dir / "plugins/known_marketplaces.json"
    if marketplace_file.exists():
        entry = json.loads(marketplace_file.read_text()).get("jStack")
        if entry:
            if entry.get("source", {}).get("source") != "directory":
                raise ReleaseError("convert the jStack marketplace to a local source before managed updates")
            root = entry["source"]["path"]
            binary = shutil.which("claude", path=tool_path()) or str(home / ".local/bin/claude")
            if serves_a_checkout(root):
                pinned("claude", root)
            else:
                result.append({"kind": "claude", "binary": binary, "root": root,
                               "config": str(claude_dir / "settings.json"), "marketplace": str(marketplace_file),
                               "ledger": str(claude_dir / "plugins/installed_plugins.json")})
    codex_dir = Path(os.environ.get("CODEX_HOME", home / ".codex"))
    native_config = codex_dir / "config.toml"
    if native_config.exists():
        entry = tomllib.loads(native_config.read_text()).get("marketplaces", {}).get("jstack")
        if entry:
            if entry.get("source_type") != "local":
                raise ReleaseError("convert the native jStack marketplace to a local source before managed updates")
            root = entry["source"]
            binary = shutil.which("codex", path=tool_path()) or str(home / ".local/bin/codex")
            if serves_a_checkout(root):
                pinned("codex", root)
            else:
                result.append({"kind": "codex", "binary": binary, "root": root,
                               "config": str(native_config), "hooks": str(codex_dir / "hooks.json")})
    return result


def replace_references(path: Path, old: str, new: str):
    if not path.exists():
        return
    from .update_macos import atomic_bytes
    text = path.read_text()
    # Both source-root values and paths into it, in JSON/TOML strings. No
    # provider-wide rewriting, and no removal of the original cached version.
    changed = text.replace(old + "/", new + "/").replace('"' + old + '"', '"' + new + '"')
    if changed != text:
        atomic_bytes(path, changed.encode())


def install(providers: list[dict], stack: Path):
    for provider in providers:
        for field in ("config", "hooks", "marketplace"):
            if provider.get(field):
                replace_references(Path(provider[field]), provider["root"], str(stack))
        move_shell_references(provider["root"], str(stack))
        binary = provider["binary"]
        if provider["kind"] == "claude":
            run([binary, "plugin", "update", "jstack@jStack", "--scope", "user"])
        else:
            run([binary, "plugin", "marketplace", "add", str(stack)])
            run([binary, "plugin", "add", "jstack@jstack", "--json"])


def observed(providers: list[dict]) -> dict:
    result = {}
    for provider in providers:
        kind = provider["kind"]
        if kind == "claude":
            rows = json.loads(Path(provider["ledger"]).read_text()).get("plugins", {}).get("jstack@jStack", [])
            row = next((r for r in rows if r.get("scope") == "user"), {})
            result[kind] = {"version": row.get("version"), "path": row.get("installPath")}
        else:
            data = json.loads(run([provider["binary"], "plugin", "list", "--marketplace", "jstack", "--json"]))
            rows = data if isinstance(data, list) else data.get("installed", [])
            row = next((r for r in rows if r.get("name") == "jstack"), {})
            result[kind] = {"version": row.get("version"), "path": row.get("source", {}).get("path")}
    return result


def prepare() -> list[dict]:
    result = discover()
    for provider in result:
        if not Path(provider["binary"]).is_file():
            raise ReleaseError(f"{provider['kind']} CLI is missing: nothing at "
                               f"{provider['binary']} and none on {tool_path()}")
    return result


def move_shell_references(old: str, new: str):
    """Only jStack's paths move; existing shells keep their loaded environment."""
    home = Path.home()
    for file in (home / ".zshrc", home / ".bash_profile", home / ".profile"):
        replace_references(file, old, new)
    for directory in (home / ".claude/rules", home / ".claude/commands"):
        if not directory.exists():
            continue
        for link in directory.iterdir():
            if not link.is_symlink():
                continue
            target = str(link.resolve())
            if not target.startswith(old + "/"):
                continue
            import uuid
            replacement = link.with_name(link.name + ".update-link-" + uuid.uuid4().hex)
            replacement.symlink_to(new + target[len(old):])
            os.replace(replacement, link)
