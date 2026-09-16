"""Updates use existing authenticated routes, with narrower write authority."""
from __future__ import annotations

import ipaddress
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import devices, fleet_updates as fleet, hostenv, managed_access
from .auth import current_device
from .release_manifest import ReleaseError, identifier

router = APIRouter()


def local_admin(request: Request, device_id: str) -> None:
    try:
        local = ipaddress.ip_address(request.client.host).is_loopback
    except (AttributeError, ValueError):
        local = False
    if not local or device_id != "host-internal":
        raise HTTPException(403, "updates require the local menu bar credential")


def leaf(device_id: str) -> dict:
    row = managed_access.leaf_for_device(device_id)
    if managed_access.is_leaf() or row is None or row["deleted"]:
        raise HTTPException(403, "an active adopted machine is required")
    return row


def offered() -> dict | None:
    try:
        if managed_access.is_leaf():
            return managed_access._post_parent("updates/check", {}).get("offer")
        return fleet.offer()
    except (ReleaseError, ValueError, OSError) as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/updates/latest")
def latest():
    return {"offer": offered()}


@router.get("/updates/inventory")
def inventory(request: Request, device_id: str = Depends(current_device)):
    local_admin(request, device_id)
    from .store import get_store
    offer = offered()
    desired = offer["manifest"]["release"] if offer else None
    store = fleet.FleetStore()
    # Only a recent supervisor report implies update capability. Reading this
    # endpoint must not manufacture a supervisor heartbeat on an old install.
    rows = [store.inventory(hostenv.host_id(), hostenv.host_name(), desired)]
    if not managed_access.is_leaf():
        for row in get_store().list_hosts():
            if not row["deleted"]:
                rows.append(store.inventory(row["key"], row["name"], desired))
    return {"release": desired, "machines": rows}


class QueueRequest(BaseModel):
    target: str = Field(max_length=128)
    request_id: str = Field(max_length=128)


@router.post("/updates/queue")
def queue(body: QueueRequest, request: Request, device_id: str = Depends(current_device)):
    local_admin(request, device_id)
    from .store import get_store
    if managed_access.is_leaf():
        if body.target not in ("self", hostenv.host_id()):
            raise HTTPException(403, "a managed Mac can update only itself")
        # The hub owns this job even when initiated by a local leaf click.
        return managed_access._post_parent("updates/request", {"request_id": body.request_id})
    offer = offered()
    if offer is None:
        raise HTTPException(409, "no complete release has been offered")
    targets = [(hostenv.host_id(), "local")]
    targets.extend((row["key"], row["device_id"]) for row in get_store().list_hosts()
                   if not row["deleted"] and row["device_id"])
    if body.target != "all":
        key = hostenv.host_id() if body.target == "self" else body.target
        targets = [row for row in targets if row[0] == key]
    if not targets:
        raise HTTPException(404, "unknown managed machine")
    store = fleet.FleetStore()
    jobs, errors = [], []
    for machine, authority in targets:
        try:
            if authority != "local":
                row = devices.row(authority)
                if row is None or row.get("revoked_at") is not None:
                    raise ReleaseError("machine credential is revoked; update not authorized")
            jobs.append(fleet.public_job(store.queue(machine, authority, offer, body.request_id)))
        except ReleaseError as exc:
            errors.append({"machine": machine, "detail": str(exc)})
    if not jobs:
        raise HTTPException(409, errors)
    return {"jobs": jobs, "errors": errors}


class LocalRequest(BaseModel):
    request_id: str = Field(max_length=128)


@router.post("/managed/updates/request")
def request_own_update(body: LocalRequest, device_id: str = Depends(current_device)):
    row = leaf(device_id)
    offer = offered()
    if offer is None:
        raise HTTPException(409, "no complete release has been offered")
    try:
        job = fleet.FleetStore().queue(row["key"], device_id, offer, body.request_id)
        return {"jobs": [fleet.public_job(job)], "errors": []}
    except ReleaseError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/managed/updates/check")
def managed_check(device_id: str = Depends(current_device)):
    leaf(device_id)
    return {"offer": offered()}


class Heartbeat(BaseModel):
    observation: dict = Field(default_factory=dict)
    job_id: str = Field(default="", max_length=128)
    state: str = Field(default="", max_length=32)
    detail: str = Field(default="", max_length=2000)


