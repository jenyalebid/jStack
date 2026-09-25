"""`jstack-host attach` — join another Mac's mesh, on purpose, in one command.

Managed mode is the third shape a host can be (mode.py): a Mac that dials OUT
to a parent hub and rides its mesh, so every device already paired to that
parent reaches this machine with no per-device setup of its own. That is the
whole product promise of a managed hub, and until now it had no front door —
the pieces existed on both ends (the parent mints a host code, `install_leaf.sh`
stands up the tunnel) but nothing on the joining machine tied them together.
A person had to redeem the code by hand, find the leaf bundle in the response,
write six files out with the right permissions, and run the installer from the
right directory. Every one of those steps was a place to get it wrong.

This is that sequence, made one deliberate action:

  1. redeem a **host-kind** enrolment code against the parent's public
     `POST /api/jremote/v1/enrolment/redeem` — the same unauthenticated
     path a leaf uses, carrying this machine's own `/host` id as `host_key`
     so the parent can record which machine just joined;
  2. take the leaf bundle the parent hands back (`response["tunnel"]["bundle"]`,
     the six `tunnel.LEAF_FILES`), write it out, and
  3. run `install_leaf.sh` from inside it — which installs the leaf daemons and
     brings the tunnel up — unless that exact tunnel is already installed and
     shaking hands, in which case the installer is left alone (jStack#127).

The skip in step 3 exists because attach is not always the first thing to bring
the tunnel up. The offline joiner (`adopt_offline.py`) installs the carried
bundle FIRST, so the far Mac has a route to the hub, and redeems second; the
redeem then hands back the same bundle, and running the installer again boots
the live daemon out and back in for nothing. That restart is not free: the
installer reads the utun name the old daemon left in `/var/run/wireguard`
before the new one has replaced it, waits twenty seconds for a handshake on an
interface that no longer exists, and reports "the leaf installer failed" about
a tunnel the hub was receiving heartbeats from the whole time. So when the
installed conf and env are byte-identical to what the parent just handed back
and the tunnel has a handshake under three minutes old, there is nothing to
install and attach says so instead.

Deliberate, never inferred. Attaching to a parent changes what off-network
devices can reach; it is a thing a person chooses and types a code for, not a
mode a Mac drifts into. So it is a command, and it reports the mode it left the
machine in rather than claiming success it did not check.

Why the bundle, not a client `.conf`: a machine joining the mesh for good runs
a LaunchDaemon that dials out, where a device toggles a VPN. The two artefacts
come off the same peer entry and are NOT interchangeable — `jrleaf.conf` is
`wg setconf`-style and deliberately carries no `Address` line. Redeeming a
*device* code here would hand back the wrong one, so a device code is refused
before anything is written (`kind` is fixed at mint and cannot be re-declared).

Re-attach is not a second machine. If this Mac was attached before, the token
it was given then is presented alongside the new code, so the parent re-keys the
one device row it already holds for this machine instead of leaving a second one
behind — the same RE-PAIRING rule enrolment.py enforces for a phone re-running
the installer.

Every external edge is injectable — the HTTP POST (`poster`), the installer
invocation (`runner`), the bundle destination (`dest_dir`) — so the whole
sequence is driven in tests against a recorder and the real `install_leaf.sh`
under `LEAF_DEST`, with nothing brought up and no root required.
"""

from __future__ import annotations

import ipaddress
import json
import os
import stat
import subprocess
from pathlib import Path

from . import addresses, hostenv, tunnel
from .enrolment import HOST_KEY_RE

#: The parent route this redeems against — the unauthenticated leaf/host
#: redemption path, mounted at the package's one API prefix.
REDEEM_PATH = "/api/jremote/v1/enrolment/redeem"

#: The port a host serves on when nobody says otherwise. Same number as
#: enrolment.DEFAULT_PORT and install_host.DEFAULT_PORT — the CLI passes the
#: real one through, so this only stands in for a caller that omitted it.
DEFAULT_PORT = 9090

#: Where this machine records the parent it attached to. Not the mesh — that
#: lives in the leaf daemons — but the device token the parent handed back,
#: which is the only copy there will ever be and is what a re-attach presents so
#: the parent re-keys rather than minting a second row. In the state dir, 0600,
#: beside the bearer token, because it is one.
PARENT_RECORD = "parent.json"

