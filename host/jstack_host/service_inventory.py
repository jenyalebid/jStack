"""Read-only macOS startup inventory. Never prints arguments or environment.

A service label is a claim, not evidence of its publisher. Report the launch
definition, executable signature and live launchd state separately. This does
not inspect TCC databases, approve services, or establish a trusted baseline.
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import stat
import subprocess
from pathlib import Path


def run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=15)


def app_data_path(path: Path) -> bool:
    """Do not make an inventory trigger consent to read another app's data."""
    for value in (str(path), str(path.resolve())):
        if any(part in value for part in (
            "/Library/Application Support/", "/Library/Containers/",
            "/Library/Group Containers/", "/Library/Mail/",
        )):
            return True
    return False


def fingerprint(path: Path) -> dict:
    if app_data_path(path):
        return {"unobserved": "app data path; requires a separate permission review"}
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            return {"error": "not a regular file"}
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        return {"sha256": digest, "uid": info.st_uid,
                "mode": oct(stat.S_IMODE(info.st_mode)),
                "resolved_path": str(path.resolve())}
    except OSError as exc:
        return {"error": type(exc).__name__}


def user_writable(path: Path) -> bool | None:
    """Include replaceable ancestors and symlink destinations, not just mode.

    Access is measured for the invoking account. A root invocation cannot
    establish what an ordinary account can replace and reports unknown.
    """
    if os.geteuid() == 0 or app_data_path(path):
        return None
    for target in (path, path.resolve()):
        if os.access(target, os.W_OK):
            return True
        child = target
        for parent in target.parents:
            try:
                info, child_info = parent.stat(), child.lstat()
                sticky_allows = not info.st_mode & stat.S_ISVTX or os.geteuid() in (
                    info.st_uid, child_info.st_uid)
                if sticky_allows and os.access(parent, os.W_OK | os.X_OK):
                    return True
            except OSError:
                pass
            child = parent
    return False


def signature(path: Path) -> dict:
    if app_data_path(path):
        return {"status": "unobservable", "reason": "app data path"}
    try:
        details = run(["/usr/bin/codesign", "-dv", "--verbose=4", str(path)])
        if details.returncode:
            return {"status": "unsigned_or_unreadable"}
        values = {}
        for key in ("Identifier", "TeamIdentifier"):
            match = re.search(rf"^{key}=(.+)$", details.stderr, re.MULTILINE)
            if match:
                values[key] = match[1]
        verified = run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(path)])
        return {"status": "valid" if verified.returncode == 0 else "invalid",
                **values}
    except (OSError, subprocess.SubprocessError):
        return {"status": "unobservable"}


def launch_state(domain: str, label: str) -> dict:
    try:
        result = run(["/bin/launchctl", "print", f"{domain}/{label}"])
    except (OSError, subprocess.SubprocessError):
        return {"status": "unobservable"}
    if result.returncode:
        text = result.stdout + result.stderr
        return {"status": "not_loaded" if "Could not find service" in text
                else "unobservable"}
    observed = {"status": "loaded"}
    # Only top-level job fields: nested resource coalitions also have a state.
    for key in ("state", "pid", "last exit code", "parent bundle identifier", "parent bundle version", "program identifier"):
        match = re.search(rf"^\t{key} = ([^\n]+)$", result.stdout, re.MULTILINE)
        if match:
            observed[key.replace(" ", "_")] = match[1]
    return observed


def inspect_job(path: Path, domain: str) -> dict:
    row = {"path": str(path), "domain": domain,
           "definition": fingerprint(path), "findings": []}
    try:
        job = plistlib.loads(path.read_bytes())
        if not isinstance(job, dict) or not isinstance(job.get("Label"), str):
            raise ValueError("missing label")
        label = job["Label"]
        if not label or "/" in label or any(ord(c) < 32 for c in label):
            raise ValueError("invalid label")
        argv = job.get("ProgramArguments", [])
        if not isinstance(argv, list) or any(not isinstance(a, str) for a in argv):
            raise ValueError("invalid arguments")
        program = job.get("Program") or (argv[0] if argv else None)
        owner = None
        if "BundleProgram" in job:
            relative = job["BundleProgram"]
            if (not isinstance(relative, str) or Path(relative).is_absolute() or
                    len(path.parents) < 4 or path.parents[2].name != "Contents" or
                    path.parents[3].suffix != ".app"):
                raise ValueError("invalid app-owned executable")
            owner = path.parents[3]
            executable = (owner / relative).resolve()
            if not executable.is_relative_to(owner.resolve()):
                raise ValueError("app-owned executable escapes its bundle")
            program = str(executable)
        if not isinstance(program, str) or not Path(program).is_absolute():
            raise ValueError("executable is not an absolute path")
    except (OSError, ValueError, plistlib.InvalidFileException):
        row["findings"].append("unreadable_or_unsupported_definition")
        return row
    executable = Path(program)
    row.update(label=label, executable=program, executable_file=fingerprint(executable),
               signature=signature(executable), launchd=launch_state(domain, label),
               run_as=job.get("UserName") or ("root" if domain == "system" else "login_user"),
               schedule={key: job[key] for key in
                         ("RunAtLoad", "KeepAlive", "StartInterval", "StartCalendarInterval")
                         if key in job},
               associated_bundles=job.get("AssociatedBundleIdentifiers", []))
    if owner is not None:
        row["owner_bundle"] = {"path": str(owner), "signature": signature(owner)}
        if row["owner_bundle"]["signature"]["status"] != "valid":
            row["findings"].append("owner_resource_seal_not_verified")
    # No argv dump: command lines and environment frequently contain tokens.
    # Script operands are evidence only; do not execute them or resolve -m by
    # importing a service into the inventory process.
    scripts = [Path(a) for a in argv[1:]
               if Path(a).is_absolute() and Path(a).suffix in (".py", ".sh", ".js")]
    row["scripts"] = [{"path": str(p), **fingerprint(p)} for p in scripts]
    if row["run_as"] == "root":
        writable = [str(p) for p in (path, executable, *scripts) if user_writable(p)]
        row["user_writable_root_code"] = writable
        if writable:
            row["findings"].append("root_executes_user_writable_code")
        if os.geteuid() == 0:
            row["findings"].append("ordinary_user_writability_unobservable")
    if row["signature"]["status"] != "valid":
        row["findings"].append("executable_signature_not_verified")
    if executable.name in ("bash", "sh", "zsh", "node", "python", "python3"):
        row["findings"].append("generic_runtime_identity")
    if "error" in row["executable_file"] or any("error" in s for s in row["scripts"]):
        row["findings"].append("code_file_unreadable")
    if "unobserved" in row["executable_file"] or any("unobserved" in s for s in row["scripts"]):
        row["findings"].append("code_file_not_observed")
    return row


