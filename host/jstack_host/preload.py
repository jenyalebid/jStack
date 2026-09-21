"""The updater's full working set, imported before it can replace its bundle.

After the Hub swaps its own bundle, the updater process's sys.path strings
resolve into the NEW bundle. Any first-time import after the swap would mix
new modules into the old process mid-transaction. Everything the supervisor
can touch during verify, observe, rollback and finalize must therefore be in
sys.modules before any byte of the bundle is replaced. The import-freeze test
holds this list honest: it runs those paths under a hook that fails on any
module not already loaded.
"""
from __future__ import annotations

MODULES = (
    # standard library reached lazily inside the update paths
    "argparse", "fcntl", "json", "os", "pathlib", "platform", "plistlib",
    "re", "shlex", "shutil", "signal", "subprocess", "tarfile", "tempfile",
    "time", "tomllib", "resource", "uuid", "zipfile", "ssl", "hashlib", "base64",
    # third-party
    "httpx", "psutil",
    "cryptography.exceptions",
    "cryptography.hazmat.primitives.asymmetric.ed25519",
    # jstack_host closure of the supervisor's tick
    "jstack_host.release_manifest",
    "jstack_host.release_channel",
    "jstack_host.fleet_updates",
    "jstack_host.hostenv",
    "jstack_host.update_supervisor",
    "jstack_host.update_macos",
    "jstack_host.update_app",
    "jstack_host.update_plugins",
    "jstack_host.app_services",
    "jstack_host.install_host",
    "jstack_host.service_settings",
    "jstack_host.sourcestamp",
    "jstack_host.releases",
    "jstack_host.migrate_services",
    "jstack_host.devices",
    "jstack_host.managed_access",
    "jstack_host.embed",
    "jstack_host.plugin_paths",
    "jstack_host.doctor",
    "jstack_host.allowance",
    "jstack_host.attach_parent",
    "jstack_host.auth",
    "jstack_host.board_watch",
    "jstack_host.desk",
    "jstack_host.fileshare",
    "jstack_host.grants",
    "jstack_host.mode",
    "jstack_host.store",
    "jstack_host.timeline",
    "jstack_host.tunnel",
    "jstack_host.attach",
    "jstack_host.board",
    "jstack_host.codex_transcript",
    "jstack_host.managed",
    "jstack_host.notify",
    "jstack_host.open_mode",
    "jstack_host.codex_commands",
    "jstack_host.composer",
    "jstack_host.engines",
    "jstack_host.load",
    "jstack_host.messages",
    "jstack_host.procscan",
    "jstack_host.seats",
    "jstack_host.turns",
    "jstack_host.open_path",
)


def updater() -> None:
    import importlib
    for name in MODULES:
        importlib.import_module(name)
