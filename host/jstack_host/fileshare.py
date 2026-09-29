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
  gives that account an inherited read/write ACL on each root.  Inheritance
  governs only what is created after it, so setup then carries the same grant
  through the existing tree -- unprivileged, as the owner of those files.

It deliberately does not turn File Sharing on.  The System Settings action is
the supported macOS path and carries OS-owned privacy authorization that a
launchctl imitation cannot honestly claim to reproduce.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import platform
import pwd
import re
import shlex
import subprocess
import sys
import tempfile
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
OSASCRIPT = "/usr/bin/osascript"
CAT = "/bin/cat"

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
# On a file the same grant prints in file words, and inheritance is meaningless
# there.  These are what `ls -le` must show on an existing file inside a root.
ACL_OBSERVED_FILE_RIGHTS = (
    "read", "write", "append", "delete",
    "readattr", "writeattr", "readextattr", "writeextattr",
)

AUDIT_INTERVAL = 300.0


class FileShareError(RuntimeError):
    """A status or requested transition could not be completed safely."""


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=20)


def _stack_root() -> Path:
    """The directory whose Agents child the host is configured to serve."""
    if os.geteuid() == 0 and not os.environ.get("JREMOTE_INSTANCE_ROOT"):
        raise FileShareError("root must name --agents-root explicitly; refusing to guess an installation")
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


def _parse_membership(text: str) -> bool | None:
    """Read `dseditgroup -o checkmember`'s answer out of its sentence.

    Its exit code is not the contract and has moved: macOS 26 answers a
    non-member with 67, where the parser once demanded 1 and read a clear
    "is NOT a member" as unknown -- which withheld readiness forever.
    """
    said = text.strip().lower()
    if "is not a member" in said:
        return False
    if "is a member" in said:
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
    smb_hash = (None if hashes.returncode or not hashes.stdout.strip()
                else "SMB-NT" in hashes.stdout.split())
    is_admin = _parse_membership(f"{admin.stdout} {admin.stderr}")
    return {
        "exists": True,
        "name": ACCOUNT,
        "home": fields.get("NFSHomeDirectory", ""),
        "shell": fields.get("UserShell", ""),
        "uid": fields.get("UniqueID", ""),
        "admin": is_admin,
        "smb_hash": smb_hash,
    }


def _acl_entry(user: str) -> str:
    return f"user:{user} allow {','.join(ACL_RIGHTS)}"


def _root_owner(path: Path) -> str:
    return pwd.getpwuid(path.stat().st_uid).pw_name


def _acl_ok(path: Path, user: str = ACCOUNT, *, directory: bool = True) -> bool:
    expected = ACL_OBSERVED_RIGHTS if directory else ACL_OBSERVED_FILE_RIGHTS
    r = _run([LS, "-lde", str(path)])
    if r.returncode != 0:
        return False
    allowed = False
    for line in r.stdout.splitlines()[1:]:
        match = re.fullmatch(r"\s*\d+:\s+(\S+)\s+(?:inherited\s+)?(allow|deny)\s+(.+)", line)
        if not match:
            continue
        principal, permission, raw = match.groups()
        # Group membership/effective deny evaluation is not observable here.
        # Conservatively withhold readiness for any deny ACE.
        if permission == "deny":
            return False
        rights = raw.replace(" ", "").split(",")
        if principal == f"user:{user}" and set(expected).issubset(rights):
            allowed = True
    return allowed


def _children_acl_ok(path: Path, user: str = ACCOUNT) -> bool | None:
    """Whether the root's existing children carry the grant too.

    An inheriting ACE reaches only what is created after it is laid.  A tree
    that predates setup keeps exactly what it had -- for a world-readable home
    that is read without write, which presents as a browsable share that
    refuses every save.  This observes the root's immediate children: one
    level, named for what it sees, and enough to tell a granted root from a
    granted tree.
    """
    try:
        children = [c for c in sorted(path.iterdir()) if not c.is_symlink()]
    except OSError:
        return None
    for child in children:
        try:
            directory = child.is_dir()
        except OSError:
            return None
        if not _acl_ok(child, user, directory=directory):
            return False
    return True


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
        "children_acl_ok": _children_acl_ok(path),
        "owner_children_acl_ok": owner == ACCOUNT or _children_acl_ok(path, owner),
    }


