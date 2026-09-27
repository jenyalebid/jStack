"""Hub-held shell grants, and how a flip reaches the machines it changes.

The store row is the authority: `shell_grants(src, dst)` means src's agents
may shell into dst, absence is refusal, and the hub itself rides no row —
hub access is unconditional. A flip writes the row first, then *pokes* each
affected machine over the managed channel; the poked machine pulls its own
set back through its parent credential (`/managed/shell`) and rewrites the
user-writable half, so no key material rides the poke and a missed poke
degrades to "until the next refresh or joiner run", never to a machine
silently keeping revoked access. A live flip spends no root at all, which is
why the authorized block is user-writable; a machine that never spent the one
root step (Remote Login) says so in its graded steps instead of half-granting.

The pull also *takes* the presenting machine's own pubkey and user, so an
identity is not adoption-only: a Mac adopted before shell access existed
becomes shell-capable on one poke, never a re-adoption.
"""

from __future__ import annotations

from pathlib import Path

#: The leaf route a poke hits. A poke carries no payload — the machine pulls.
REFRESH_PATH = "/api/jremote/v1/shell/refresh"


class ShellGrantError(Exception):
    """The flip itself could not happen. A *poke* that fails is a graded step,
    never a raise — the row already changed."""


def _store():
    from .store import get_store
    return get_store()


def leaf_shell(host_key: str) -> dict:
    """What `host_key`'s machine must authorize and may reach — the one
    compute behind both the adoption handshake and a live pull. {} for a
    machine that has not presented a key yet — a build old enough to present
    none at all, or the first moment of a pre-shell Mac's refresh, before the
    same request stores what it presented."""
    store = _store()
    row = store.host_row(host_key)
    if row is None or row["deleted"] or not row["shell_pubkey"]:
        return {}
    from . import shell_access
    from .enrolment import peer_name
    authorized = [shell_access.identity()]
    for src in store.shell_sources_for(host_key):
        srow = store.host_row(src)
        if srow and not srow["deleted"] and srow["shell_pubkey"]:
            authorized.append(srow["shell_pubkey"])
    peers = []
    for dst in store.shell_targets_for(host_key):
        drow = store.host_row(dst)
        if (drow and not drow["deleted"] and drow["address"]
                and drow["shell_user"]):
            peers.append({"name": peer_name(drow["name"]) or dst,
                          "address": drow["address"],
                          "user": drow["shell_user"]})
    return {"authorized": authorized, "peers": peers}


def refresh_on(host_key: str, *, poster=None) -> dict:
    """Poke one machine to re-pull its set. Graded, never raising: an
    unreachable machine catches up at its next refresh or joiner run, and
    the whole-set rewrite makes any later poke converge."""
    from . import grants
    step = {"step": f"refresh:{host_key}", "ok": False, "note": ""}
    row = _store().host_row(host_key)
    if row is None or row["deleted"]:
        step["note"] = "unknown machine"
        return step
    try:
        access = grants.mint_on(dict(row), "Shell refresh",
                                poster=poster, owner_id="host-internal")
        status, body = (poster or grants._httpx_post)(
            f"http://{access['address']}:{access['port']}{REFRESH_PATH}",
            {}, access["token"])
    except grants.GrantError as exc:
        step["note"] = f"{exc} — its keys catch up at the next joiner run"
        return step
    if status != 200:
        step["note"] = (f"the machine answered {status}: "
                        f"{body.get('detail', 'no detail')} — its keys catch "
                        "up at the next joiner run")
        return step
    applied = body.get("steps") or []
    bad = [s for s in applied if not s.get("ok")]
    step["ok"] = not bad
    step["note"] = (f"applied {len(applied)} step(s)" if not bad else
                    "the machine reported a failure: "
                    + (bad[0].get("note") or bad[0].get("step", "")))
    return step


