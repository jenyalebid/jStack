"""Shell access on a managed machine — identity, keys, root steps, config.

The leaf-side primitives behind hub-held shell grants (#131): a per-machine
SSH identity whose private key never leaves the machine that minted it, a
marked block in `authorized_keys` this module owns outright — rewritten in
place on every grant change, gone when the list empties, the user's own keys
untouched — the one root step a grant needs (Remote Login) graded like
detach's step lists, and a marked `~/.ssh/config` block so a granted peer is
`ssh <name>` with nothing to set up. A grant carries no standing sudo: shell
is the enrolled account's own authority, root is asked for per use.

Every external edge is injectable (`runner`, `root`, `state`) the same way
attach_parent's and detach_parent's are, so all of it runs in tests against a
fake root with no root rights and nothing enabled for real.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import hostenv

MARK_BEGIN = "# >>> jremote managed keys >>>"
MARK_END = "# <<< jremote managed keys <<<"
CONFIG_BEGIN = "# >>> jremote managed hosts >>>"
CONFIG_END = "# <<< jremote managed hosts <<<"

#: Only ever removed. No grant writes it — shell access carries no standing
#: sudo — but disable keeps sweeping the drop-in earlier builds laid.
#: Relative to the machine root, so tests sweep under `root=tmp_path`.
SUDOERS_PATH = "etc/sudoers.d/jremote-managed"

KEY_FILE = "ssh/id_jremote"

#: What survives being an unquoted word in sudoers or an ssh_config Host
#: alias. Anything outside this set could smuggle a directive into a file
#: another parser reads as configuration.
_SAFE_WORD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

_RECORD = "shell_access.json"

SYSTEMSETUP = "/usr/sbin/systemsetup"


#: The exact shape of a bare authorized_keys line — type, blob, optional
#: comment word. No options prefix and no second line, because these lines are
#: written verbatim into OTHER machines' authorized_keys: an options field is
#: a command, a newline is a smuggled second key.
_PUBKEY = re.compile(
    r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-[a-z0-9-]+|sk-[a-z0-9.@-]+) "
    r"[A-Za-z0-9+/=]+( [A-Za-z0-9._:@+-]+)?$")


class ShellAccessError(Exception):
    """A grant input that must not reach a file other parsers trust."""


def _word(value: str, what: str) -> str:
    if not _SAFE_WORD.match(value or ""):
        raise ShellAccessError(f"{what} {value!r} cannot be written safely")
    return value


def valid_pubkey(line: str) -> bool:
    return bool(line) and len(line) <= 1024 and bool(_PUBKEY.match(line))


def valid_user(name: str) -> bool:
    return bool(_SAFE_WORD.match(name or ""))


# ── identity ────────────────────────────────────────────────────────────────

def identity(state: Path | None = None, *, keygen=None) -> str:
    """This machine's SSH public key, minting the keypair on first call.

    ed25519, comment = the machine's own host id, so every authorized_keys
    line this key lands in names exactly one leaf — revocation finds its line
    by that name. Re-calling returns the existing key; nothing ever rotates
    it implicitly, because the pubkey has already been handed to a parent.
    """
    state = state or hostenv.state_dir()
    key = state / KEY_FILE
    pub = key.with_suffix(".pub")
    if pub.is_file():
        return pub.read_text().strip()
    key.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(key.parent, 0o700)
    run = keygen or subprocess.run
    proc = run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "",
                "-C", hostenv.host_id(), "-f", str(key)],
               capture_output=True, text=True)
    if proc.returncode != 0 or not pub.is_file():
        raise ShellAccessError(
            "ssh-keygen could not mint this machine's identity: "
            + ((proc.stderr or proc.stdout or "").strip() or "no output"))
    os.chmod(key, 0o600)
    return pub.read_text().strip()


def public_key(state: Path | None = None) -> str:
    """The minted public key, or "" — never mints as a side effect."""
    pub = (state or hostenv.state_dir()) / KEY_FILE
    try:
        return pub.with_suffix(".pub").read_text().strip()
    except OSError:
        return ""


# ── the managed blocks ──────────────────────────────────────────────────────

def _splice(text: str, begin: str, end: str, block: list[str]) -> str:
    lines, kept, inside = text.splitlines(), [], False
    for line in lines:
        if line.strip() == begin:
            inside = True
            continue
        if line.strip() == end:
            inside = False
            continue
        if not inside:
            kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    if block:
        kept += ([""] if kept else []) + [begin] + block + [end]
    return ("\n".join(kept) + "\n") if kept else ""


def _write_marked(path: Path, begin: str, end: str, block: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    try:
        existing = path.read_text()
    except OSError:
        existing = ""
    path.write_text(_splice(existing, begin, end, block))
    os.chmod(path, 0o600)


def write_authorized_block(path: Path, lines: list[str]) -> None:
    """Make the managed block exactly `lines` — the whole grant list every
    time, never a delta, so a flip that removes a grant removes its key."""
    _write_marked(path, MARK_BEGIN, MARK_END, list(lines))


def read_authorized_block(path: Path) -> list[str]:
    try:
        text = path.read_text()
    except OSError:
        return []
    out, inside = [], False
    for line in text.splitlines():
        if line.strip() == MARK_BEGIN:
            inside = True
        elif line.strip() == MARK_END:
            inside = False
        elif inside:
            out.append(line)
    return out


def write_ssh_config(path: Path, peers: list[dict],
                     key_path: Path | None = None) -> None:
    """`ssh <name>` for every reachable peer — HostName, user, this machine's
    identity, keepalives tuned for a mesh that drops idle flows."""
    key_path = key_path or hostenv.state_dir() / KEY_FILE
    block: list[str] = []
    for peer in peers:
        block += [
            f"Host {_word(peer.get('name', ''), 'a peer name')}",
            f"  HostName {_word(peer.get('address', ''), 'a peer address')}",
            f"  User {_word(peer.get('user', ''), 'a peer user')}",
            f"  IdentityFile {key_path}",
            "  ServerAliveInterval 30",
            "  ServerAliveCountMax 4",
            "  StrictHostKeyChecking accept-new",
        ]
    _write_marked(path, CONFIG_BEGIN, CONFIG_END, block)


def root_step_spent(state: Path | None = None) -> bool:
    """Whether this machine has ever spent the one root step a grant needs.

    `enabled_remote_login` answers a narrower question — whether a grant
    *turned Remote Login on* — and is False on a Mac whose owner already ran
    SSH before any grant, so it cannot decide this by itself: recorded True
    and recorded False both mean inbound ssh works. What distinguishes a
    machine that never spent the step is that `enable` left no record at all,
    which is exactly a Mac adopted before shell access existed and presenting
    its identity late.
    """
    return _remote_login_record(state or hostenv.state_dir()) is not None


def apply_material(shell: dict, home: Path) -> list[dict]:
    """The user-writable half of a grant, graded: the authorized block and
    the peer config. Needs no root at all, which is what lets a live refresh
    run — and lets a Mac adopted before shell access existed become
    shell-capable on a poke instead of a re-adoption.

    The root half (Remote Login) is spent by `enable` at adoption. A machine
    presenting its identity later has not spent it and must not pretend
    otherwise, so an unspent step is appended here as a failed step the hub
    and the console can name to the user — never acquired silently, and never
    assumed done. A machine that did spend it emits nothing here, because
    `enable` reports that step where it is actually taken.
    """
    steps: list[dict] = []
    ssh_dir = Path(home) / ".ssh"
    authorized = list(shell.get("authorized") or [])
    try:
        write_authorized_block(ssh_dir / "authorized_keys", authorized)
        steps.append({"step": "authorized-keys", "ok": True,
                      "note": f"{len(authorized)} granted key(s) may log "
                              "in here"})
    except OSError as exc:
        steps.append({"step": "authorized-keys", "ok": False,
                      "note": f"could not write authorized_keys: {exc}"})
    peers = list(shell.get("peers") or [])
    try:
        write_ssh_config(ssh_dir / "config", peers)
        steps.append({"step": "ssh-config", "ok": True,
                      "note": (f"{len(peers)} peer(s) reachable by name"
                               if peers else "no peers granted yet")})
    except (OSError, ShellAccessError) as exc:
        steps.append({"step": "ssh-config", "ok": False,
                      "note": f"could not write the ssh config: {exc}"})
    if authorized and not root_step_spent():
        steps.append({
            "step": "remote-login", "ok": False,
            "note": "the granted keys are in place, but inbound ssh needs "
                    "Remote Login on and no grant on this Mac has ever turned "
                    "it on — turn on System Settings > General > Sharing > "
                    "Remote Login, or re-run the joiner, which asks for root "
                    "once and does it"})
    return steps


# ── the root step ───────────────────────────────────────────────────────────

def _defaults(runner, root, state):
    return (runner or subprocess.run,
            Path(root) if root else Path(os.environ.get("LEAF_DEST") or "/"),
            state or hostenv.state_dir())


def _record_path(state: Path) -> Path:
    return state / _RECORD


def enabled_remote_login(state: Path | None = None) -> bool:
    """Whether a grant turned Remote Login on — the fact disable restores."""
    try:
        rec = json.loads(_record_path(state or hostenv.state_dir()).read_text())
        return rec.get("remote_login_enabled") is True
    except (OSError, ValueError):
        return False


def _remote_login_record(state: Path) -> bool | None:
    try:
        rec = json.loads(_record_path(state).read_text())
        return bool(rec.get("remote_login_enabled"))
    except (OSError, ValueError):
        return None


def enable(*, runner=None, sudo: bool = True,
           state: Path | None = None) -> list[dict]:
    """The joiner's root moment: Remote Login on — and nothing else.

    Remote Login's prior state is probed first and recorded, because turning
    it on is only this grant's to undo if it was off before — a Mac whose
    owner already ran SSH keeps it on a later detach.
    """
    runner, _, state = _defaults(runner, None, state)
    prefix = ["sudo"] if sudo else []
    steps: list[dict] = []

    probe = runner(prefix + [SYSTEMSETUP, "-getremotelogin"],
                   capture_output=True, text=True)
    already_on = "On" in (probe.stdout or "")
    turned_on, ok = False, True
    if already_on:
        note = "Remote Login was already on — left as it was"
    else:
        proc = runner(prefix + [SYSTEMSETUP, "-setremotelogin", "on"],
                      capture_output=True, text=True)
        ok = proc.returncode == 0
        turned_on = ok
        note = ("Remote Login turned on" if ok else
                "systemsetup could not turn Remote Login on: "
                + ((proc.stderr or proc.stdout or "").strip() or "no output"))
    steps.append({"step": "remote-login", "ok": ok, "note": note})
    state.mkdir(parents=True, exist_ok=True)
    _record_path(state).write_text(
        json.dumps({"remote_login_enabled": turned_on}))
    return steps


def disable(*, runner=None, sudo: bool = True,
            root: Path | None = None, state: Path | None = None,
            authorized_keys: Path | None = None) -> list[dict]:
    """Enable's full reverse, graded per step like detach — a machine that
    still answers `sudo -n true` for a revoked parent is the failure mode."""
    runner, root, state = _defaults(runner, root, state)
    prefix = ["sudo"] if sudo else []
    steps: list[dict] = []

    record = _remote_login_record(state)
    if record:
        # -f, because systemsetup asks for confirmation on the way off.
        proc = runner(prefix + [SYSTEMSETUP, "-f", "-setremotelogin", "off"],
                      capture_output=True, text=True)
        ok = proc.returncode == 0
        steps.append({"step": "remote-login", "ok": ok,
                      "note": ("Remote Login restored to off" if ok else
                               "systemsetup could not turn Remote Login off — "
                               "turn it off by hand if it should be")})
    elif record is None:
        steps.append({"step": "remote-login", "ok": True,
                      "note": "no grant ever enabled it — left alone"})
    else:
        steps.append({"step": "remote-login", "ok": True,
                      "note": "it was on before the grant — left on"})

    target = root / SUDOERS_PATH
    ok, err = True, ""
    if target.exists():
        try:
            target.unlink()
        except OSError:
            proc = runner(prefix + ["/bin/rm", "-f", str(target)],
                          capture_output=True, text=True)
            ok, err = proc.returncode == 0, (proc.stderr or "").strip()
    steps.append({"step": "sudoers", "ok": ok,
                  "note": ("the sudoers drop-in is gone" if ok else
                           f"could not remove {target}: {err or 'no detail'}")})

    ak = authorized_keys or Path.home() / ".ssh" / "authorized_keys"
    try:
        text = ak.read_text()
    except OSError:
        text = ""
    try:
        # Only a file that carries the managed block is rewritten — a machine
        # that never had a grant keeps its authorized_keys byte-untouched.
        if MARK_BEGIN in text:
            write_authorized_block(ak, [])
            note = "no managed key can log in here any more"
        else:
            note = "no managed keys were present"
        steps.append({"step": "authorized-keys", "ok": True, "note": note})
    except OSError as exc:
        steps.append({"step": "authorized-keys", "ok": False,
                      "note": f"could not rewrite {ak}: {exc}"})

    try:
        shutil.rmtree((state / KEY_FILE).parent, ignore_errors=False)
        note = "this machine's shell identity is destroyed"
    except FileNotFoundError:
        note = "no shell identity was ever minted"
    except OSError as exc:
        steps.append({"step": "identity", "ok": False,
                      "note": f"could not remove the keypair: {exc}"})
    else:
        steps.append({"step": "identity", "ok": True, "note": note})

    try:
        _record_path(state).unlink()
    except OSError:
        pass
    return steps