#: Per-file permissions for the bundle written to disk. `jrleaf.conf` carries a
#: private key; the shell scripts are executed. `install_leaf.sh` re-installs
#: everything under its own modes anyway, but it reads these first, and a
#: world-readable private key on the way in is still a leaked key.
_MODE = {
    "jrleaf.conf": 0o600,
    "leaf.env": 0o644,
    "install_leaf.sh": 0o755,
    "wg_up.sh": 0o755,
    "wg_leaf_watch.sh": 0o755,
    "README.md": 0o644,
}


def _refuse_a_parent_only_the_tunnel_can_reach(parent_url: str) -> None:
    """Refuse a parent address that lives inside the mesh this attach creates.

    Attaching is what puts this machine on the parent's mesh: it redeems a code
    over the ordinary network, receives a leaf bundle, and only then brings a
    tunnel up. So a `--parent` inside `MESH_SUBNET` is circular on a machine
    that is not already a peer — the address becomes reachable as a *result* of
    the thing that cannot start without it.

    Unchecked, that circularity spent thirty seconds in `httpx` and came back as
    `could not reach the parent at http://10.66.0.1:9090/...: timed out`, which
    reads as a hub that is down. It is not down; it was never addressable from
    here. Someone acting on that message goes and restarts a healthy hub.

    Same defect as the one fixed for phones in jRemote `ceb527a` — a device
    pinned to an address only the tunnel has. That fix never reached this path.

    Not refused when this machine already holds a mesh address: re-attaching an
    existing leaf, or moving it to a new parent, legitimately talks in-tunnel.
    """
    from urllib.parse import urlsplit

    host = (urlsplit(parent_url).hostname or "").strip()
    try:
        parent_ip = ipaddress.ip_address(host)
    except ValueError:
        return  # a name, not an address — DNS decides, and it may well resolve
    if parent_ip not in addresses.MESH_SUBNET:
        return
    if any(_in_mesh(a) for a in addresses._inet_addrs()):
        return  # already a peer; in-tunnel is a legitimate way to talk
    raise AttachError(
        f"{host} is a mesh address, and this machine is not on the mesh yet — "
        "attaching is what puts it there, so that address cannot answer until "
        "after this has already succeeded.\n\n"
        "If this Mac is on the parent's network, use its LAN name or address "
        "instead: e.g. http://studio.local:9090 (`jstack-host status` on the "
        "parent prints what it publishes).\n\n"
        "If it is NOT — and that is the case this refusal usually means — no "
        "address works from here, because a hub publishes no public HTTP. On "
        "the parent, run:\n\n"
        "    jstack-host adopt <name-for-this-mac> --offline\n\n"
        "and carry the folder it names to this Mac. Running `./join.sh` in it "
        "brings the tunnel up first and redeems second, which is the only "
        "order that can work.")


def _in_mesh(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr) in addresses.MESH_SUBNET
    except ValueError:
        return False


class AttachError(Exception):
    """Attaching to the parent could not be completed. Carries a message meant
    to be printed to the person who ran the command."""


def _httpx_post(url: str, payload: dict) -> tuple[int, dict]:
    """POST `payload` as JSON, return (status, decoded body).

    httpx, the same client apns.py and spawn.py already depend on. A body that
    is not JSON comes back as {} — every status this cares about (200 success,
    FastAPI's {"detail": …} errors) is JSON, and a non-JSON 500 is handled by
    the caller as "no detail" rather than a decode crash.
    """
    import httpx
    try:
        resp = httpx.post(url, json=payload, timeout=30.0)
    except httpx.HTTPError as exc:
        raise AttachError(f"could not reach the parent at {url}: {exc}")
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, body if isinstance(body, dict) else {}


def _prior_token(state: Path) -> str:
    """The device token this machine was given last time it attached, or "".

    Presented as `device_token` so a re-attach re-keys the existing row on the
    parent instead of adding a second one. Absent, unreadable or malformed all
    fall through to "" — a plain first-time attach — because by the time this is
    read the only cost of getting it wrong is one extra device row, which the
    parent already treats as the normal case.
    """
    try:
        rec = json.loads((state / PARENT_RECORD).read_text())
        return str(rec.get("token") or "")
    except (OSError, ValueError):
        return ""


