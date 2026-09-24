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

    It decides one thing only: whether the update may rewrite the registration
    to somewhere else. A checkout moves itself — the update fetches the commit
    into it — so pointing it at a frozen stage would pin `plugin update`, the
    nightly currency heal and `jstack-doctor` to one commit forever, each of
    them reading its ground truth from the thing that is wrong. Observed on a
    hub 2026-09-17 → 2026-09-21: four nights of a green self-heal over a plugin
    stuck at 0.69.3 while the checkout reached 0.69.5.

    It does NOT decide whether the provider is updated. Every Mac installs from
    a commit now, so every registration is a checkout, and skipping them was
    skipping the whole job: `observed()` answered for no engine and `install()`
    refreshed no cache, on every machine there is. `.git` is a file in a
    worktree, so test for existence rather than for a directory.
    """
    return (Path(root) / ".git").exists()


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
            result.append({"kind": "claude", "binary": binary, "root": root,
                           "checkout": serves_a_checkout(root),
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
            result.append({"kind": "codex", "binary": binary, "root": root,
                           "checkout": serves_a_checkout(root),
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


def advance(root: str, sha: str) -> None:
    """Bring a checkout onto the commit this update installs.

    `plugin update` re-reads the plugin from the marketplace's directory, and
    a checkout that nothing moves serves the commit it was cloned at forever:
    the hub that built 0.79.0 verified itself against a plugin still read from
    the 0.78.0 clone `install.sh` made (verify-journeys, 2026-09-24), and on
    every commit before the bump the versions agreed while the files did not.
    The update moves the checkout the way it moves everything else — to the
    commit the manifest names. A branch that can fast-forward keeps its name;
    anything else is left detached on the commit, no branch touched. An
    uncommitted change is the one thing this refuses to run over: a Mac that
    develops jStack in its registered checkout is told, by name, rather than
    have its work moved under it.
    """
    from .update_macos import command
    git = ["git", "-C", root]
    if command([*git, "status", "--porcelain", "--untracked-files=no"]).strip():
        raise ReleaseError(f"the jStack checkout at {root} has uncommitted changes; it was "
                           f"not moved to {sha[:8]} and the plugin stays where it is")
    if command([*git, "rev-parse", "HEAD"]).strip() == sha:
        return
    if subprocess.run([*git, "cat-file", "-e", sha + "^{commit}"], capture_output=True).returncode:
        command([*git, "fetch", "--force", "origin", sha], timeout=3600)
    on_branch = not subprocess.run([*git, "symbolic-ref", "--quiet", "HEAD"],
                                   capture_output=True).returncode
    if not (on_branch and not subprocess.run([*git, "merge", "--ff-only", "--quiet", sha],
                                             capture_output=True).returncode):
        command([*git, "checkout", "--quiet", "--detach", sha])
    # The tree is written from the commit, not trusted to the move: a file
    # touched within the second its index entry was written reads as clean
    # to git and is skipped by the checkout. Safe, since nothing uncommitted
    # survived the check above; untracked files are not git's to remove.
    command([*git, "reset", "--quiet", "--hard", "HEAD"])


def install(providers: list[dict], stack: Path, sha: str | None = None):
    # Every checkout the engines read the plugin from, moved once, before any
    # engine is told to re-read it. A shipped copy has no commit to move to.
    for root in sorted({p["root"] for p in providers if p.get("checkout")}):
        if sha:
            advance(root, sha)
    for provider in providers:
        # A checkout stays where it is; the update moved its contents, not its
        # path. Only a shipped copy is relocated onto what was just staged.
        if not provider.get("checkout"):
            for field in ("config", "hooks", "marketplace"):
                if provider.get(field):
                    replace_references(Path(provider[field]), provider["root"], str(stack))
            move_shell_references(provider["root"], str(stack))
        binary = provider["binary"]
        # Where the engine is told to read the plugin from. Naming the stage
        # for a checkout would move the registration the branch above just
        # declined to move.
        source = provider["root"] if provider.get("checkout") else str(stack)
        if provider["kind"] == "claude":
            run([binary, "plugin", "update", "jstack@jStack", "--scope", "user"])
        else:
            run([binary, "plugin", "marketplace", "add", source])
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
