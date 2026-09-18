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
    "time", "uuid", "zipfile", "ssl", "hashlib", "base64",
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
)


def updater() -> None:
    import importlib
    for name in MODULES:
        importlib.import_module(name)