def collect(locations: list[tuple[Path, str]] | None = None) -> dict:
    locations = locations if locations is not None else [
        (Path.home() / "Library/LaunchAgents", f"gui/{os.getuid()}"),
        (Path("/Library/LaunchAgents"), f"gui/{os.getuid()}"),
        (Path("/Library/LaunchDaemons"), "system"),
        (Path("/Applications/jStack Hub.app/Contents/Library/LaunchAgents"), f"gui/{os.getuid()}"),
        (Path("/Applications/jStack Hub Services.app/Contents/Library/LaunchAgents"), f"gui/{os.getuid()}"),
        (Path("/Library/PrivilegedHelperTools/jStack Network.app/Contents/Library/LaunchDaemons"), "system"),
    ]
    rows, errors = [], []
    for directory, domain in locations:
        try:
            paths = sorted(directory.iterdir())
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append({"path": str(directory), "error": type(exc).__name__})
            continue
        rows.extend(inspect_job(p, domain) for p in paths if p.suffix == ".plist")
    identities: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        if "label" in row:
            identities.setdefault((row["domain"], row["label"]), []).append(row)
    for group in identities.values():
        if len(group) > 1:
            for row in group:
                row["findings"].append("duplicate_launchd_label")
    return {"schema": 1, "services": rows, "errors": errors,
            "limits": ["Inventory is not a malware clearance or trusted baseline.",
                       "Includes installed jStack app definitions, not every third-party SMAppService registration.",
                       "Privacy grants and background-item approval are not inspected.",
                       "A valid signature is not proof of an approved publisher.",
                       "Interpreter modules, transitive code and child processes are not attested."]}


def compare(before: dict, after: dict) -> dict:
    """Report persistence/code drift without automatically trusting either side."""
    fields = ("definition", "executable", "executable_file", "scripts", "signature", "owner_bundle", "run_as", "schedule")
    old = {row["path"]: row for row in before["services"]}
    new = {row["path"]: row for row in after["services"]}
    changes = []
    for path in sorted(old.keys() | new.keys()):
        if path not in old or path not in new:
            changes.append({"path": path, "change": "added" if path in new else "removed"})
            continue
        changed = [field for field in fields if old[path].get(field) != new[path].get(field)]
        if changed:
            changes.append({"path": path, "change": "modified", "fields": changed})
    unknown = [row["path"] for row in after["services"] if
               "unreadable_or_unsupported_definition" in row["findings"] or
               "code_file_unreadable" in row["findings"] or "code_file_not_observed" in row["findings"]]
    return {"changes": changes, "unobserved": unknown, "errors": after.get("errors", [])}


def report(*, as_json: bool = False, baseline: Path | None = None) -> int:
    inventory = collect()
    if baseline is not None:
        inventory["comparison"] = compare(json.loads(baseline.read_text()), inventory)
    if as_json:
        print(json.dumps(inventory, indent=2))
    else:
        for row in inventory["services"]:
            print(f"{row.get('label', row['path'])}: "
                  f"{row.get('executable', 'unreadable')} "
                  f"[{row.get('launchd', {}).get('status', 'unobservable')}]")
            for finding in row["findings"]:
                print(f"  {finding}")
        for limitation in inventory["limits"]:
            print(f"Note: {limitation}")
        for error in inventory["errors"]:
            print(f"Unreadable: {error['path']} ({error['error']})")
        for change in inventory.get("comparison", {}).get("changes", []):
            print(f"Drift: {change['path']} ({change['change']})")
    return 1 if (inventory["errors"] or any(r["findings"] for r in inventory["services"]) or
                 inventory.get("comparison", {}).get("changes")) else 0