def status() -> dict:
    """Declared and observed file-sharing state, without privilege or writes."""
    desired = desired_shares()
    if not _supported():
        return {"available": False, "configured": False, "secure": False,
                "ready": False,
                "reason": "macOS SMB sharing is not available", "shares": [],
                "unexpected": [], "security_problems": [], "service_enabled": None,
                "guest_enabled": None, "unverified": [],
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
    account_ok = (account["exists"] and account["admin"] is False and
                  account["home"] == ACCOUNT_HOME and
                  account["shell"] == ACCOUNT_SHELL)
    guest = _guest_enabled()
    service = _service_enabled()
    configured = bool(rows) and all(row["present"] for row in rows)
    # Reading the account's hash types is root-only, and the host is sealed
    # against root: demanding a `True` there made `ready` unreachable in the
    # one configuration this ships in.  Unknown blocks nothing and is named in
    # `unverified`; an observed `False` still blocks.
    unverified = [f"account.{key}" for key in ("admin", "smb_hash")
                  if account.get(key) is None]
    secure = (configured and not unexpected and account_ok and
              account.get("smb_hash") is not False and guest is False and
              all(all(row[k] for k in ("path_ok", "guest_off", "writable",
                                       "encrypted", "shared", "acl_ok",
                                       "owner_acl_ok", "children_acl_ok",
                                       "owner_children_acl_ok"))
                  for row in rows))
    problems = [{"kind": "unexpected_share", **row} for row in unexpected]
    for row in rows:
        if not row["present"]:
            continue
        failed = [key for key in ("path_ok", "guest_off", "encrypted", "shared",
                                  "children_acl_ok", "owner_children_acl_ok")
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
        "unverified": unverified,
        "account": {**account, "ok": account_ok},
    }


def _display(argv: list[str]) -> str:
    return shlex.join(argv)


def _needs_password(argv: list[str]) -> bool:
    """The two plan steps that set the sharing account's password."""
    return ((argv[0] == SYSADMINCTL and "-addUser" in argv)
            or (argv[0] == DSCL and "-passwd" in argv))


def _administrator_script(commands: list[list[str]], password_file: Path | None) -> str:
    """The plan as one `set -eu` shell script of fixed system tools, for the
    OS administrator prompt.  The password steps read the account password
    from `password_file` (user-owned, 0600) instead of a terminal: under the
    prompt there is no tty for `sysadminctl -password -` or `dscl -passwd` to
    ask on.  The script itself never contains the password."""
    lines = ["set -eu", "umask 077", "export LC_ALL=C"]
    if password_file is not None:
        lines.append("pw=$(" + CAT + " " + shlex.quote(str(password_file)) + ")")
    for argv in commands:
        if _needs_password(argv):
            if password_file is None:
                raise FileShareError("plan sets a password but none was collected")
            if argv[0] == SYSADMINCTL:
                argv = [a if a != "-" else '"$pw"' for a in argv]
                words = [shlex.quote(a) if a != '"$pw"' else a for a in argv]
            else:
                words = [shlex.quote(a) for a in argv] + ['"$pw"']
            lines.append(" ".join(words))
        else:
            lines.append(_display(argv))
    return "\n".join(lines)


def _run_as_administrator(script: str, prompt: str, runner=subprocess.run) -> None:
    """Execute a reviewed script of fixed system tools through the OS
    administrator prompt, as the logged-in user.  The sealed runtime refuses
    to start as root, and the identity rule behind that — root never runs
    user-writable Python — holds here too: what root runs is the plan's own
    `sharing`, `sysadminctl`, `dscl`, `pwpolicy` and `chmod` lines, nothing
    from the package."""
    literal = '"' + script.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    source = ("do shell script " + literal + " with administrator privileges "
              "with prompt " + json.dumps(prompt))
    result = runner([OSASCRIPT, "-"], input=source, capture_output=True, text=True, timeout=600)
    if result.returncode:
        raise FileShareError("administrator approval or setup command failed: "
                             + (result.stderr or result.stdout)[-1000:].strip())


def _collect_password() -> str:
    if not sys.stdin.isatty():
        raise FileShareError("the sharing account needs a password and there is no "
                             "terminal to ask on; run this from a terminal")
    first = getpass.getpass(f"password for the {ACCOUNT} account (what a device types to mount): ")
    if not first:
        raise FileShareError("empty password refused")
    if getpass.getpass("again: ") != first:
        raise FileShareError("passwords did not match")
    return first


def _private_password_file(password: str) -> Path:
    folder = Path(tempfile.mkdtemp(prefix="jstack-fileshare-", dir=None))
    folder.chmod(0o700)
    path = folder / "account-password"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(password)
    return path


def _discard(path: Path) -> None:
    try:
        path.unlink()
        path.parent.rmdir()
    except OSError:
        pass


def setup_plan(observed: dict | None = None) -> list[list[str]]:
    """The exact privileged commands needed to reach selected-folder state."""
    observed = observed or status()
    if not observed.get("available"):
        raise FileShareError(observed.get("reason") or "file sharing unavailable")
    desired = desired_shares()
    if not desired:
        raise FileShareError("no selected roots exist; refusing to modify share points")
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

    removed = set()
    for row in observed.get("unexpected", []):
        commands.append([SHARING, "-r", row["name"]])
        removed.add(row["name"])

    by_name = {row["name"]: row for row in observed.get("shares", [])}
    for name, path in desired.items():
        row = by_name.get(name, {})
        if row.get("present") and not row.get("path_ok"):
            if name not in removed:
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
    # Account credentials, ACLs and guest policy must succeed before exposure.
    return ([c for c in commands if c[0] != SHARING] +
            [c for c in commands if c[0] == SHARING])


def tree_plan(observed: dict | None = None) -> list[list[str]]:
    """The unprivileged second half of setup: carry the grant into the tree.

    The privileged plan lays an inheriting ACE on each root, which governs
    what is created from then on.  Everything already in the tree keeps the
    permissions it had, so a share can mount, list and read while refusing
    every write -- the state this pass exists to end.  `chmod -R` needs no
    root: the roots and what is under them belong to the host's own user, and
    an owner may set an ACL on what it owns.
    """
    observed = observed or status()
    rows = {row["name"]: row for row in observed.get("shares", [])}
    commands: list[list[str]] = []
    for name, path in desired_shares().items():
        row = rows.get(name, {})
        if not row.get("children_acl_ok"):
            commands.append([CHMOD, "-R", "+a", _acl_entry(ACCOUNT), str(path)])
        owner = row.get("owner") or _root_owner(path)
        if owner != ACCOUNT and not row.get("owner_children_acl_ok"):
            commands.append([CHMOD, "-R", "+a", _acl_entry(owner), str(path)])
    return commands


def _apply_tree(commands: list[list[str]], runner=subprocess.run) -> list[dict]:
    """Run the recursive grants, reporting what each one could not reach.

    A file owned by someone other than the host user -- one the sharing
    account itself created before the grant existed -- refuses an ACL change
    from that user.  `chmod -R` walks past it and exits non-zero; that is a
    named leftover, not a reason to abandon the rest of the tree.
    """
    out = []
    for argv in commands:
        print(f"granting access across {argv[-1]} (large trees take minutes)",
              file=sys.stderr, flush=True)
        r = runner(argv, capture_output=True, text=True)
        refused = [line for line in (r.stderr or "").splitlines() if line.strip()]
        out.append({"command": _display(argv), "returncode": r.returncode,
                    "refused": len(refused), "first_refusal": refused[0] if refused else ""})
    return out


def setup(*, apply: bool = False, runner=subprocess.run) -> dict:
    """Print the plan, or apply it.  Applying as the logged-in user goes
    through the OS administrator prompt (the sealed `jstack-host` cannot be
    started under sudo); applying as root, on an unsealed install, runs the
    plan directly."""
    before = status()
    commands = setup_plan(before)
    tree = tree_plan(before)
    if not apply:
        return {"applied": False, "commands": [_display(c) for c in commands],
                "tree_commands": [_display(c) for c in tree],
                "status": before,
                "note": "dry run; re-run with --apply (an administrator prompt "
                        "opens), then enable File Sharing in System Settings"}
    if os.geteuid() != 0:
        password_file = None
        if any(_needs_password(c) for c in commands):
            password_file = _private_password_file(_collect_password())
        try:
            script = _administrator_script(commands, password_file)
            _run_as_administrator(script, "Set up jStack selected-folder file sharing", runner)
        finally:
            if password_file is not None:
                _discard(password_file)
        granted = _apply_tree(tree)
        after = status()
        if not after["secure"]:
            raise FileShareError("setup commands completed but observed state is still not secure")
        return {"applied": True, "commands": [_display(c) for c in commands],
                "tree": granted, "status": after,
                "note": "share points are ready; enable File Sharing in System Settings"}
    roots = desired_shares()
    identities = {name: (path.stat().st_dev, path.stat().st_ino) for name, path in roots.items()}
    completed = 0
    for argv in commands:
        for name, path in roots.items():
            if path.is_symlink() or not path.is_dir() or (path.stat().st_dev, path.stat().st_ino) != identities[name]:
                raise FileShareError(f"selected root changed; stopped after {completed} commands: {name}")
        r = subprocess.run(argv, text=True, timeout=120)
        if r.returncode != 0:
            raise FileShareError(f"partial setup: {completed} commands applied; command failed ({r.returncode}): {_display(argv)}")
        completed += 1
    granted = _apply_tree(tree)
    after = status()
    if not after["secure"]:
        raise FileShareError("setup commands completed but observed state is still not secure")
    return {"applied": True, "commands": [_display(c) for c in commands],
            "tree": granted, "status": after,
            "note": "share points are ready; enable File Sharing in System Settings"}


def off(*, apply: bool = False, runner=subprocess.run) -> dict:
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
        if commands:
            _run_as_administrator(_administrator_script(commands, None),
                                  "Remove jStack SMB share points", runner)
        return {"applied": True, "commands": [_display(c) for c in commands],
                "status": status(),
                "note": "share points removed; switch File Sharing off in System Settings"}
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
            await asyncio.to_thread(audit_once)
        except Exception as e:  # noqa: BLE001 -- an audit never kills the host
            print(f"jremote SECURITY (file-share audit failed: {type(e).__name__}): {e}",
                  flush=True)
        await asyncio.sleep(interval)


def serves_files() -> bool:
    try:
        return bool(status()["ready"])
    except Exception:
        return False
