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

from . import app_services, hostenv, service_settings

WORK = Path("/private/var/db/live.jstack.sos")
LABEL = "live.jstack.sos"
PLIST = Path("/Library/LaunchDaemons/live.jstack.sos.plist")
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
    if str(path).startswith("/Users/") and not path.is_relative_to(home):
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
    root = hostenv.stack_root()
    result = {"schema": 1, "user": pwd.getpwuid(os.getuid()).pw_name,
              "uid": os.getuid(), "home": str(home), "hostname": socket.gethostname(),
              "root": str(root), "history": [], "data": [], "apps": [], "services": [],
              "app": config.get("app", "/Applications/jStack Hub.app")}

    def add(phase, path):
        value = str(safe_target(Path(path), home))
        if phase != "apps" and not (Path(value).is_relative_to(home) or
                                    (Path(value).is_relative_to("/Volumes") and len(Path(value).parts) >= 4)):
            raise ValueError(f"data location is outside the account or a configured volume: {value}")
        if value not in result[phase]:
            result[phase].append(value)

    for directory, variable in PROVIDERS.values():
        add("history", home / directory)
        if environment.get(variable):
            add("history", environment[variable])
    for relative in (".cache/jremote", ".local/state/jremote", ".local/share/jremote",
                     "Library/Containers/dev.jenya.jRemote",
                     "Library/Application Support/Claude", "Library/Application Support/Codex"):
        add("history", home / relative)
    for variable in ("JREMOTE_STATE_DIR", "JREMOTE_CACHE_DIR"):
        if environment.get(variable):
            add("history", environment[variable])
    for name in TREE_DIRS:
        add("data", root / name)
    add("data", hostenv.instance_root())
    for relative in (".claude.json", ".claude.json.backup", ".config/jstack", ".agents",
                     ".local/share/claude", ".local/bin/claude", ".local/bin/codex",
                     ".local/bin/jstack-host", "jStack", ".cache/claude", ".cache/codex",
                     "Library/Containers/dev.jenya.jRemote.Share",
                     "Library/Containers/dev.jenya.jRemote.tunnel",
                     "Library/Application Support/Jump Desktop", "Library/Application Support/Jump Desktop Connect",
                     "Library/Caches/com.anthropic.claudefordesktop", "Library/Caches/com.openai.codex"):
        add("data", home / relative)
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
    definitions = app / "Contents/Library/LaunchAgents"
    if definitions.is_dir():
        for path in definitions.glob("live.jstack.*.plist"):
            label = plistlib.loads(path.read_bytes()).get("Label")
            if isinstance(label, str) and re.fullmatch(r"live\.jstack\.[A-Za-z0-9_.-]+", label):
                result["services"].append({"domain": f"gui/{os.getuid()}", "label": label})
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
        target = service["domain"] + "/" + service["label"]
        lines += [f"/bin/launchctl disable {q(target)} || fail {q('disable: ' + target)}",
                  f"if /bin/launchctl print {q(target)} >/dev/null 2>&1; then /bin/launchctl bootout {q(target)} || fail {q('stop: ' + target)}; fi"]
    # Exact process names, never a match against prompt/command text.
    for name in ("claude", "codex", "Claude", "Codex", "jRemote"):
        lines += [f"/usr/bin/pkill -KILL -u {uid} -x {q(name)} 2>/dev/null || :",
                  f"if /usr/bin/pgrep -u {uid} -x {q(name)} >/dev/null; then fail {q('writer: ' + name)}; fi"]
    for target in plan["history"]:
        lines += [f"if test -d {q(target)} && test ! -L {q(target)}; then",
                  f"  /usr/sbin/lsof -a -u {uid} -t +D {q(target)} > writers 2> scan-errors; scan=$?",
                  '  if test -s scan-errors || test "$scan" -gt 1; then fail "cannot observe history writers"; fi',
                  '  while read -r writer; do case "$writer" in ""|*[!0-9]*) fail "invalid writer PID";; '
                  '*) /bin/kill -KILL "$writer" 2>/dev/null || :;; esac; done < writers',
                  "fi"]
    lines.append('[ "$failed" = 0 ] || exit 1')
    for phase in ("history", "data", "apps"):
        lines.append("phase " + phase)
        if phase == "data":
            for service in ("Claude Code-credentials", "Claude", "Codex Auth", "jRemote"):
                delete = shlex.join(["/usr/bin/sudo", "-n", "-H", "-u", "#" + str(uid),
                                     "/usr/bin/security", "delete-generic-password", "-s", service])
                lines += [f"while :; do {delete} >/dev/null 2> keychain-error; result=$?;",
                          'case "$result" in 0) ;; 44) break;; *) fail "keychain deletion denied"; break;; esac; done']
        if phase == "apps":
            for name in ("JumpConnect", "Jump Desktop Connect", "Jump Desktop", "JumpDesktop"):
                lines.append(f"/usr/bin/pkill -KILL -x {q(name)} 2>/dev/null || :")
        lines += ["remove " + q(path) for path in plan[phase]]
        lines.append('[ "$failed" = 0 ] || exit 1')
    # An install root is a container, never a recursive deletion target.
    lines += [f"/bin/rmdir {q(plan['root'])} 2>/dev/null || :",
              'phase verify', *[f'test ! -e {q(path)} && test ! -L {q(path)} || fail {q("remains: " + path)}'
                                for phase in ("history", "data", "apps") for path in plan[phase]],
              '[ "$failed" = 0 ] || exit 1', 'phase complete',
              f"/bin/rm -f {q(str(PLIST))} || exit 1", "cd /",
              f"{q(str(WORK / 'Erase'))} {q(str(WORK))} || exit 1",
              f"/bin/launchctl bootout system/{LABEL}"]
    return "\n".join(lines) + "\n"