def _confirm(machine: str, report: dict, job: dict) -> bool:
    """The hub reconnects independently; a leaf's success claim is insufficient."""
    import httpx
    from . import grants, sourcestamp
    from .store import get_store
    if report.get("release") != job["release"] or report.get("verified") is not True:
        return False
    manifest = json.loads(job["envelope"])["manifest"]
    if machine == hostenv.host_id():
        source = sourcestamp.capture()
    else:
        row = get_store().host_row(machine)
        if row is None or row["deleted"]:
            return False
        try:
            access = grants.mint_on(row, "Update verification", owner_id="host-internal")
            with httpx.Client(timeout=6, trust_env=False) as client:
                base = f"http://{access['address']}:{access['port']}/api/jremote/v1"
                headers = {"Authorization": "Bearer " + access["token"]}
                response = client.get(base + "/host", headers=headers)
                response.raise_for_status()
                identity = response.json()
                if identity.get("host_id") != machine:
                    return False
                source = identity.get("source", {})
                response = client.get(base + "/sessions/active", headers=headers)
                response.raise_for_status()
                if not isinstance(response.json().get("sessions"), list):
                    return False
        except (httpx.HTTPError, grants.GrantError, KeyError, ValueError):
            return False
    return (source.get("sha") == manifest["sources"]["stack"] and
            source.get("release") == manifest["release"] and not source.get("dirty"))


def heartbeat(machine: str, authority: str, body: Heartbeat) -> dict:
    store = fleet.FleetStore()
    try:
        store.report(machine, body.observation)
        job = store.latest(machine)
        if job and job["authority"] != authority:
            if job["state"] in {"pending", "downloading"}:
                store.transition(job["id"], machine, "cancelled", "adoption authority changed")
            job = None
        if job and body.job_id == job["id"]:
            state = ("verifying" if body.state == "current" and job["state"] != "current"
                     else body.state)
            if state and state != job["state"]:
                job = store.transition(job["id"], machine, state, body.detail)
            if job["state"] == "verifying" and _confirm(machine, body.observation, job):
                job = store.transition(job["id"], machine, "current", verified=True)
        result = fleet.public_job(job)
        if result and job["state"] in fleet.ACTIVE:
            result["envelope"] = json.loads(job["envelope"])
        return {"job": result, "offer": offered()}
    except (ReleaseError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/managed/updates/heartbeat")
def managed_heartbeat(body: Heartbeat, device_id: str = Depends(current_device)):
    return heartbeat(leaf(device_id)["key"], device_id, body)


@router.post("/updates/heartbeat")
def local_heartbeat(body: Heartbeat, request: Request,
                    device_id: str = Depends(current_device)):
    local_admin(request, device_id)
    if managed_access.is_leaf():
        # Retain local progress so the leaf's menu does not require a second
        # fleet owner. Execution still gets its authority directly from parent.
        fleet.FleetStore().report(hostenv.host_id(), body.observation)
        return {"job": None}
    return heartbeat(hostenv.host_id(), "local", body)


@router.get("/managed/updates/artifact/{release}/{filename}")
def managed_artifact(release: str, filename: str, device_id: str = Depends(current_device)):
    leaf(device_id)
    return artifact(release, filename)


@router.get("/updates/artifact/{release}/{filename}")
def artifact(release: str, filename: str):
    try:
        identifier(release)
        identifier(filename)
        directory = fleet.feed_dir() / release
        envelope = json.loads((directory / "manifest.json").read_text())
        from .release_manifest import verify
        settings = fleet.config()
        manifest = verify(envelope, settings.get("public_key", ""),
                          promoted=not settings.get("candidate_test", False))
        if manifest["release"] != release or filename not in {
                item["file"] for item in manifest["components"].values()}:
            raise ReleaseError("artifact is not in the release")
        path = (directory / filename).resolve()
        if path.parent != directory.resolve() or not path.is_file():
            raise ReleaseError("artifact missing or outside release")
        return FileResponse(path, media_type="application/octet-stream")
    except (ReleaseError, OSError, ValueError) as exc:
        raise HTTPException(404, str(exc)) from exc
