"""Hub-owned access policy, shared by HTTP, streams, delegation and discovery.

The existing hosts table owns the two visibility switches. A managed device
credential on a leaf is a stable projection of a hub credential, not a new
independent enrollment. Each request rechecks the hub; no positive permission
cache can outlive a revocation or a visibility change.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress

from fastapi import HTTPException


def parent_record() -> dict:
    from . import attach_parent
    return attach_parent.parent_record()


def is_leaf() -> bool:
    from . import mode
    return bool(parent_record()) or mode.is_managed()


def console(request) -> bool:
    from . import mode
    try:
        local = ipaddress.ip_address(request.client.host).is_loopback
    except (AttributeError, ValueError):
        return False
    return local and not is_leaf() and mode.is_hub()


def require_console(request) -> None:
    if not console(request):
        raise HTTPException(403, "device and machine management belongs to the hub menu bar")


def leaf_for_device(device_id: str) -> dict | None:
    from .store import get_store
    return get_store().host_for_device(device_id)


def may_reach(device_id: str, target: str) -> bool:
    from . import devices, hostenv
    row = devices.row(device_id)
    if row is None or row.get("revoked_at") is not None:
        return False
    if device_id in devices.SHARED_IDS:
        return True
    leaf = leaf_for_device(device_id)
    if leaf is None:
        from .store import get_store
        # A legacy adoption that cannot be mapped unambiguously is not an
        # ordinary phone. Refuse it until the hub restores its binding.
        return not get_store().is_host_credential(device_id)
    if leaf["deleted"]:
        return False
    if target == hostenv.host_id():
        return bool(leaf["sees_home"])
    return target == leaf["key"] or bool(leaf["sees_leaves"])


def _post_parent(route: str, body: dict) -> dict:
    import httpx
    rec = parent_record()
    token = rec.get("token", "")
    address = rec.get("parent_address")
    base = (f"http://{address}:{int(rec.get('parent_port') or 9090)}"
            if address else rec.get("parent_url", ""))
    if not token or not base:
        raise HTTPException(503, "this managed machine has no parent connection")
    try:
        response = httpx.post(base.rstrip("/") + "/api/jremote/v1/managed/" + route,
                              json=body, headers={"Authorization": "Bearer " + token},
                              timeout=6, trust_env=False)
    except httpx.HTTPError as exc:
        raise HTTPException(503, "the parent hub could not authorize this request") from exc
    if response.status_code != 200:
        raise HTTPException(403 if response.status_code in (401, 403, 404) else 503,
                            "the parent hub refused this access")
    try:
        result = response.json()
    except ValueError as exc:
        raise HTTPException(503, "invalid parent authorization response") from exc
    if not isinstance(result, dict):
        raise HTTPException(503, "invalid parent authorization response")
    return result


def visible_hosts() -> dict:
    return _post_parent("hosts", {})


def parent_grant(key: str) -> dict:
    return _post_parent("grant", {"key": key})


def parent_shell() -> dict:
    return _post_parent("shell", {})


def device_allowed(device_id: str, *, local: bool = False) -> bool:
    from . import devices, grants
    row = devices.row(device_id)
    if row is None or row.get("revoked_at") is not None:
        return False
    digest = row.get("authority_grant", "")
    if digest:
        # Detaching revokes the issuing grant; all its projected credentials
        # stop immediately, even if a parent is unreachable.
        if grants._store().parent_grant(digest) is None:
            return False
        try:
            return _post_parent("authorize", {"device_id": row["authority_device"]}).get("allowed") is True
        except HTTPException:
            return False
    if is_leaf():
        # Existing independently paired leaf tokens cannot bypass hub policy.
        return local and device_id in devices.SHARED_IDS
    return True


def authorize(device_id: str, request) -> None:
    from . import hostenv
    path = request.url.path.removeprefix("/api/jremote/v1")
    ip = request.client.host if request.client else ""
    try:
        local = ipaddress.ip_address(ip).is_loopback
    except ValueError:
        local = False
    if not device_allowed(device_id, local=local):
        raise HTTPException(403, "access is no longer authorized by the parent hub")
    if not path.startswith("/managed/") and not may_reach(device_id, hostenv.host_id()):
        raise HTTPException(403, "this machine is not allowed to access the home instance")


def stream_allowed(device_id: str) -> bool:
    from . import hostenv
    return device_allowed(device_id, local=True) and may_reach(device_id, hostenv.host_id())


def can_disconnect(device_id: str) -> bool:
    from . import devices
    from .store import get_store
    return bool(device_id) and device_id not in devices.SHARED_IDS and (
        leaf_for_device(device_id) is None
        and not get_store().is_host_credential(device_id))


def mint_projection(grant: str, name: str, owner_id: str) -> tuple[dict, str]:
    """Repeat requests return one row and one token; never rotate a live peer."""
    from . import devices, grants
    _, secret = grants.parse(grant)
    if not secret or not owner_id or len(owner_id) > 128:
        raise HTTPException(400, "a hub device identity is required")
    digest = grants._hash(secret)
    material = "managed-device:" + owner_id
    credential = hmac.new(secret.encode(), material.encode(), hashlib.sha256).hexdigest()
    device_id = hashlib.sha256((digest + ":" + owner_id).encode()).hexdigest()[:24]
    token = f"jr1.{device_id}.{credential}"
    store = devices._store()
    store.add_device(device_id, name, devices._hash(credential))
    row = store.device(device_id)
    if row is None or row["revoked_at"] is not None:
        raise HTTPException(403, "this device disconnected from this machine")
    if not hmac.compare_digest(row["token_hash"], devices._hash(credential)):
        raise HTTPException(409, "device identity collision")
    store.bind_device_authority(device_id, digest, owner_id)
    return store.device(device_id), token