def bootstrap(plan: dict) -> str:
    q = shlex.quote
    definition = {"Label": LABEL, "ProgramArguments": ["/bin/bash", str(WORK / "worker.sh")],
                  "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30}
    executable = Path(plan["app"]) / "Contents/MacOS/JStackErase"
    expected = hashlib.sha256(executable.read_bytes()).hexdigest()
    lines = ["set -eu", "umask 077", f"test ! -e {q(str(WORK))} && test ! -L {q(str(WORK))}",
             f"test ! -e {q(str(PLIST))} && test ! -L {q(str(PLIST))}",
             f"/bin/mkdir -m 700 {q(str(WORK))}",
             f"/usr/bin/install -o root -g wheel -m 700 {q(str(executable))} {q(str(WORK / 'Erase'))}",
             f'test "$(/usr/bin/shasum -a 256 {q(str(WORK / "Erase"))} | /usr/bin/cut -d " " -f 1)" = {q(expected)}']
    for path, data in ((WORK / "worker.sh", worker(plan).encode()),
                       (WORK / "manifest.json", json.dumps(plan).encode()),
                       (PLIST, plistlib.dumps(definition))):
        encoded = base64.b64encode(data).decode()
        lines += [f"printf %s {q(encoded)} | /usr/bin/base64 -D > {q(str(path))}",
                  f"/bin/chmod 600 {q(str(path))}"]
    lines += [f"/bin/chmod 644 {q(str(PLIST))}",
              f"/bin/launchctl bootstrap system {q(str(PLIST))}"]
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
        function.restype = ctypes.c_int
        return int(function())
    subprocess.run(["/usr/bin/sudo", "-v"], check=True)
    print(f"Accepted {action} on {socket.gethostname()}; the connection may close.", file=out, flush=True)
    if action == "wipe":
        return subprocess.run(["/usr/bin/sudo", "-n", "/bin/sh", "-c", bootstrap(plan)]).returncode
    return subprocess.run(["/usr/bin/sudo", "-n", "/sbin/shutdown",
                           "-r" if action == "reboot" else "-h", "now"]).returncode