#: The probe behind the step-3 skip, run as root on a real machine (the conf
#: is 0600 root and so is the utun name file). Exit 0 with `<iface> <age>` on
#: stdout when the bundle at $1/$2 is the tunnel already installed and carrying;
#: 3 when the installed files differ (or are absent); 4 when they match but the
#: tunnel is not shaking hands, which is the case a re-install is FOR. Arguments
#: rather than environment, because `sudo` drops the environment. $3 is the
#: install root — empty on a real machine, `LEAF_DEST` under test, the same
#: seam `install_leaf.sh` documents — and $4 a `wg` to prefer.
_LEAF_PROBE = r"""
set -u
conf="$1"; env_="$2"; root="${3:-}"; wg_pref="${4:-}"
cmp -s "$conf" "$root/etc/wireguard/jrleaf.conf" || exit 3
cmp -s "$env_" "$root/Library/Application Support/jRemote Leaf/leaf.env" || exit 3
iface="$(cat "$root/var/run/wireguard/jremote-wg.name" 2>/dev/null || true)"
[ -n "$iface" ] || exit 4
wg=""
for cand in "$wg_pref" "$(command -v wg 2>/dev/null || true)" /opt/homebrew/bin/wg /usr/local/bin/wg; do
    if [ -n "$cand" ] && [ -x "$cand" ]; then wg="$cand"; break; fi
done
[ -n "$wg" ] || exit 4
hs="$("$wg" show "$iface" latest-handshakes 2>/dev/null | awk 'NR==1 {print $2}' || true)"
case "$hs" in ''|*[!0-9]*) exit 4;; esac
[ "$hs" -gt 0 ] || exit 4
age=$(( $(date +%s) - hs ))
[ "$age" -le 180 ] || exit 4
echo "$iface $age"
"""


def _tunnel_already_up(dest: Path, *, sudo: bool, env: dict, prober) -> str | None:
    """The note to report instead of running the installer — or None, run it.

    Byte-identical installed files AND a handshake under three minutes old. The
    second half is what keeps this from being a way to skip a repair: a Mac
    whose conf matches but whose tunnel is dead gets the installer, restart and
    all, exactly as before. A probe that cannot run at all (no bash, no sudo
    right) reads as "not up" for the same reason — the installer is the
    conservative answer.
    """
    root = env.get("LEAF_DEST", "")
    argv = (["sudo"] if sudo and not root else []) + [
        "bash", "-c", _LEAF_PROBE, "leaf-probe",
        str(dest / "jrleaf.conf"), str(dest / "leaf.env"), root, env.get("WG", "")]
    try:
        proc = prober(argv, cwd=str(dest), env=env, capture_output=True, text=True)
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    iface, _, age = (proc.stdout or "").strip().partition(" ")
    if not iface:
        return None
    return (f"tunnel already up on {iface} (handshake {age}s ago) with this "
            "exact bundle — installer not re-run")


def _write_bundle(bundle: dict, dest: Path) -> Path:
    """Write the six leaf files to `dest`, each with the mode its job needs.

    Refuses a bundle that is missing any of `tunnel.LEAF_FILES` before writing a
    single byte — a partial bundle installs a tunnel that cannot come up, and it
    should fail here with the missing name, not later inside `install_leaf.sh`.
    """
    missing = [n for n in tunnel.LEAF_FILES if n not in bundle]
    if missing:
        raise AttachError(
            "the parent's leaf bundle is missing " + ", ".join(missing)
            + " — it would install a tunnel that cannot start")
    dest.mkdir(parents=True, exist_ok=True)
    for name in tunnel.LEAF_FILES:
        path = dest / name
        path.write_text(bundle[name])
        os.chmod(path, _MODE.get(name, 0o644))
    return dest


def _record_parent(state: Path, parent_url: str, result: dict) -> None:
    """Persist what this attach learned about the parent, so nothing in the
    redeem response — the only place any of it appears — is lost.

    Three things, each a single copy:
      · the device token, so a re-attach re-keys the parent's one row for this
        machine instead of minting a second (enrolment.py's re-pairing rule);
      · the parent's identity — its host id, name, mesh address and port — so
        this leaf can show its own devices a tile for the parent and mint on it
        in reverse, the direction #62 was missing;
      · the grant the parent issued, kept in `host_grants` (grants.py) rather
        than in this file, because it is a credential to spend and the grant
        roster is where credentials already live. Without it `mint_on` has
        nothing to present and the parent's `/delegate/mint` answers 401.

    A parent on a build from before #62 sends neither identity nor grant; the
    leaf simply records the token as it always did and never tiles a home,
    which is the pre-#62 behaviour, not a failure.
    """
    device = result.get("device") or {}
    identity = result.get("parent_identity") or {}
    rec = {
        "parent_url": parent_url,
        "device_id": device.get("id", ""),
        "token": result.get("token", ""),
        "parent_key": identity.get("key", ""),
        "parent_name": identity.get("name", ""),
        "parent_address": identity.get("address", ""),
        "parent_port": identity.get("port", DEFAULT_PORT),
    }
    state.mkdir(parents=True, exist_ok=True)
    path = state / PARENT_RECORD
    path.write_text(json.dumps(rec, indent=2))
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)

    # Held, keyed by the parent's own host id, replacing any grant this leaf
    # held for a parent of the same id. `list_hosts` surfaces it only while it
    # is live, so a grant revoked at the parent takes the home tile with it.
    from . import grants
    key, leaf_grant = identity.get("key", ""), result.get("leaf_grant", "")
    if key and leaf_grant:
        grants.remember(key, leaf_grant, parent_url)


