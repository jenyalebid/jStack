"""One-time local bootstrap for remotely managed updates.

Trust is installed deliberately, not learned from a network response. Old
leaves need this bootstrap once; enrollment alone is not an execution agent.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import devices, embed, hostenv, install_host
from .update_supervisor import atomic_json
from .update_macos import atomic_bytes, bundle_info, client_distribution

LABEL = "com.jremote.updater"


def repair_native(public_key: str, settings: dict, *, state_dir: Path | None,
                  candidate_test: bool) -> dict:
    """Observe an installed recovery owner without replacing trust or approval.

    Initial provisioning and legacy cutover belong to the journaled installer.
    Repeating the old bootstrap command must never recreate its LaunchAgent.
    """
    from . import app_services
    from .update_app import control
    state_value = settings["environment"].get("JREMOTE_STATE_DIR")
    if not state_value or not Path(state_value).is_absolute():
        raise ValueError("signed updater requires an explicit installed state directory")
    state = Path(state_value)
    if state_dir is not None and state_dir.resolve() != state.resolve():
        raise ValueError("updater repair cannot change the installed host identity")
    owner = Path(settings["app"])
    app_services.verify(owner)
    config_path = state / "updates/config.json"
    configuration = json.loads(config_path.read_text()) if config_path.exists() else {}
    if configuration.get("public_key") != public_key:
        raise ValueError("updater trust is absent or different; use explicit installer trust provisioning")
    if (configuration.get("service_model") != "app" or
            configuration.get("menubar_path") != settings["app"] or
            configuration.get("local_url") != f"http://127.0.0.1:{settings['port']}" or
            configuration.get("host_capability") != settings.get("host_capability")):
        raise ValueError("updater does not match the signed installation; use the migration installer")
    if bool(configuration.get("candidate_test", False)) != candidate_test:
        raise ValueError("updater repair cannot change release-channel trust")
    observed = control(owner, "status").get("updater")
    if observed not in {"enabled", "requires_approval", "not_registered"}:
        raise ValueError("cannot observe signed updater approval")
    return {"machine": configuration["machine"], "state_dir": str(state),
            "supervisor": str(owner), "status": observed}


def stage_runtime(package: Path, root: Path) -> Path:
    """Keep the bootstrap's exact source identity beside its copied package."""
    from . import sourcestamp
    identity_path = package.parent / "release-identity.json"
    identity = (json.loads(identity_path.read_text()) if identity_path.exists() else
                {**sourcestamp.capture(), "package_sha256": sourcestamp.fingerprint(package)})
    identity.setdefault("release", "")
    identity_bytes = json.dumps(identity, sort_keys=True).encode()
    stamp = hashlib.sha256(identity_bytes + b"".join(
        p.read_bytes() for p in sorted(package.glob("*.py")))).hexdigest()[:16]
    destination = root / "bootstrap" / stamp
    if not destination.exists():
        import tempfile
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".stage-", dir=destination.parent))
        shutil.copytree(package, staging / "jstack_host",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        atomic_json(staging / "release-identity.json", identity)
        os.rename(staging, destination)
    return destination


def bootstrap(public_key: str, *, state_dir: Path | None = None, load=True,
              candidate_test=False) -> dict:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
    from . import app_services, service_settings
    settings = service_settings.read()
    if settings:
        return repair_native(public_key, settings, state_dir=state_dir,
                             candidate_test=candidate_test)
    if app_services.bundled():
        raise ValueError("signed installation settings are absent; use the signed installer")
    install_host.adopt_installed_environment()
    if state_dir is not None:
        os.environ["JREMOTE_STATE_DIR"] = str(state_dir)
        hostenv.reset_profile()
    state = hostenv.state_dir()
    root = state / "updates"
    old_config = json.loads((root / "config.json").read_text()) if (root / "config.json").exists() else {}
    if old_config and old_config.get("public_key") != public_key:
        raise ValueError("updater already trusts a different key; explicit trust rotation required")
    marker = embed.read()
    host_label = marker.get("agent_label") or install_host.LABEL
    host_plist = install_host.plist_path(host_label)
    host_job = plistlib.loads(host_plist.read_bytes())
    menu_plist = install_host.plist_path("com.jremote.menubar")
    menu_job = plistlib.loads(menu_plist.read_bytes())
    menu_exe = Path(menu_job["ProgramArguments"][0])
    menu_app = next(p for p in menu_exe.parents if p.suffix == ".app")
    client = Path("/Applications/jRemote.app")
    info = bundle_info(client) if client.exists() else {}
    signing_target = client if client.exists() else menu_app
    signing = subprocess.run(["/usr/bin/codesign", "-dv", "--verbose=4", str(signing_target)],
                             capture_output=True, text=True, check=True).stderr
    team = re.search(r"^TeamIdentifier=([A-Z0-9]+)$", signing, re.MULTILINE)
    if team is None:
        raise ValueError("a Developer ID signed menu or client is required to establish update trust")
    # Mints only the machine's own existing plumbing credential; never rotates
    # a paired device or revives a revoked internal credential.
    token = devices.internal_token()
    if not token:
        raise ValueError("local administrative credential unavailable")
    from . import attach_parent
    port = marker.get("port") or install_host.installed_port(host_plist) or 9090
    configuration = {"public_key": public_key, "team_id": team[1],
                     "candidate_test": candidate_test,
                     "machine": hostenv.host_id(), "managed": bool(attach_parent.parent_record()),
                     "local_url": f"http://127.0.0.1:{port}",
                     "token_path": str(devices._credential_dir() / "internal-token"),
                     "python": host_job["ProgramArguments"][0],
                     "launcher_path": shutil.which("jstack-host") or str(Path.home() / ".local/bin/jstack-host"),
                     "host_plist": str(host_plist), "host_label": host_label,
                     "menubar_plist": str(menu_plist), "menubar_label": menu_job["Label"],
                     "menubar_path": str(menu_app), "client_path": str(client),
                     "menubar_bundle_id": bundle_info(menu_app)["CFBundleIdentifier"],
                     "client_bundle_id": info.get("CFBundleIdentifier", ""),
                     "client_managed": client_distribution(client, {**old_config, "client_managed":
                                         old_config.get("client_managed", bool(old_config))}) == "hub"}
    # Versioned stable bootstrap. Never overwrite imported supervisor modules
    # while an older process could still be recovering a transaction.
    package = Path(__file__).parent
    bootstrap_dir = stage_runtime(package, root)
    configuration["runtime_imports"] = [str(bootstrap_dir)]
    configuration["dispatcher"] = str(bootstrap_dir / "jstack_host/update_dispatcher.py")
    from . import releases as app_releases
    configuration["feed_dir"] = str(app_releases.RELEASE_DIR.parent / "fleet")
    from .release_channel import repository
    origin = subprocess.run(["git", "-C", str(package), "remote", "get-url", "origin"],
                            capture_output=True, text=True)
    identity_path = package.parent / "release-identity.json"
    identity = json.loads(identity_path.read_text()) if identity_path.exists() else {}
    source = old_config.get("github_repo") or identity.get("github_repo") or origin.stdout.strip()
    if source:
        configuration["github_repo"] = repository(source)
    atomic_json(root / "config.json", configuration)
    logs = root / "logs"
    logs.mkdir(exist_ok=True)
    job = {"Label": LABEL, "ProgramArguments": [sys.executable, configuration["dispatcher"],
                                               "--state-dir", str(state)],
           "WorkingDirectory": str(bootstrap_dir),
           "EnvironmentVariables": {"PATH": hostenv.spawn_path()},
           "KeepAlive": True, "RunAtLoad": True, "ThrottleInterval": 10,
           "StandardOutPath": str(logs / "supervisor.out"),
           "StandardErrorPath": str(logs / "supervisor.err")}
    plist = install_host.plist_path(LABEL)
    if load:
        subprocess.run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    atomic_bytes(plist, plistlib.dumps(job))
    if load:
        subprocess.run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], check=True)
    return {"machine": configuration["machine"], "state_dir": str(state), "supervisor": str(plist)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trust-key", type=Path,
                        help="local file containing the base64 Ed25519 release public key")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--candidate-test", action="store_true",
                        help="explicit lab-only bootstrap: accept signed, unpromoted candidates")
    args = parser.parse_args()
    public = (args.trust_key.read_text().strip() if args.trust_key else
              json.loads((Path(__file__).parent / "release-trust.json").read_text())["public_key"])
    print(json.dumps(bootstrap(public, state_dir=args.state_dir,
                               candidate_test=args.candidate_test), indent=2))


if __name__ == "__main__":
    main()
