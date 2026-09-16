"""Declared SMB file access for a jStack host.

The macOS account home is not a share boundary.  It contains credentials,
private application data and whatever a future tool happens to create there.
The host instead publishes exactly three independent roots -- Agents, Systems
and Projects -- when they exist beside the configured Agents root.  Anything
else in Directory Services' share-point table is drift.

This module owns both sides of that contract:

* :func:`status` is unprivileged and is the one observation used by the CLI,
  API, doctor and audit.
* :func:`setup` is a dry-run unless explicitly applied as root.  It creates a
  non-admin, non-login account, removes every undeclared share point, declares
  the three roots with guest access disabled and SMB3 encryption required, and
  gives that account one inherited read/write ACL on each root.

It deliberately does not turn File Sharing on.  The System Settings action is
the supported macOS path and carries OS-owned privacy authorization that a
launchctl imitation cannot honestly claim to reproduce.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import pwd
import subprocess
from pathlib import Path

from . import hostenv

SHARING = "/usr/sbin/sharing"
SYSADMINCTL = "/usr/sbin/sysadminctl"
DSCL = "/usr/bin/dscl"
DSEDITGROUP = "/usr/sbin/dseditgroup"
PWPOLICY = "/usr/bin/pwpolicy"
CHMOD = "/bin/chmod"
LS = "/bin/ls"
LAUNCHCTL = "/bin/launchctl"

ACCOUNT = "jstackshare"
ACCOUNT_HOME = "/Users/Shared/.jstackshare"
ACCOUNT_SHELL = "/usr/bin/false"
SHARE_NAMES = ("Agents", "Systems", "Projects")

ACL_RIGHTS = (
    "list", "search", "add_file", "add_subdirectory", "delete_child",
    "readattr", "writeattr", "readextattr", "writeextattr", "read",
    "write", "append", "delete", "file_inherit", "directory_inherit",
)
# chmod expresses file read/write/append on a directory as their directory
# equivalents (list/add_file/add_subdirectory) when it prints the ACL back.
# These are the canonical words `ls -lde` must therefore show.
ACL_OBSERVED_RIGHTS = tuple(
    right for right in ACL_RIGHTS if right not in {"read", "write", "append"}
)

AUDIT_INTERVAL = 300.0


class FileShareError(RuntimeError):
    """A status or requested transition could not be completed safely."""


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=20)


def _stack_root() -> Path:
    """The directory whose Agents child the host is configured to serve."""
    return hostenv.instance_root().expanduser().resolve().parent


def desired_shares() -> dict[str, Path]:
    """Existing selected roots, by their stable SMB names.

    Missing roots are absent rather than created.  The same host package runs
    on leaves that may have no Projects or Systems tree, and setup must not
    invent an empty directory then advertise it as useful file access.
    """
    root = _stack_root()
    out: dict[str, Path] = {}
    for name in SHARE_NAMES:
        path = root / name
        if path.is_dir() and not path.is_symlink():
            out[name] = path.resolve()
    return out


def _supported() -> bool:
    return platform.system() == "Darwin" and Path(SHARING).exists()


def _actual_shares() -> dict[str, dict]:
    r = _run([SHARING, "-l", "-f", "json"])
    if r.returncode != 0:
        raise FileShareError((r.stderr or r.stdout or "sharing -l failed").strip())
    try:
        raw = json.loads(r.stdout or "{}")
    except (TypeError, json.JSONDecodeError) as e:
        raise FileShareError(f"sharing -l returned invalid JSON: {e}") from e
    if not isinstance(raw, dict):
        raise FileShareError("sharing -l returned a non-object")
    return {str(name): dict(row) for name, row in raw.items()
            if isinstance(row, dict)}


def _service_enabled() -> bool | None:
    r = _run([LAUNCHCTL, "print-disabled", "system"])
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        if '"com.apple.smbd"' in line:
            if "=> enabled" in line:
                loaded = _run([LAUNCHCTL, "print", "system/com.apple.smbd"])
                return loaded.returncode == 0
            if "=> disabled" in line:
                return False
    return None


def _guest_enabled() -> bool | None:
    r = _run([SYSADMINCTL, "-smbGuestAccess", "status"])
    text = f"{r.stdout}\n{r.stderr}".lower()
    if "disabled" in text:
        return False
    if "enabled" in text:
        return True
    return None


def _account() -> dict:
    r = _run([DSCL, ".", "-read", f"/Users/{ACCOUNT}", "NFSHomeDirectory",
              "UserShell", "UniqueID"])
    if r.returncode != 0:
        return {"exists": False, "name": ACCOUNT, "home": "", "shell": "",
                "admin": False, "smb_hash": False}
    fields: dict[str, str] = {}
    for line in r.stdout.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    admin = _run([DSEDITGROUP, "-o", "checkmember", "-m", ACCOUNT, "admin"])
    hashes = _run([PWPOLICY, "-u", ACCOUNT, "-gethashtypes"])
    # Reading hash *types* is root-only on current macOS. Unknown is kept as
    # unknown instead of pretending the credential is either armed or broken.
    smb_hash = None if not hashes.stdout.strip() else "SMB-NT" in hashes.stdout.split()
    return {
        "exists": True,
        "name": ACCOUNT,
        "home": fields.get("NFSHomeDirectory", ""),
        "shell": fields.get("UserShell", ""),
        "uid": fields.get("UniqueID", ""),
        "admin": "yes" in f"{admin.stdout}\n{admin.stderr}".lower(),
        "smb_hash": smb_hash,
    }


def _acl_entry(user: str) -> str:
    return f"user:{user} allow {','.join(ACL_RIGHTS)}"


def _root_owner(path: Path) -> str:
    return pwd.getpwuid(path.stat().st_uid).pw_name


def _acl_ok(path: Path, user: str = ACCOUNT) -> bool:
    r = _run([LS, "-lde", str(path)])
    if r.returncode != 0:
        return False
    for line in r.stdout.splitlines()[1:]:
        folded = " ".join(line.strip().split())
        if f"user:{user}" not in folded or " allow " not in folded:
            continue
        rights = folded.split(" allow ", 1)[1].replace(" ", "").split(",")
        if set(ACL_OBSERVED_RIGHTS).issubset(rights):
            return True
    return False


def _row(name: str, path: Path, actual: dict | None) -> dict:
    actual = actual or {}
    actual_path = str(actual.get("path") or "")
    owner = _root_owner(path)
    return {
        "name": name,
        "path": str(path),
        "present": bool(actual),
        "path_ok": bool(actual) and Path(actual_path).resolve() == path,
        "guest_off": actual.get("smb_guest_access") == 0,
        "writable": actual.get("smb_read_only") == 0,
        "encrypted": actual.get("smb_sealed") == 1,
        "shared": actual.get("smb_shared") == 1,
        "owner": owner,
        "acl_ok": _acl_ok(path),
        # An SMB-created file belongs to the sharing account.  This inherited
        # ACE keeps the host's normal user able to edit that same live file.
        "owner_acl_ok": _acl_ok(path, owner),
    }


def status() -> dict:
    """Declared and observed file-sharing state, without privilege or writes."""
    desired = desired_shares()
    if not _supported():
        return {"available": False, "configured": False, "secure": False,
                "ready": False,
                "reason": "macOS SMB sharing is not available", "shares": [],
                "unexpected": [], "security_problems": [], "service_enabled": None,
                "guest_enabled": None,
                "account": {"exists": False, "name": ACCOUNT, "ok": False,
                            "smb_hash": False}}

    actual = _actual_shares()
    rows = [_row(name, path, actual.get(name)) for name, path in desired.items()]
    desired_paths = {str(p) for p in desired.values()}
    unexpected = [
        {"name": name, "path": str(row.get("path") or "")}
        for name, row in actual.items()
        if name not in desired or str(Path(str(row.get("path") or "")).resolve())
        not in desired_paths
    ]
    account = _account()
    account_ok = (account["exists"] and not account["admin"] and
                  account["home"] == ACCOUNT_HOME and
                  account["shell"] == ACCOUNT_SHELL)
    guest = _guest_enabled()
    service = _service_enabled()
    configured = bool(rows) and all(row["present"] for row in rows)
    secure = (configured and not unexpected and account_ok and
              account.get("smb_hash") is not False and guest is False and
              all(all(row[k] for k in ("path_ok", "guest_off", "writable",
                                       "encrypted", "shared", "acl_ok",
                                       "owner_acl_ok"))
                  for row in rows))
    problems = [{"kind": "unexpected_share", **row} for row in unexpected]
    for row in rows:
        if not row["present"]:
            continue
        failed = [key for key in ("path_ok", "guest_off", "encrypted", "shared")
                  if not row[key]]
        if failed:
            problems.append({"kind": "share_security_drift", "name": row["name"],
                             "path": row["path"], "failed": failed})
    if configured and guest is True:
        problems.append({"kind": "global_guest_enabled"})
    if configured and account.get("admin"):
        problems.append({"kind": "sharing_account_is_admin", "name": ACCOUNT})
    return {
        "available": True,
        "configured": configured,
        "secure": secure,
        "ready": secure and service is True,
        "shares": rows,
        "unexpected": unexpected,
        "security_problems": problems,
        "service_enabled": service,
        "guest_enabled": guest,
        "account": {**account, "ok": account_ok},
    }


def _display(argv: list[str]) -> str:
    import shlex
    return shlex.join(argv)


def setup_plan(observed: dict | None = None) -> list[list[str]]:
    """The exact privileged commands needed to reach selected-folder state."""
    observed = observed or status()
    if not observed.get("available"):
        raise FileShareError(observed.get("reason") or "file sharing unavailable")
    account = observed["account"]
    if account["exists"] and not account.get("ok"):
        raise FileShareError(
            f"account {ACCOUNT!r} exists but is not the declared non-admin "
            f"sharing account; refusing to repurpose it")

    commands: list[list[str]] = []
    if not account["exists"]:
        commands.append([SYSADMINCTL, "-addUser", ACCOUNT,
                         "-fullName", "jStack File Sharing",
                         "-shell", ACCOUNT_SHELL, "-home", ACCOUNT_HOME,
                         "-password", "-"])
        commands.append([DSCL, ".", "-create", f"/Users/{ACCOUNT}",
                         "IsHidden", "1"])
    if not account["exists"] or account.get("smb_hash") is False:
        # Creating a local password does not create the NT hash SMB uses.  The
        # native File Sharing UI enables the hash then asks for the password;
        # reproduce that order without ever putting the password in argv.
        commands.append([PWPOLICY, "-u", ACCOUNT, "-sethashtypes",
                         "SMB-NT", "on"])
        commands.append([DSCL, ".", "-passwd", f"/Users/{ACCOUNT}"])

    for row in observed.get("unexpected", []):
        commands.append([SHARING, "-r", row["name"]])

    by_name = {row["name"]: row for row in observed.get("shares", [])}
    for name, path in desired_shares().items():
        row = by_name.get(name, {})
        if row.get("present") and not row.get("path_ok"):
            commands.append([SHARING, "-r", name])
            row = {}
        if not row.get("present"):
            commands.append([SHARING, "-a", str(path), "-n", name,
                             "-S", name, "-s", "001", "-g", "000",
                             "-R", "0", "-E", "1"])
        elif not all(row.get(k) for k in ("guest_off", "writable",
                                          "encrypted", "shared")):
            commands.append([SHARING, "-e", name, "-S", name,
                             "-s", "001", "-g", "000", "-R", "0",
                             "-E", "1"])
        if not row.get("acl_ok"):
            commands.append([CHMOD, "+a", _acl_entry(ACCOUNT), str(path)])
        owner = row.get("owner") or _root_owner(path)
        if owner != ACCOUNT and not row.get("owner_acl_ok"):
            commands.append([CHMOD, "+a", _acl_entry(owner), str(path)])

    if observed.get("guest_enabled") is not False:
        commands.append([SYSADMINCTL, "-smbGuestAccess", "off"])
    return commands


def setup(*, apply: bool = False) -> dict:
    if apply and os.geteuid() != 0:
        raise PermissionError("--apply requires root; run sudo jstack-host files setup --apply")
    before = status()
    commands = setup_plan(before)
    if not apply:
        return {"applied": False, "commands": [_display(c) for c in commands],
                "status": before,
                "note": "dry run; re-run with sudo and --apply, then enable "
                        "File Sharing in System Settings"}
    for argv in commands:
        r = subprocess.run(argv, text=True, timeout=120)
        if r.returncode != 0:
            raise FileShareError(f"command failed ({r.returncode}): {_display(argv)}")
    after = status()
    if not after["secure"]:
        raise FileShareError("setup commands completed but observed state is still not secure")
    return {"applied": True, "commands": [_display(c) for c in commands],
            "status": after,
            "note": "share points are ready; enable File Sharing in System Settings"}


def off(*, apply: bool = False) -> dict:
    observed = status()
    names = sorted({row["name"] for row in observed.get("shares", [])
                    if row.get("present")} |
                   {row["name"] for row in observed.get("unexpected", [])})
    commands = [[SHARING, "-r", name] for name in names]
    if not apply:
        return {"applied": False, "commands": [_display(c) for c in commands],
                "status": observed,
                "note": "dry run; --apply removes share points but File Sharing "
                        "must be switched off in System Settings"}
    if os.geteuid() != 0:
        raise PermissionError("--apply requires root; run sudo jstack-host files off --apply")
    for argv in commands:
        r = subprocess.run(argv, text=True, timeout=30)
        if r.returncode != 0:
            raise FileShareError(f"command failed ({r.returncode}): {_display(argv)}")
    return {"applied": True, "commands": [_display(c) for c in commands],
            "status": status(),
            "note": "share points removed; switch File Sharing off in System Settings"}


_last_alert = ""


def audit_once() -> list[dict]:
    """Alert once per distinct unsafe-share state; return offending rows."""
    global _last_alert
    observed = status()
    unsafe = observed.get("security_problems", observed.get("unexpected", []))
    fingerprint = json.dumps(unsafe, sort_keys=True)
    if unsafe and fingerprint != _last_alert:
        detail = ", ".join(
            f"{r.get('kind', 'unexpected_share')}:"
            f"{r.get('name', '')}={r.get('path', '')}" for r in unsafe)
        hostenv.security_alert(f"undeclared SMB share point(s): {detail}")
    _last_alert = fingerprint if unsafe else ""
    return unsafe


async def audit_loop(interval: float = AUDIT_INTERVAL) -> None:
    """Continuous, failure-isolated share audit for either host shape."""
    while True:
        try:
            audit_once()
        except Exception as e:  # noqa: BLE001 -- an audit never kills the host
            print(f"jremote SECURITY (file-share audit failed: {type(e).__name__}): {e}",
                  flush=True)
        await asyncio.sleep(interval)


def serves_files() -> bool:
    try:
        return bool(status()["ready"])
    except Exception:
        return False
