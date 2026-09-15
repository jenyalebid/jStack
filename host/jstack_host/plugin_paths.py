"""Where an installed Claude Code plugin's files actually live.

Two surfaces answer "what can this machine invoke" — jRemote's slash palette
(`commands.py`) and the context inventory's skills list
(`dashboard/shared/context_inventory.py`) — and both were reading the
versioned copy under `~/.claude/plugins/cache/`. For a
plugin whose marketplace is a `directory` source that copy is a snapshot, not
the truth: the install location *is* the working tree, so an edit to
`~/jStack` is invocable in the very next session with no reinstall, while the
cache stays frozen at whatever the last install wrote.

The cost of reading the snapshot was a pair of screens quietly behind the
machine — on 2026-09-02 the palette and the skills list still showed the
pre-trim descriptions and had never heard of `/jstack:pict` or `/jstack:task`,
hours after both landed. Nothing errored; the lists simply described an older
Mac.

Only `directory` sources are redirected. A github marketplace keeps a git clone
under `plugins/marketplaces/` that runs ahead of the installed version, so its
cache copy is the honest one — redirecting there would show skills the session
cannot invoke, which is the same lie in the other direction.

In `jremote` rather than `shared` because a standalone host has the plugin
directories and not the dashboard tree (tests/test_jremote_standalone.py), and
because one rule about where a plugin lives is worth exactly one copy — the
inventory reaches in the way `shared/helpers.py` already reaches for
`jremote.transcripts`.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

#: Resolved per call, never at import: the tests that move `Path.home()` are
#: the ones asking "what would this machine answer", and a constant bound at
#: import answers for the machine running the test instead.
def plugin_cache() -> Path:
    return Path.home() / ".claude" / "plugins" / "cache"


def known_marketplaces() -> Path:
    return Path.home() / ".claude" / "plugins" / "known_marketplaces.json"


def live_marketplace_roots() -> dict[str, Path]:
    """`marketplace name → its live checkout`, for `directory` sources only."""
    try:
        data = json.loads(known_marketplaces().read_text())
    except (OSError, ValueError):
        return {}
    roots: dict[str, Path] = {}
    for market, mp in (data or {}).items():
        if not isinstance(mp, dict):
            continue
        src = mp.get("source") or {}
        if src.get("source") != "directory":
            continue
        loc = mp.get("installLocation") or src.get("path")
        if loc:
            roots[market] = Path(loc)
    return roots


def live_root(plugin_id: str, roots: dict[str, Path] | None = None) -> Path | None:
    """The plugin's own directory inside its live marketplace, or None.

    `plugin_id` is the `{plugin}@{marketplace}` key both `installed_plugins`
    and `settings.enabledPlugins` are written in. The path within the
    marketplace comes from that marketplace's own manifest rather than a guess
    at the layout.
    """
    plugin, _, market = plugin_id.partition("@")
    base = (roots if roots is not None else live_marketplace_roots()).get(market)
    if not base:
        return None
    try:
        manifest = json.loads((base / ".claude-plugin" / "marketplace.json").read_text())
    except (OSError, ValueError):
        return None
    for entry in manifest.get("plugins", []):
        if not isinstance(entry, dict) or entry.get("name") != plugin:
            continue
        src = entry.get("source")
        if not isinstance(src, str):    # a git source is not a path on this disk
            return None
        root = base / src
        return root if root.is_dir() else None
    return None


def newest_cached(plugin_id: str, cache: Path | None = None) -> Path | None:
    """The highest-versioned copy under `plugins/cache/`, or None.

    The fallback for anything not live: version dirs sort numerically, so
    `0.10.0` beats `0.9.0` — a lexical sort would pick the older one.
    """
    plugin, _, market = plugin_id.partition("@")
    cache_root = cache if cache is not None else plugin_cache()
    try:
        # Preserve marketplace spelling on case-insensitive Macs as well:
        # the legacy jStack catalog and current jstack catalog are distinct.
        root = next((p / plugin for p in cache_root.iterdir()
                     if p.name == market), None)
    except OSError:
        return None
    if root is None:
        return None
    if not root.is_dir():
        return None
    versions = sorted((p for p in root.iterdir() if p.is_dir()),
                      key=lambda p: [int(x) for x in re.findall(r"\d+", p.name)])
    return versions[-1] if versions else None


def plugin_root(plugin_id: str, fallback: Path | None = None,
                roots: dict[str, Path] | None = None) -> Path | None:
    """Where to read this plugin from — live checkout first, then the copy the
    caller already knows about, then the newest cached version."""
    live = live_root(plugin_id, roots)
    if live is not None:
        return live
    if fallback is not None and fallback.is_dir():
        return fallback
    return newest_cached(plugin_id)


#: The plugin this repo shells out to, and where it sits on the machine that
#: maintains it — a checkout the marketplace registers as a `directory` source.
JSTACK_ID = "jstack@jstack"


def jstack_dev() -> Path:
    return Path.home() / "jStack" / "plugins" / "jstack"


def jstack_root() -> Path:
    """The jstack plugin's directory on THIS machine.

    Live checkout, then the dev tree if one exists, then the newest installed
    version. The dev path is the answer only where the marketplace is a
    `directory` source; install the plugin from its github marketplace and
    there is no `~/jStack` at all — the working copy is the versioned one under
    `plugins/cache/`, the same copy Claude Code itself runs.

    Never probed here. A caller that must degrade (a machine with no jStack)
    tests the result itself and keeps its own answer for absence — "this host
    has no renderer" and "the render failed" are different answers.
    """
    native_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    return (plugin_root(JSTACK_ID, fallback=jstack_dev())
            or plugin_root("jstack@jStack")
            or newest_cached(JSTACK_ID, native_home / "plugins/cache")
            or jstack_dev())


def jstack_bin(name: str) -> Path:
    """`bin/<name>` — the one place this repo spells a jstack adapter's path.

    Nine call sites across the host, the dashboard, the assistant and the test
    suite each spelled `~/jStack/plugins/jstack/bin/<name>` in full, and every
    one of them was a machine assumption wearing a path. On 2026-09-04 that
    assumption answered `/pict` from the app with 501 "pict isn't installed on
    this host" and killed splitoff with a FileNotFoundError, on a Mac where
    both adapters sat one directory over.
    """
    return jstack_root() / "bin" / name