def parent_record() -> dict:
    """What this machine recorded about its parent at attach, or `{}` when it
    has none. The router reads it to synthesise the parent's tile (#62); it
    carries the parent's id, name, mesh address and port, never the grant."""
    try:
        return json.loads((hostenv.state_dir() / PARENT_RECORD).read_text())
    except (OSError, ValueError):
        return {}


def _redeem(parent_url: str, payload: dict, poster) -> dict:
    """Spend the code on the parent, mapping its status codes to plain words.

    The parent's answers are the router's: 400 for a bad host key or port
    (decided from the request, so it may be specific), 429 with a lockout, 401
    for every kind of bad code (deliberately indistinguishable), 200 with the
    device, token and — for a host code — the leaf bundle.
    """
    url = parent_url.rstrip("/") + REDEEM_PATH
    status, body = poster(url, payload)
    if status == 200:
        return body
    detail = body.get("detail") or f"HTTP {status}"
    if status == 429:
        raise AttachError(f"the parent is rate-limiting enrolment: {detail}")
    if status in (400, 401):
        raise AttachError(detail)
    raise AttachError(f"the parent refused the code ({status}): {detail}")


def attach(code: str, parent_url: str, *, host_key: str,
           port: int = DEFAULT_PORT, dest_dir: Path | None = None,
           poster=None, runner=None, sudo: bool = True,
           install_env: dict | None = None, prober=None,
           home: Path | None = None) -> dict:
    """Redeem a host code on `parent_url` and stand up the leaf tunnel it hands
    back, turning this Mac into a managed hub of the parent.

    Returns the facts of what happened: the device row and token the parent
    minted, the host row it recorded, whether an existing row was re-keyed, the
    bundle directory, and the reachability note. The *mode* this left the
    machine in is the CLI's to read afterwards, off the machine's own state —
    this function does the attaching and does not also grade it.

    Raises AttachError for anything a person can act on: an unusable host key, a
    parent that cannot be reached, a refused code, a device code where a host
    code was needed, a parent with no mesh to join, or an installer that failed.
    """
    poster = poster or _httpx_post
    runner = runner or subprocess.run
    prober = prober or subprocess.run

    if not HOST_KEY_RE.match(host_key or ""):
        raise AttachError(
            "this machine has no usable host id to present to the parent "
            f"({host_key!r}) — it should be 8 to 128 characters of letters, "
            "digits, dot, dash, underscore or colon")
    if not (parent_url.startswith("http://") or parent_url.startswith("https://")):
        raise AttachError(
            f"the parent address must be an http(s) URL, not {parent_url!r} — "
            "e.g. http://studio.local:9090")
    _refuse_a_parent_only_the_tunnel_can_reach(parent_url)

    state = hostenv.state_dir()
    payload = {"code": code, "host_key": host_key, "port": int(port)}
    prior = _prior_token(state)
    if prior:
        payload["device_token"] = prior

    # The reciprocal half of the handshake (grants.py). Minted BEFORE the
    # redemption and sent with it, because the parent has exactly one moment
    # where it is talking to this machine and knows which machine it is — asking
    # for it afterwards would need the parent to authenticate to a host it has no
    # credential for yet, which is the chicken and egg this solves.
    #
    # Every grant this machine issued is revoked first. Attaching is a statement
    # about who administers this Mac, and a grant from a previous parent
    # surviving it would leave a machine that left a mesh still mintable from it.
    from . import grants
    grants.revoke_issued()
    payload["grant_token"] = grants.issue(parent_url)

    # The shell half of the same one-moment handshake (#131): the public key
    # of an identity minted here, whose private half never crosses the wire,
    # and the account a granted machine shells into. A mint that fails costs
    # shell access, never the attach.
    import getpass
    from . import shell_access
    try:
        payload["ssh_pubkey"] = shell_access.identity()
        payload["ssh_user"] = getpass.getuser()
    except shell_access.ShellAccessError:
        pass

    result = _redeem(parent_url, payload, poster)

    if result.get("kind") != "host":
        raise AttachError(
            "that code enrols a device, not a machine — attaching a Mac to a "
            "parent needs a host code (minted with kind=host on the parent)")

    peer = result.get("tunnel")
    if not peer or not peer.get("bundle"):
        # The code was spent and a device row exists on the parent, but the
        # parent runs no mesh, so there is nothing to dial into and this Mac is
        # not a managed hub. Say so plainly rather than reporting a success that
        # installed nothing.
        note = result.get("tunnel_note") or "the parent does not run a mesh"
        raise AttachError(
            f"the parent has no mesh to join — {note}. Nothing was installed; "
            "this machine did not become a managed hub.")

    dest = Path(dest_dir) if dest_dir else state / "leaf-bundle"
    _write_bundle(peer["bundle"], dest)
    _record_parent(state, parent_url, result)

    env = dict(install_env) if install_env is not None else dict(os.environ)
    already = _tunnel_already_up(dest, sudo=sudo, env=env, prober=prober)
    if already is not None:
        installer_output, installer_ran = already, False
    else:
        argv = (["sudo"] if sudo else []) + ["bash", str(dest / "install_leaf.sh")]
        proc = runner(argv, cwd=str(dest), env=env,
                      capture_output=True, text=True)
        if proc.returncode != 0:
            raise AttachError(
                "the leaf installer failed: "
                + ((proc.stderr or proc.stdout or "").strip() or "no output")
                + f"\nthe bundle is at {dest} — fix the cause and re-run "
                "`sudo bash install_leaf.sh` from there")
        installer_output, installer_ran = (proc.stdout or "").strip(), True

    shell_steps = _apply_shell(result.get("shell") or {},
                               home=Path(home) if home else Path.home(),
                               runner=runner, sudo=sudo)

    return {
        "device": result.get("device") or {},
        "token": result.get("token", ""),
        "host": result.get("host"),
        "superseded": bool(result.get("superseded")),
        "tunnel_note": result.get("tunnel_note", ""),
        "bundle_dir": str(dest),
        "parent_url": parent_url,
        "installer_output": installer_output,
        # False when the tunnel this bundle describes was already installed and
        # carrying, so nothing was (re)started — the offline joiner's case.
        "installer_ran": installer_ran,
        # Whether the parent kept the grant — read from the parent's own answer,
        # never from the fact that one was sent. A parent running a build from
        # before delegated minting ignores the field entirely, and this machine
        # must not tell its owner their devices get in by themselves when that
        # parent has no idea how to let them.
        "delegated": bool(result.get("delegated")),
        # Whether the token above is worth anything. A parent with reachback
        # switched off mints the row and revokes it in the same breath, so
        # this machine holds a credential that authenticates nothing. Read
        # from the parent's answer and defaulted TRUE, because a parent on an
        # older build does not send the field and its tokens do work — the
        # absent case has to mean "yes" or every existing parent starts
        # reporting a restriction it does not impose.
        "reachback": bool(result.get("reachback", True)),
        # The graded shell-grant steps — [] when the parent answered no
        # `shell`, which is a parent from before hub-held shell grants or a
        # mint that failed here, and either way an attach exactly as it was.
        "shell_steps": shell_steps,
    }


def _apply_shell(shell: dict, *, home: Path,
                 runner, sudo: bool) -> list[dict]:
    """Lay what the parent's `shell` answer grants, graded like detach's steps.

    Runs after the tunnel is up, so a step that fails is reported beside the
    attach that still succeeded — shell access is re-runnable via the joiner,
    the enrolment code it would cost is not.

    The root step goes first because the joiner is where it is spent: with it
    recorded, `apply_material` grades it as done instead of reporting it
    outstanding, which is the report reserved for a machine presenting its
    identity over the managed channel with no root moment of its own.
    """
    if not shell.get("authorized"):
        return []
    from . import shell_access
    steps = shell_access.enable(runner=runner, sudo=sudo)
    steps += shell_access.apply_material(shell, home)
    return steps
