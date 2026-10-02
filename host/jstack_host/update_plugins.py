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
    dirty = command([*git, "status", "--porcelain", "--untracked-files=no"]).split("\n")
    dirty = [line[3:] for line in dirty if line.strip()]
    if dirty:
        # Named, because the refusal outlives the edit: by the time anyone
        # reads the log the tree is clean again and nothing says who wrote.
        raise ReleaseError(f"the jStack checkout at {root} has uncommitted changes "
                           f"({', '.join(dirty[:5])}{' …' if len(dirty) > 5 else ''}); it was "
                           f"not moved to {sha[:8]} and the plugin stays where it is")
    if command([*git, "rev-parse", "HEAD"]).strip() == sha:
        return
    if subprocess.run([*git, "cat-file", "-e", sha + "^{commit}"], capture_output=True).returncode:
        command([*git, "fetch", "--force", "origin", sha], timeout=3600)
    on_branch = not subprocess.run([*git, "symbolic-ref", "--quiet", "HEAD"],
                                   capture_output=True).returncode
    merged = on_branch and not subprocess.run([*git, "merge", "--ff-only", "--quiet", sha],
                                              capture_output=True).returncode
    # A merge to a commit the branch already contains is "already up to date":
    # git exits 0 and moves nothing. Read as success that left the checkout on
    # the newer commit, serving the newer plugin, while the update installed
    # the older one — and the job then fails verification over a plugin version
    # that disagrees with the stack's, naming neither (run 20260928-112354:
    # acc-leaf1 staged onto 3c78eb62 with ~/jStack still at 79f82820 and the
    # plugin still 26.9.5). Where HEAD ended up is the answer, not the status.
    if not merged or command([*git, "rev-parse", "HEAD"]).strip() != sha:
        command([*git, "checkout", "--quiet", "--detach", sha])
    # The tree is written from the commit, not trusted to the move: a file
    # touched within the second its index entry was written reads as clean
    # to git and is skipped by the checkout. Safe, since nothing uncommitted
    # survived the check above; untracked files are not git's to remove.
    command([*git, "reset", "--quiet", "--hard", "HEAD"])


def local_checkout() -> Path | None:
    """The jStack checkout the installer makes on this Mac, if it is one.

    `$JSTACK_CHECKOUT`, default `~/jStack` — the same two answers install.sh
    gives. A directory counts only when it is a git tree AND carries this
    plugin: `.git` alone is any repo, the plugin alone is any copy.
    """
    root = Path(os.environ.get("JSTACK_CHECKOUT") or Path.home() / "jStack")
    if (root / ".git").exists() and (root / "plugins/jstack/.claude-plugin/plugin.json").exists():
        return root
    return None


def readopt(provider: dict, sha: str | None) -> Path | None:
    """The checkout a stage-registered provider goes back to, or None.

    Before 564f571 an update rewrote the registration from `~/jStack` to the
    shipped copy inside its stage, and every update since carried that forward:
    the copy is relocated stage to stage while the checkout beside it is never
    advanced and never read — dead weight that looks live to a developer, to
    `jstack-doctor` and to the nightly heal (#163). The doctor now fails that
    registration wherever a checkout exists; this is the updater's half. The
    checkout is moved to the commit this update installs, exactly as a
    registered checkout is, and the registration follows it home. What
    `advance` refuses — uncommitted work, most of all — refuses the
    re-adoption too: the stage keeps serving, the update still lands, and the
    reason is said rather than swallowed.
    """
    if provider.get("checkout") or not sha:
        return None
    checkout = local_checkout()
    if checkout is None:
        return None
    try:
        advance(str(checkout), sha)
    except ReleaseError as exc:
        import sys
        print(f"jstack update: the registration stays on its release stage — {exc}",
              file=sys.stderr, flush=True)
        return None
    return checkout


def install(providers: list[dict], stack: Path, sha: str | None = None):
    # Every checkout the engines read the plugin from, moved once, before any
    # engine is told to re-read it. A shipped copy has no commit to move to.
    for root in sorted({p["root"] for p in providers if p.get("checkout")}):
        if sha:
            advance(root, sha)
    for provider in providers:
        # A checkout stays where it is; the update moved its contents, not its
        # path. A shipped copy is relocated — back onto the checkout this Mac
        # has when it has one, else onto what was just staged.
        if not provider.get("checkout"):
            home = readopt(provider, sha)
            target = str(home) if home else str(stack)
            for field in ("config", "hooks", "marketplace"):
                if provider.get(field):
                    replace_references(Path(provider[field]), provider["root"], target)
            move_shell_references(provider["root"], target)
        binary = provider["binary"]
        # Where the engine is told to read the plugin from. Naming the stage
        # for a checkout would move the registration the branch above just
        # declined to move.
        source = provider["root"] if provider.get("checkout") else target
        if provider["kind"] == "claude":
            run([binary, "plugin", "update", "jstack@jStack", "--scope", "user"])
        else:
            register_codex_marketplace(binary, source)
            run([binary, "plugin", "add", "jstack@jstack", "--json"])
            # A release can change hooks/hooks.json, and nothing before this
            # ever re-ran the install-time write of the operator-owned config
            # (#141) — so a managed machine kept trusting hooks that no
            # longer matched what the plugin now ships. Best-effort: a
            # machine with no passwordless path to /etc/codex logs the same
            # message `codex_setup.py` would print and is left exactly where
            # it was; `jstack-doctor`'s `codex hooks` check already catches
            # that drift and names the manual re-run that clears it. Never
            # fatal to the update itself, on the same reasoning as the doctor
            # check reading this same manifest.
            from .codex_hooks import install_managed_config, silence_plugin_hooks
            plugin = Path(source) / "plugins/jstack"
            try:
                print(install_managed_config(plugin))
            except OSError as exc:
                print(f"codex hooks not refreshed: {exc}")
            # The `plugin add` above unpacked a fresh hooks/hooks.json into the
            # new version's cache directory, which Codex reads as a second
            # registration of every hook the operator file already carries. An
            # update is exactly when it comes back, so it is silenced here and
            # not only at install time.
            try:
                for manifest in silence_plugin_hooks(plugin):
                    print(f"left the plugin's own hook manifest only what the operator file lacks: {manifest}")
            except OSError as exc:
                print(f"codex plugin hooks not silenced: {exc}")


def registered_codex_root(binary: str) -> Path | None:
    """The directory codex holds for the `jstack` marketplace, resolved."""
    for line in run([binary, "plugin", "marketplace", "list"]).splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0] == "jstack":
            return Path(parts[1].strip()).resolve()
    return None


def register_codex_marketplace(binary: str, source: str) -> None:
    """`codex plugin marketplace add`, accepting a registration that already
    names this directory.

    codex-cli 0.157 canonicalizes the path it is given and compares that with
    the string it stored, so a registration written before the checkout moved
    behind a symlink (`~/jStack` -> jStack-Project/jStack-Code) is refused as
    "a different source" although both name the same tree — which stopped the
    hub's own 26.9.2 update (#220). The registration is left as it is: the
    engine reads the plugin through it fine, and only a registration that
    really names another directory is an error.
    """
    try:
        run([binary, "plugin", "marketplace", "add", source])
    except ReleaseError as exc:
        if "already added from a different source" not in str(exc):
            raise
        if registered_codex_root(binary) != Path(source).resolve():
            raise


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
