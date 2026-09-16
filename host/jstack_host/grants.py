"""Delegation is an adoption capability, not independent device membership.

A leaf issues one grant to its parent when it attaches. The hub holds that
secret and uses /delegate/access to obtain stable projections of its devices
on the leaf. Devices never receive the grant, only their own projected token.

The leaf checks the parent\'s policy on every authenticated request and while
streams are open. Hub revocation, withdrawal and the two per-leaf visibility
settings therefore apply to credentials already cached by clients. Revoking
an issued grant also invalidates every projection derived from it.

The separate jrg1 namespace authenticates only the delegation endpoint. A
hub credential is still required for ordinary APIs; managed_access.py applies
the shared authority policy beneath HTTP, WebSocket, SSE and discovery.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid

#: Grant tokens are their own namespace. `jr1.` is a device token and `jrg1.` is
#: a grant, and the prefix is what makes a grant presented to `require_token`
#: fail as an unknown credential rather than being parsed as a device id — two
#: credential formats sharing a prefix is how one gets accepted where the other
#: was meant.
GRANT_PREFIX = "jrg1"

#: The route a grant authenticates. One constant, named here, so the module that
#: *holds* a grant and the module that *verifies* one cannot drift apart on it.
# A new endpoint prevents an older leaf from silently ignoring owner_id and
# issuing independent credentials. It must be upgraded before access resumes.
MINT_PATH = "/api/jremote/v1/delegate/access"


class GrantError(Exception):
    """Delegated minting could not be completed. The message is meant for the
    person who asked for the access, not for a log."""


def _store():
    from .store import get_store
    return get_store()


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def parse(presented: str) -> tuple[str, str]:
    """`jrg1.<id>.<secret>` → (id, secret), or ("", "") for anything else."""
    parts = (presented or "").split(".")
    if len(parts) != 3 or parts[0] != GRANT_PREFIX:
        return "", ""
    return parts[1], parts[2]


# ── the leaf's end: issuing and verifying ──

def issue(parent: str) -> str:
    """Mint a grant for `parent` and return it. The token exists only in this
    return value — the caller's one job is to hand it over and forget it.

    `parent` is a label, not an authority: it is what the grant roster shows a
    person, and nothing branches on it. Two grants from two parents are two
    independent credentials, each revocable alone.
    """
    grant_id = uuid.uuid4().hex[:12]
    secret = secrets.token_urlsafe(32)
    _store().put_parent_grant(_hash(secret), parent or "")
    return f"{GRANT_PREFIX}.{grant_id}.{secret}"


def authenticate(presented: str) -> str | None:
    """The parent label behind a live grant, or None.

    Hashed compare against the stored digest, constant-time, the same posture
    `devices.authenticate` holds — and like it, this never grandfathers anything
    into existence. A host with no grants issued authenticates nobody, which is
    the correct answer for a machine that has never been attached to anything.
    """
    _, secret = parse(presented)
    if not secret:
        return None
    row = _store().parent_grant(_hash(secret))
    if row is None:
        return None
    # The digest IS the key, so a row coming back is already the match. The
    # compare stays as the explicit, constant-time statement of that — a future
    # lookup that widens (by id, say) must not silently become a bare equality.
    if not hmac.compare_digest(row["token_hash"], _hash(secret)):
        return None
    return row["parent"] or "(unnamed parent)"


def note_used(presented: str) -> None:
    _, secret = parse(presented)
    if secret:
        _store().note_parent_grant_used(_hash(secret))


def revoke_issued(parent: str = "") -> int:
    """Revoke grants this machine issued — one parent's, or every one. This is
    the revocation that actually ends authority, because this is the end that
    verifies."""
    return _store().revoke_parent_grants(parent)


def issued() -> list[dict]:
    return _store().list_parent_grants()


# ── the hub's end: holding and spending ──

def remember(host_key: str, token: str, parent_url: str = "") -> None:
    """Keep the grant a machine issued at attach. Silently ignores an empty
    token: a leaf running a build from before this existed sends none, and it is
    a machine that joined the mesh without delegating, not an error."""
    if token:
        _store().put_host_grant(host_key, token, parent_url)


def held(host_key: str) -> str:
    row = _store().host_grant(host_key)
    return (row or {}).get("token", "")


def forget(host_key: str) -> bool:
    return _store().revoke_host_grant(host_key)


def holdings() -> list[dict]:
    """Which machines this host can mint on — never the tokens themselves."""
    return _store().list_host_grants()


def _httpx_post(url: str, payload: dict, token: str) -> tuple[int, dict]:
    import httpx
    try:
        resp = httpx.post(url, json=payload, timeout=20.0,
                          headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise GrantError(f"could not reach {url}: {exc}")
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, body if isinstance(body, dict) else {}


def mint_on(host_row: dict, name: str, poster=None, *, owner_id: str = "") -> dict:
    """Spend this host's grant on `host_row`'s machine and return what it minted.

    The address comes off the registry row — the mesh address the machine was
    handed when it joined — and never from the caller. A device naming the host
    it wants a token from would be a device pointing this host's credential at
    a machine of its choosing, which turns a grant into an oracle.

    Returns the leaf's own answer plus the facts a device needs to store the
    result: which machine, at which address, on which port.
    """
    poster = poster or _httpx_post
    key = host_row.get("key") or ""
    token = held(key)
    if not token:
        raise GrantError(
            f"this host holds no grant for {host_row.get('name') or key} — it "
            "joined the mesh without delegating, or the grant was revoked here. "
            "Re-attach that machine to restore it.")
    address = (host_row.get("address") or "").strip()
    if not address:
        raise GrantError(
            f"{host_row.get('name') or key} has no address in the registry — it "
            "enrolled without a mesh peer, so there is nothing to reach.")
    port = int(host_row.get("port") or 9090)
    url = f"http://{address}:{port}{MINT_PATH}"

    payload = {"name": name}
    if owner_id:
        payload["owner_id"] = owner_id
    status, body = poster(url, payload, token)
    if status == 200:
        _store().note_host_grant_used(key)
        return {"host": key, "name": body.get("device", {}).get("name", name),
                "address": address, "port": port,
                "device": body.get("device") or {},
                "token": body.get("token", "")}
    detail = body.get("detail") or f"HTTP {status}"
    if status in (401, 403):
        raise GrantError(
            f"{host_row.get('name') or key} refused this host's grant — it was "
            f"revoked there ({detail}). Re-attach that machine to restore it.")
    raise GrantError(f"{host_row.get('name') or key} could not mint ({status}): "
                     f"{detail}")


def stamp(ts) -> str:
    """An epoch second as a readable line, or "" — shared by the CLI surfaces
    that print a grant roster."""
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
