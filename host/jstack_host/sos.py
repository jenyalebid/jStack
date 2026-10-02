"""Confirmed machine actions and dependency-ordered local removal.

The wipe executor is a root-owned shell program using only macOS tools. It
does not import Python or execute anything from a tree it removes.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import shlex
import socket
import subprocess
import sys
import uuid

from . import app_services, fileshare, hostenv, network_admin, service_settings

WORK = Path("/private/var/db/live.jstack.sos")
LABEL = "live.jstack.sos"
PLIST = Path("/Library/LaunchDaemons/live.jstack.sos.plist")
NETWORK_APP = Path("/Library/PrivilegedHelperTools/jStack Network.app")
PROVIDERS = {"claude": (".claude", "CLAUDE_CONFIG_DIR"),
             "codex": (".codex", "CODEX_HOME")}
TREE_DIRS = ("Agents", "Projects", "Systems", "Config", "State", "Logs", "Credentials")


def safe_target(path: Path, home: Path) -> Path:
    """Do not turn configuration into permission to delete an enclosing tree."""
    path = Path(os.path.abspath(path.expanduser()))
    forbidden = {Path("/"), Path("/Users"), home, Path("/Applications"), Path("/System"),
                 Path("/usr"), Path("/bin"), Path("/sbin"), Path("/private"),
                 Path("/private/etc"), Path("/private/var"), Path("/private/var/db"),
                 Path("/Library"), home / "Library", home / ".local",
                 home / ".config", Path("/opt"), Path("/opt/homebrew"), Path("/usr/local")}
    if path in forbidden or path in home.parents or path == WORK or WORK in path.parents:
        raise ValueError(f"refusing enclosing directory: {path}")
    if str(path).startswith("/Users/") and not path.is_relative_to(home) and path != Path(fileshare.ACCOUNT_HOME):
        raise ValueError(f"refusing another account: {path}")
    if path.is_relative_to("/System") or path.is_relative_to("/bin") or path.is_relative_to("/sbin"):
        raise ValueError(f"refusing operating system path: {path}")
    if any(c in str(path) for c in "\n\r\x00"):
        raise ValueError("invalid removal path")
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError(f"removal parent is a symlink: {parent}")
    return path


def inventory() -> dict:
    home = Path.home()
    config = service_settings.read()
    environment = {**config.get("environment", {}), **os.environ}
    locations = (config.get("environment", {}),
                 config.get("scheduler", {}).get("environment", {}), os.environ)
    root = hostenv.stack_root()
    result = {"schema": 1, "user": pwd.getpwuid(os.getuid()).pw_name,
              "uid": os.getuid(), "home": str(home), "hostname": socket.gethostname(),
              "root": str(root), "history": [], "data": [], "apps": [], "services": [],
              "registrations": [], "network_transaction": config.get("network_transaction"),
              "tmux_socket": environment.get("JREMOTE_TMUX_SOCK", "jremote"),
              "shares": {},
              "app": config.get("app", "/Applications/jStack Hub.app")}

    def add(phase, path):
        value = str(safe_target(Path(path), home))
        if phase != "apps" and not (Path(value).is_relative_to(home) or
                                    (Path(value).is_relative_to("/Volumes") and len(Path(value).parts) >= 4)):
            raise ValueError(f"data location is outside the account or a configured volume: {value}")
        if value not in result[phase]:
            if phase == "history" and Path(value).is_symlink():
                raise ValueError(f"history root is a symlink; its contents cannot be accounted for: {value}")
            result[phase].append(value)

    for directory, variable in PROVIDERS.values():
        add("history", home / directory)
        for declared in locations:
            if declared.get(variable):
                add("history", declared[variable])
    for relative in (".cache/jremote", ".local/state/jremote", ".local/share/jremote",
                     "Library/Containers/dev.jenya.jRemote",
                     "Library/Application Support/Claude", "Library/Application Support/Codex"):
        add("history", home / relative)
    # The installed services and the invoking shell can name different
    # locations; neither declaration makes the other one's history disappear.
    for declared in locations:
        for variable in ("JREMOTE_STATE_DIR", "JREMOTE_CACHE_DIR", "JREMOTE_ATTENTION_DIR",
                         "JREMOTE_TURN_DIR", "JSTACK_LOGS_DIR", "JSTACK_TIMELINE_DIR",
                         "JSTACK_STATE_DIR", "SCHEDULER_HOME", "SCHEDULER_STATE_DIR",
                         "JSTACK_AGENTS_DIR", "JREMOTE_INSTANCE_ROOT"):
            if declared.get(variable):
                add("history", declared[variable])
        for variable in ("JREMOTE_CREDENTIALS_DIR", "JREMOTE_RELEASES_DIR", "JREMOTE_TOKEN_PATH",
                         "JSTACK_CONFIG_DIR", "JSTACK_CREDENTIALS_DIR", "JSTACK_SYSTEMS_DIR",
                         "SCHEDULER_CONFIG_DIR", "WG_PEER_DIR"):
            if declared.get(variable):
                add("data", declared[variable])
    for key in ("migration_dir", "automation_settings"):
        if config.get(key):
            add("history" if key == "migration_dir" else "data", config[key])
    add("history", home / ".scheduler")
    # Agent and project trees can hold transcript exports under arbitrary names.
    # They are already selected for removal; searching filenames cannot prove
    # that all those copies disappeared before the data phase.
    for name in TREE_DIRS:
        add("history" if name in {"Agents", "Projects", "Logs", "State"} else "data", root / name)
    add("history", hostenv.instance_root())
    for relative in (".claude.json", ".claude.json.backup", ".config/jstack", ".agents",
                     ".local/share/claude", ".local/bin/claude", ".local/bin/codex",
                     ".local/bin/jstack-host", "jStack", "jRemote-Code",
                     "Library/Containers/dev.jenya.jRemote.Share",
                     "Library/Containers/dev.jenya.jRemote.tunnel",
                     "Library/Application Support/Jump Desktop", "Library/Application Support/Jump Desktop Connect"):
        add("data", home / relative)
    for name in ("jStack", "jRemote-Code"):
        add("data", root / name)
    for relative in (".config/claude", ".config/codex", ".config/jremote", ".local/share/jstack",
                     ".cache/claude", ".cache/codex",
                     "Library/Caches/com.anthropic.claudefordesktop", "Library/Caches/com.openai.codex",
                     "Library/Logs/Claude", "Library/Logs/Codex", "Library/Logs/jRemote",
                     "Library/Logs/jStack", "Library/Saved Application State/com.anthropic.claudefordesktop.savedState",
                     "Library/Saved Application State/com.openai.codex.savedState"):
        add("history", home / relative)
    for name in ("jStack Hub.app", "jRemote.app", "Claude.app", "Codex.app",
                 "Jump Desktop.app", "Jump Desktop Connect.app"):
        for base in (Path("/Applications"), home / "Applications"):
            add("apps", base / name)
    if config.get("app"):
        add("apps", config["app"])
    for path in ("/Library/PrivilegedHelperTools/jStack Network.app",
                 "/Library/PrivilegedHelperTools/.jstack-network",
                 "/Library/Application Support/jRemote Leaf", "/Library/Application Support/Jump Desktop Connect",
                 "/Library/Logs/Jump Desktop", "/Library/Audio/Plug-Ins/HAL/JumpAudio.driver",
                 "/Library/Audio/Plug-Ins/HAL/JumpAudioMic.driver",
                 "/etc/codex", "/etc/wireguard/jrleaf.conf", "/etc/sudoers.d/jremote-managed"):
        # /etc is macOS's own symlink; retain the canonical system location.
        add("apps", Path(path).resolve())
    for prefix in (Path("/opt/homebrew"), Path("/usr/local")):
        for relative in ("bin/claude", "bin/codex", "Caskroom/codex", "Caskroom/claude-code",
                         "lib/node_modules/@anthropic-ai/claude-code", "lib/node_modules/@openai/codex"):
            add("apps", prefix / relative)
    for directory, domain in ((home / "Library/LaunchAgents", f"gui/{os.getuid()}"),
                              (Path("/Library/LaunchAgents"), f"gui/{os.getuid()}"),
                              (Path("/Library/LaunchDaemons"), "system")):
        if not directory.exists():
            continue
        for path in directory.glob("*.plist"):
            if path.name.startswith(("live.jstack.", "com.jstack.", "com.jremote.", "com.p5sys.jump")):
                value = plistlib.loads(path.read_bytes())
                label = value.get("Label", "")
                if label == LABEL or not re.fullmatch(r"[A-Za-z0-9_.-]+", label):
                    continue
                result["services"].append({"domain": domain, "label": label})
                add("apps", path)
    app = Path(config.get("app", "/Applications/jStack Hub.app"))
    for target in result["history"] + result["data"]:
        if app == Path(target) or app.is_relative_to(target):
            raise ValueError(f"removal target contains the required Hub executor: {target}")
    definitions = app / "Contents/Library/LaunchAgents"
    if definitions.is_dir():
        catalog = json.loads((app / "Contents/Resources/services.json").read_text())
        for role in catalog:
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", role):
                raise ValueError("invalid sealed service role")
            result["registrations"].append(role)
        for path in definitions.glob("live.jstack.*.plist"):
            label = plistlib.loads(path.read_bytes()).get("Label")
            if isinstance(label, str) and re.fullmatch(r"live\.jstack\.[A-Za-z0-9_.-]+", label):
                result["services"].append({"domain": f"gui/{os.getuid()}", "label": label})
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", result["tmux_socket"]):
        raise ValueError("invalid managed tmux socket")
    if NETWORK_APP.exists():
        if not re.fullmatch(r"[a-f0-9]{32}", str(result["network_transaction"])):
            raise ValueError("installed Network service has no reviewed uninstall transaction")
        result["network_invocation"] = uuid.uuid4().hex
    actual = fileshare._actual_shares()
    desired = fileshare.desired_shares()
    for name, path in desired.items():
        if name in actual and Path(actual[name]["path"]) == path:
            result["shares"][name] = str(path)
    account = fileshare._account()
    if account["exists"]:
        if account["home"] != fileshare.ACCOUNT_HOME or account["shell"] != fileshare.ACCOUNT_SHELL or account["admin"] is not False:
            raise ValueError("sharing account ownership cannot be established")
        result["share_account"] = True
        add("apps", fileshare.ACCOUNT_HOME)
    return result


def confirm(action: str, *, out=sys.stdout) -> bool:
    if not sys.stdin.isatty():
        raise ValueError("SOS commands require an interactive terminal")
    phrase = f"{action.upper()} {socket.gethostname()}"
    print(f"Target: {socket.gethostname()} · {pwd.getpwuid(os.getuid()).pw_name}", file=out)
    try:
        answer = input(f"Type {phrase} to confirm: ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer == phrase


def worker(plan: dict) -> str:
    q = shlex.quote
    home, uid = plan["home"], plan["uid"]
    lines = ["#!/bin/bash", "set -u", "umask 077", "export PATH=/usr/bin:/bin:/usr/sbin:/sbin",
             f"cd {q(str(WORK))} || exit 1", "exec >> progress.log 2>&1", "failed=0",
             "fail() { printf 'INCOMPLETE: %s\\n' \"$*\"; failed=1; }",
             # Recheck ancestry immediately before each removal; rm does not
             # follow symlinks within the target directory.
             'remove() { local p="$1" a; a="${p%/*}"; while [ -n "$a" ]; do '
             '[ ! -L "$a" ] || { fail "symlink parent: $a"; return; }; '
             '[ "$a" != / ] || break; a="${a%/*}"; done; '
             f'{q(str(WORK / "Erase"))} "$p" || fail "delete: $p"; '
             'if [ -e "$p" ] || [ -L "$p" ]; then fail "remains: $p"; fi; }',
             'phase() { printf "%s\\n" "$1" > phase; printf "PHASE %s\\n" "$1"; }',
             'phase quiesce']
    for service in plan["services"]:
        # Network and remote-desktop access are retained until history is gone.
        if late_service(service["label"]):
            continue
        target = service["domain"] + "/" + service["label"]
        lines += [f"/bin/launchctl disable {q(target)} || fail {q('disable: ' + target)}",
                  f"if /bin/launchctl print {q(target)} >/dev/null 2>&1; then /bin/launchctl bootout {q(target)} || fail {q('stop: ' + target)}; fi"]
    # Exact process names, never a match against prompt/command text.
    for name in ("claude", "codex", "Claude", "Codex", "jRemote"):
        lines += [f"/usr/bin/pkill -KILL -u {uid} -x {q(name)} 2>/dev/null || :",
                  f"if /usr/bin/pgrep -u {uid} -x {q(name)} >/dev/null; then fail {q('writer: ' + name)}; fi"]
    app = Path(plan.get("app", "/Applications/jStack Hub.app"))
    # sudo -H changes HOME, not cwd. The worker's 0700 directory is unreadable
    # to this user; CLI audit metadata calls getcwd before cleanup can begin.
    user = ["/usr/bin/sudo", "-n", "-H", "-u", "#" + str(uid),
            "/bin/sh", "-c", 'cd / && exec "$@"', "sos-user"]
    tmux = app / "Contents/MacOS/tmux"
    lines += [f"if test -x {q(str(tmux))}; then",
              shlex.join(user + [str(tmux), "-L", plan.get("tmux_socket", "jremote"), "kill-server"]) + " 2>/dev/null || :",
              "fi"]
    for target in plan["history"]:
        lines += [f"if test -L {q(target)}; then fail 'history root became a symlink'; fi",
                  f"if test -d {q(target)} && test ! -L {q(target)}; then",
                  f"  /usr/sbin/lsof -a -u {uid} -t +D {q(target)} > writers 2> scan-errors; scan=$?",
                  '  if test -s scan-errors || test "$scan" -gt 1; then fail "cannot observe history writers"; fi',
                  # Unknown writers are not permission to kill an unrelated app.
                  '  if test -s writers; then fail "history still has open writers"; fi',
                  "fi"]
    lines.append('[ "$failed" = 0 ] || exit 1')
    for phase in ("history", "data", "apps"):
        lines.append("phase " + phase)
        if phase == "data":
            for name, path in plan.get("shares", {}).items():
                node = "/SharePoints/" + name
                lines += [f"if /usr/bin/dscl . -read {q(node)} >/dev/null 2>&1; then",
                          f'test "$(/usr/bin/dscl . -read {q(node)} directory_path)" = {q("directory_path: " + path)} || exit 1',
                          f"/usr/sbin/sharing -r {q(name)} || exit 1", "fi"]
            if plan.get("share_account"):
                node = "/Users/" + fileshare.ACCOUNT
                lines += [f"if /usr/bin/dscl . -read {q(node)} >/dev/null 2>&1; then",
                          f'test "$(/usr/bin/dscl . -read {q(node)} NFSHomeDirectory)" = {q("NFSHomeDirectory: " + fileshare.ACCOUNT_HOME)} || exit 1',
                          f'test "$(/usr/bin/dscl . -read {q(node)} UserShell)" = {q("UserShell: " + fileshare.ACCOUNT_SHELL)} || exit 1',
                          f"/usr/bin/dscl . -delete {q(node)} || exit 1", "fi"]
            lines += ['if test ! -f user-cleanup-done; then',
                      shlex.join(user + [str(app / "Contents/MacOS/JStackCLI"), "_wipe-user-cleanup"])
                      + ' || { fail "user cleanup"; exit 1; }',
                      'touch user-cleanup-done', 'fi']
            for service in ("Claude Code-credentials", "Claude", "Codex Auth", "jRemote"):
                delete = shlex.join(user + ["/usr/bin/security", "delete-generic-password", "-s", service])
                lines += [f"while :; do {delete} >/dev/null 2> keychain-error; result=$?;",
                          'case "$result" in 0) ;; 44) break;; *) fail "keychain deletion denied"; break;; esac; done']
        if phase == "apps":
            lines += ['if test ! -f registrations-done; then']
            for role in plan.get("registrations", []):
                lines += [shlex.join(user + [str(app / "Contents/MacOS/JStackHub"), "unregister", role])
                          + " || fail 'user service unregister' "]
            lines += ['[ "$failed" = 0 ] || exit 1', 'touch registrations-done', 'fi']
            if plan.get("network_invocation"):
                invocation = network_admin.ROOT / "invocations" / plan["network_invocation"]
                lines += ['if test ! -f network-done; then',
                          shlex.join([str(invocation / "Installer"), str(invocation / "request.json")]) + " || exit 1",
                          'touch network-done', 'fi']
            for service in plan["services"]:
                target = service["domain"] + "/" + service["label"]
                if late_service(service["label"]):
                    lines += [f"/bin/launchctl disable {q(target)} || fail {q('disable: ' + target)}",
                              f"if /bin/launchctl print {q(target)} >/dev/null 2>&1; then /bin/launchctl bootout {q(target)} || fail {q('stop: ' + target)}; fi"]
            for name in ("JumpConnect", "Jump Desktop Connect", "Jump Desktop", "JumpDesktop"):
                lines.append(f"/usr/bin/pkill -KILL -x {q(name)} 2>/dev/null || :")
            lines.append('[ "$failed" = 0 ] || exit 1')
        lines += ["remove " + q(path) for path in plan[phase]]
        lines.append('[ "$failed" = 0 ] || exit 1')
    # An install root is a container, never a recursive deletion target.
    lines += [f"/bin/rmdir {q(plan['root'])} 2>/dev/null || :",
              'phase verify', *[f'test ! -e {q(path)} && test ! -L {q(path)} || fail {q("remains: " + path)}'
                                for phase in ("history", "data", "apps") for path in plan[phase]],
              '[ "$failed" = 0 ] || exit 1', 'phase complete',
              f"/usr/bin/plutil -replace SOSComplete -bool YES {q(str(PLIST))} || exit 1"]
    return "\n".join(lines) + "\n"


def late_service(label: str) -> bool:
    return label in {"live.jstack.network", "com.jremote.hub", "com.jremote.hub-sync",
                     "com.jremote.leaf", "com.jremote.leaf-watch"} or label.startswith("com.p5sys.jump")


def supervisor() -> str:
    q = shlex.quote
    # launchd retains this script in memory, and reloads it from its plist on
    # reboot. Completion lives outside WORK so interrupted self-removal cannot
    # destroy the only code or evidence needed to finish cleaning up.
    return "\n".join([
        "set -eu", "export PATH=/usr/bin:/bin:/usr/sbin:/sbin", "cd /",
        f"if test ! -e {q(str(PLIST))} && test ! -e {q(str(WORK))}; then",
        f"  /bin/launchctl bootout system/{LABEL}; exit 0", "fi",
        f"complete=$(/usr/bin/plutil -extract SOSComplete raw {q(str(PLIST))})",
        'if test "$complete" != true; then',
        f"  /bin/bash {q(str(WORK / 'worker.sh'))}",
        f"  complete=$(/usr/bin/plutil -extract SOSComplete raw {q(str(PLIST))})", "fi",
        'test "$complete" = true',
        # Only the fixed, root-owned executor directory is removed here. All
        # user data goes through the separately approved native inventory.
        f"/bin/rm -rf {q(str(WORK))}",
        f"/bin/rm -f {q(str(PLIST))}",
        f"/bin/launchctl bootout system/{LABEL}",
    ]) + "\n"


def bootstrap(plan: dict) -> str:
    q = shlex.quote
    definition = {"Label": LABEL, "ProgramArguments": ["/bin/sh", "-c", supervisor()],
                  "SOSComplete": False, "RunAtLoad": True,
                  "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30}
    executable = Path(plan["app"]) / "Contents/MacOS/JStackErase"
    expected = hashlib.sha256(executable.read_bytes()).hexdigest()
    network_admin.protected_ancestry(WORK.parent)
    network_admin.protected_ancestry(PLIST.parent)
    lines = ["set -eu", "umask 077", "export PATH=/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C",
             "check_parent() { test ! -L \"$1\"; test \"$(/usr/bin/stat -f %u \"$1\")\" = 0; "
             "test $(( 0$(/usr/bin/stat -f %Lp \"$1\") & 022 )) = 0; "
             "/bin/ls -lde \"$1\" | /usr/bin/awk 'NR > 1 && / allow / { exit 1 }'; }",
             *["check_parent " + q(str(path)) for path in sorted(set((*WORK.parent.parents, WORK.parent,
                                                                 *PLIST.parent.parents, PLIST.parent)))],
             f"test ! -e {q(str(WORK))} && test ! -L {q(str(WORK))}",
             f"test ! -e {q(str(PLIST))} && test ! -L {q(str(PLIST))}",
             f"/bin/mkdir -m 700 {q(str(WORK))}",
             "launched=0; plist_created=0; invocation_created=0",
             "rollback() { result=$?; trap - EXIT; "
             f"if test \"$launched\" = 0 && ! /bin/launchctl print system/{LABEL} >/dev/null 2>&1; then "
             f"if test \"$plist_created\" = 1; then /bin/rm -f {q(str(PLIST))}; fi; "
             f"/bin/rm -rf {q(str(WORK))}; "
             + (f"if test \"$invocation_created\" = 1; then /bin/rm -rf {q(str(network_admin.ROOT / 'invocations' / plan['network_invocation']))}; fi; "
                if plan.get("network_invocation") else "")
             + "fi; exit \"$result\"; }",
             "trap rollback EXIT", "trap 'exit 1' HUP INT TERM",
             f"/usr/bin/install -o root -g wheel -m 700 {q(str(executable))} {q(str(WORK / 'Erase'))}",
             f'test "$(/usr/bin/shasum -a 256 {q(str(WORK / "Erase"))} | /usr/bin/cut -d " " -f 1)" = {q(expected)}']
    if plan.get("network_invocation"):
        network_admin.protected_ancestry(network_admin.ROOT)
        app_services.verify(NETWORK_APP, "live.jstack.network")
        source = NETWORK_APP / "Contents/MacOS/JStackNetworkInstaller"
        invocation = network_admin.ROOT / "invocations" / plan["network_invocation"]
        request = {"schema": 1, "action": "uninstall", "transaction": plan["network_transaction"]}
        for directory in (network_admin.ROOT, network_admin.ROOT / "invocations"):
            lines += [f"if test ! -e {q(str(directory))}; then /bin/mkdir -m 700 {q(str(directory))}; fi",
                      "check_parent " + q(str(directory))]
        lines += [f"/bin/mkdir -m 700 {q(str(invocation))}",
                  "invocation_created=1",
                  f"/usr/bin/install -o root -g wheel -m 700 {q(str(source))} {q(str(invocation / 'Installer'))}",
                  f'test "$(/usr/bin/shasum -a 256 {q(str(invocation / "Installer"))} | /usr/bin/cut -d " " -f 1)" = {q(hashlib.sha256(source.read_bytes()).hexdigest())}',
                  f"printf %s {q(base64.b64encode(json.dumps(request).encode()).decode())} | /usr/bin/base64 -D > {q(str(invocation / 'request.json'))}"]
    for path, data in ((WORK / "worker.sh", worker(plan).encode()),
                       (WORK / "manifest.json", json.dumps(plan).encode()),
                       (WORK / "launchd.plist", plistlib.dumps(definition))):
        encoded = base64.b64encode(data).decode()
        lines += [f"printf %s {q(encoded)} | /usr/bin/base64 -D > {q(str(path))}",
                  f"/bin/chmod 600 {q(str(path))}"]
    lines += ["plist_created=1",
              f"/usr/bin/install -o root -g wheel -m 644 {q(str(WORK / 'launchd.plist'))} {q(str(PLIST))}",
              f"/bin/launchctl bootstrap system {q(str(PLIST))}",
              "launched=1", "trap - EXIT HUP INT TERM"]
    return "\n".join(lines)


def run(action: str, *, dry_run=False, out=sys.stdout) -> int:
    if sys.platform != "darwin" or os.geteuid() == 0:
        raise ValueError("run SOS commands as the Mac login user")
    if action not in {"reboot", "shutdown", "lock", "wipe"}:
        raise ValueError("unknown SOS command")
    if action == "wipe":
        plan = inventory()
        print(json.dumps(plan, indent=2), file=out)
        if dry_run:
            return 0
        app_services.verify(Path(plan["app"]))
        if not (Path(plan["app"]) / "Contents/MacOS/JStackErase").is_file():
            raise ValueError("installed Hub does not contain the SOS executor")
    if not confirm(action, out=out):
        print("Cancelled; no action taken.", file=out)
        return 1
    if action == "lock":
        import ctypes
        library = ctypes.CDLL("/System/Library/PrivateFrameworks/login.framework/Versions/Current/login")
        function = library.SACLockScreenImmediate
        function.argtypes = []
        function.restype = None
        function()
        return 0
    subprocess.run(["/usr/bin/sudo", "-v"], check=True)
    print(f"Accepted {action} on {socket.gethostname()}; the connection may close.", file=out, flush=True)
    if action == "wipe":
        return subprocess.run(["/usr/bin/sudo", "-n", "/bin/sh", "-c", bootstrap(plan)]).returncode
    return subprocess.run(["/usr/bin/sudo", "-n", "/sbin/shutdown",
                           "-r" if action == "reboot" else "-h", "now"]).returncode