def flip(src: str, dst: str, allowed: bool, *, poster=None) -> dict:
    """Grant or revoke src's shell access into dst, live."""
    store = _store()
    if src == dst:
        raise ShellGrantError("a machine does not need a grant to itself")
    for key in (src, dst):
        row = store.host_row(key)
        if row is None or row["deleted"]:
            raise ShellGrantError(f"unknown machine: {key}")
    store.set_shell_grant(src, dst, allowed)
    # dst's authorized set changed; src's reachable peers changed.
    return {"src": src, "dst": dst, "allowed": allowed,
            "steps": [refresh_on(dst, poster=poster),
                      refresh_on(src, poster=poster)]}


def machine_forgotten(key: str, *, poster=None) -> list[dict]:
    """The hub half of revocation-by-forget: every pair the machine is in
    goes, every counterpart is poked to shed its key or its config entry,
    and the hub's own config drops it. The leaf half is `detach`."""
    store = _store()
    affected = sorted(set(store.shell_sources_for(key))
                      | set(store.shell_targets_for(key)))
    store.drop_shell_grants(key)
    steps = [refresh_on(k, poster=poster) for k in affected]
    try:
        refresh_hub_config()
    except OSError as exc:
        steps.append({"step": "hub-ssh-config", "ok": False,
                      "note": f"could not rewrite the hub's ssh config: {exc}"})
    return steps


def hub_peers() -> list[dict]:
    """Every adopted machine the hub can shell into, as ssh-config peers."""
    from .enrolment import peer_name
    peers = []
    for row in _store().list_hosts():
        if row["deleted"] or not row["address"] or not row["shell_user"]:
            continue
        peers.append({"name": peer_name(row["name"]) or row["key"],
                      "address": row["address"], "user": row["shell_user"]})
    return peers


def refresh_hub_config(path: Path | None = None) -> None:
    """`ssh <leaf>` from the hub itself, kept current as machines join."""
    from . import shell_access
    shell_access.write_ssh_config(
        Path(path) if path else Path.home() / ".ssh" / "config", hub_peers())


def _is_hub() -> bool:
    """A leaf's two ssh blocks are its parent's answer (`/shell/refresh`); a
    hub-side rewrite there would erase them. Only a machine that owns the mesh
    and has no parent writes them from its own store."""
    from . import managed_access, mode
    return mode.is_hub() and not managed_access.is_leaf()


def hub_drift(home: Path | None = None) -> list[str]:
    """Which of the hub's two managed blocks disagree with the store — read
    only. The hosts block must be exactly `hub_peers()`; the keys block must be
    empty, because nothing grants a shell *into* the hub (it rides no row), so
    any key there was put by something other than a grant."""
    from . import shell_access
    ssh = (Path(home) if home else Path.home()) / ".ssh"
    drift = []
    if (shell_access.read_config_block(ssh / "config")
            != shell_access.config_block(hub_peers())):
        drift.append("config")
    if shell_access.read_authorized_block(ssh / "authorized_keys"):
        drift.append("authorized_keys")
    return drift


def reconcile_hub(home: Path | None = None) -> dict:
    """Put the hub's ssh blocks back to what its store says, whatever emptied
    them. Enrolment, a grant change and a forget each rewrite the config as a
    side effect; nothing else did, so a config emptied from outside stayed
    empty until the next adoption (#186). Run at host startup and from
    `jstack-host shell-grants refresh`; a no-op when nothing drifted, and never
    on a leaf."""
    from . import shell_access
    if not _is_hub():
        return {"ok": True, "hub": False, "fixed": [],
                "note": "not a hub — this machine's ssh blocks come from its parent"}
    ssh = (Path(home) if home else Path.home()) / ".ssh"
    drift = hub_drift(home)
    if "config" in drift:
        refresh_hub_config(ssh / "config")
    if "authorized_keys" in drift:
        shell_access.write_authorized_block(ssh / "authorized_keys", [])
    return {"ok": True, "hub": True, "fixed": drift,
            "note": f"{len(hub_peers())} reachable machine(s)"}
