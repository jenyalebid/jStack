"""`jstack-host detach` — leave a parent hub, at both ends, in one command.

Attach had a front door and leaving did not, and the asymmetry was not cosmetic.
Undoing an attach by hand is: revoke the grants this machine issued, boot out two
LaunchDaemons, delete a conf holding a private key, delete the bundle, forget the
parent record, and then — on the *other* machine, which is the whole problem —
revoke the device row and drop the registry tile. Nobody does the last two. What
is left behind is not clutter: it is a live credential on a machine that believes
it is still administering this one, and a tile on every device promising access
to something that no longer answers.

So detaching is the reverse of attaching and is graded the same way: it reports
what it actually did, per step, and a step that could not run says so instead of
being folded into a success.

## Order, and why it is this one

  1. **Revoke the grants this machine issued.** First, always, and never
     conditional on anything else working. It is the only step that ends real
     authority — the parent's ability to mint here — and it is purely local, so
     it cannot fail for a reason that lives on the network. A detach that got no
     further than this has still done the thing that matters most.
  2. **Tell the parent**, with the credential the parent itself issued at attach
     (`parent.json`): forget the registry tile, then revoke this machine's device
     row. Best-effort by nature — the mesh is about to come down, the parent may
     be off, and a parent that cannot be reached must not be able to keep a
     machine attached. Forget before revoke, because the revoke is a self-revoke
     that kills the credential both calls are using.
  3. **Bring the tunnel down and remove it** — the two daemons, the conf, the
     app dir, the logs. Needs root, which is why it is a step that can be skipped
     (`--keep-tunnel`) rather than a prerequisite for the rest.
  4. **Drop the local record** — the bundle and `parent.json`. Last, because
     `parent.json` holds the token step 2 needs.

`--keep-tunnel` exists for the honest case where a machine should stay on the
mesh but stop being administered from it: the mesh is a transport, delegation is
an authority, and un-delegating without leaving the network is a real thing to
want. Its opposite — bringing the tunnel down and leaving the grants live — is
not offered, because that is a machine that looks detached and is not.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from . import hostenv
from .attach_parent import PARENT_RECORD

#: The two LaunchDaemons `install_leaf.sh` writes, and the paths it writes them
#: to. Named here rather than derived, because this file has to be able to clean
#: up after a bundle that is already gone — deriving them from the bundle would
#: make "the bundle was deleted" into "the tunnel can never be removed".
LEAF_LABELS = ("com.jremote.leaf", "com.jremote.leaf-watch")
LEAF_PATHS = (
    "/Library/LaunchDaemons/com.jremote.leaf.plist",
    "/Library/LaunchDaemons/com.jremote.leaf-watch.plist",
    "/etc/wireguard/jrleaf.conf",
    "/Library/Application Support/jRemote Leaf",
    "/var/log/jremote-leaf",
)


class DetachError(Exception):
    """Detaching could not even begin. Carries a message for the person who ran
    the command — a *step* that fails is reported, not raised."""


def parent_record(state: Path | None = None) -> dict:
    """What this machine knows about the parent it attached to, or {}."""
    state = state or hostenv.state_dir()
    try:
        rec = json.loads((state / PARENT_RECORD).read_text())
        return rec if isinstance(rec, dict) else {}
    except (OSError, ValueError):
        return {}


def _httpx_post(url: str, token: str) -> tuple[int, dict]:
    import httpx
    try:
        resp = httpx.post(url, timeout=15.0,
                          headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        return 0, {"detail": str(exc)}
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, body if isinstance(body, dict) else {}


def _tell_parent(rec: dict, host_key: str, poster) -> list[dict]:
    """Ask the parent to forget this machine and revoke its credential.

    Returns one step record per call attempted. Never raises: every failure here
    is a fact about a machine this one is in the middle of leaving.
    """
    steps: list[dict] = []
    url = (rec.get("parent_url") or "").rstrip("/")
    token = rec.get("token") or ""
    device_id = rec.get("device_id") or ""
    if not url or not token:
        steps.append({
            "step": "parent", "ok": False,
            "note": "no parent record on this machine — nothing was told to "
                    "forget it; if a tile for this Mac is still showing, forget "
                    "it on the parent by hand"})
        return steps

    if not host_key:
        # Never silently skipped. The tile is what every device sees; leaving
        # one pointing at a machine that no longer answers is the visible half
        # of a bad detach, and a caller with no key to send has to be told it
        # happened rather than reading a step list that never mentions it.
        steps.append({
            "step": "parent-forget", "ok": False,
            "note": "this machine could not name itself to the parent, so its "
                    "tile is still there — forget it on the parent by hand"})
    else:
        status, body = poster(
            f"{url}/api/jremote/v1/hosts/{host_key}/forget", token)
        steps.append({
            "step": "parent-forget", "ok": status == 200,
            "note": ("the parent dropped this machine from the grid"
                     if status == 200 else
                     f"the parent did not drop the tile ({status or 'unreachable'}: "
                     f"{body.get('detail', 'no detail')}) — forget it there by hand")})
    # Last, and deliberately: this kills the token the call above needs.
    if device_id:
        status, body = poster(
            f"{url}/api/jremote/v1/devices/{device_id}/revoke", token)
        steps.append({
            "step": "parent-revoke", "ok": status == 200,
            "note": ("this machine's credential on the parent is revoked"
                     if status == 200 else
                     f"the parent did not revoke this machine's credential "
                     f"({status or 'unreachable'}: {body.get('detail', 'no detail')}) "
                     f"— revoke device {device_id} there by hand")})
    return steps


def _remove_tunnel(runner, sudo: bool, root: Path) -> list[dict]:
    """Boot out the leaf daemons and delete what `install_leaf.sh` installed."""
    steps: list[dict] = []
    prefix = ["sudo"] if sudo else []
    booted = []
    for label in LEAF_LABELS:
        plist = root / str(Path(LEAF_PATHS[0]).parent).lstrip("/") / f"{label}.plist"
        if not plist.exists():
            continue
        proc = runner(prefix + ["/bin/launchctl", "bootout", f"system/{label}"],
                      capture_output=True, text=True)
        # 3 is "no such process" — a daemon that was already down is the state
        # this is trying to reach, not a failure to reach it.
        booted.append((label, proc.returncode in (0, 3),
                       (proc.stderr or "").strip()))
    for label, ok, err in booted:
        # Named, because there are two of these and they are the only steps
        # whose notes would otherwise be identical. A report with two lines
        # reading "unloaded" and nothing to tell them apart looks like the same
        # step printed twice — and the one a person has to go fix by hand is
        # whichever of the two failed.
        steps.append({"step": f"bootout:{label}", "ok": ok,
                      "note": (f"unloaded {label}" if ok
                               else f"launchctl refused {label}: {err}")})

    removed, failed = [], []
    for raw in LEAF_PATHS:
        target = root / raw.lstrip("/")
        if not target.exists():
            continue
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            removed.append(raw)
        except OSError:
            # Retry under sudo — everything here is root-owned on a real machine
            # and unprivileged under LEAF_DEST in the tests.
            proc = runner(prefix + ["/bin/rm", "-rf", str(target)],
                          capture_output=True, text=True)
            (removed if proc.returncode == 0 else failed).append(raw)
    steps.append({
        "step": "tunnel-files",
        "ok": not failed,
        "note": (f"removed {len(removed)} of the leaf's files"
                 if not failed else
                 "could not remove " + ", ".join(failed) + " — try again with sudo")})
    return steps


def detach(*, host_key: str = "", keep_tunnel: bool = False,
           tell_parent: bool = True, sudo: bool = True,
           poster=None, runner=None, root: Path | None = None,
           state: Path | None = None, home: Path | None = None) -> dict:
    """Leave the parent hub, and report every step by name.

    Returns `{"steps": [...], "detached": bool}`. `detached` is the answer to
    "is this machine still administered from somewhere else", and it is decided
    by the two steps that answer it — the grants being revoked and the tunnel
    being gone — never by whether the parent could be reached.
    """
    poster = poster or _httpx_post
    runner = runner or subprocess.run
    root = Path(root) if root else Path(os.environ.get("LEAF_DEST") or "/")
    state = state or hostenv.state_dir()
    steps: list[dict] = []

    from . import devices, grants
    rec = parent_record(state)
    attached = bool(rec) or any(r["revoked_at"] is None for r in grants.issued())
    attached = attached or (root / "etc/wireguard/jrleaf.conf").exists()
    try:
        if attached:
            revoked, device_ids = grants._store().revoke_parent_authority()
            for device_id in device_ids:
                devices.notify_revoked(device_id)
        else:
            revoked = 0
    except Exception as exc:
        raise DetachError("could not revoke parent authority; attachment preserved") from exc
    steps.append({
        "step": "grants", "ok": True,
        "note": (f"revoked {revoked} grant{'' if revoked == 1 else 's'} — no "
                 "parent can mint on this machine any more"
                 if revoked else
                 "no grants were outstanding — nothing could mint here")})

    if tell_parent:
        steps.extend(_tell_parent(rec, host_key, poster))

    # Shell access ends with the delegation it rode in on: the granted keys
    # out of authorized_keys, the sudoers drop-in gone, Remote Login restored
    # to what it was before the grant, this machine's identity destroyed.
    from . import shell_access
    steps.extend(shell_access.disable(
        runner=runner, sudo=sudo, root=root, state=state,
        authorized_keys=(Path(home) if home else Path.home())
        / ".ssh" / "authorized_keys"))

    tunnel_gone = True
    if keep_tunnel:
        steps.append({
            "step": "tunnel", "ok": True,
            "note": "left up on request — this Mac is still on the parent's "
                    "mesh, it is just no longer administered from it"})
        tunnel_gone = False
    else:
        steps.extend(_remove_tunnel(runner, sudo, root))
        tunnel_gone = all(s["ok"] for s in steps if s["step"].startswith(
            ("bootout:", "tunnel-files")))

    # Last: parent.json holds the token _tell_parent just spent.
    dropped = []
    for path in (state / PARENT_RECORD, state / "leaf-bundle"):
        if not path.exists():
            continue
        try:
            shutil.rmtree(path) if path.is_dir() else path.unlink()
            dropped.append(path.name)
        except OSError as exc:
            steps.append({"step": "local-record", "ok": False,
                          "note": f"could not remove {path}: {exc}"})
    if dropped:
        steps.append({"step": "local-record", "ok": True,
                      "note": "dropped " + ", ".join(dropped)})

    return {"steps": steps, "detached": tunnel_gone,
            "parent_url": rec.get("parent_url", "")}
