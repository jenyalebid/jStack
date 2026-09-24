"""Updates use existing authenticated routes, with narrower write authority."""
from __future__ import annotations

import ipaddress
import json
import threading
import time

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


# ── The ref this hub follows ────────────────────────────────────────────────
#
# The three doors `jstack-host updates channel` and `updates build` already
# are, opened to the menu bar: read what is followed, follow something else,
# build it. Nothing here runs by itself — no tick reaches these, and each one
# is a press. A route that checked on open would be the download-on-settings
# bug in a new layer, so the read below touches nothing but three local files.

#: Read-modify-write on `build.json` across concurrent presses. Within this
#: process only, which is the only concurrency a double-click produces; the
#: durable guard is the marker itself, which the CLI and the supervisor read.
_build_gate = threading.Lock()


def _source() -> dict:
    """What this hub follows, what it is running, and what was last observed.

    Three local reads and no request: the ref out of the updater config, the
    last check's answer out of `channel.json`, the build half out of
    `build.json`. `checked` is in the answer because a window that renders a
    stale verdict as a live one is the lie this shape exists to prevent.
    """
    from . import build_source, sourcestamp
    root = fleet.root()
    config = fleet.config()
    try:
        status = json.loads((root / "channel.json").read_text())
    except (OSError, ValueError):
        status = {}
    running = sourcestamp.capture()
    refusal = build_refusal(root, config)
    return {
        "ref": build_source.channel_ref(config),
        "repository": config.get("github_repo", ""),
        "enabled": (root / "config.json").exists(),
        "managed": managed_access.is_leaf(),
        # The same four keys `/host` reports its source with, so the window
        # renders a build here exactly as it renders one there.
        "running": {"release": running.get("release", ""), "sha": running.get("sha", ""),
                    "version": running.get("version", ""), "dirty": bool(running.get("dirty"))},
        "check": {"status": status.get("status", "unknown"),
                  "head": status.get("head", ""),
                  "checked": status.get("checked", 0),
                  "detail": status.get("detail", "")},
        "build": build_source.phase(root),
        "can_build": not refusal,
        "blocked": refusal,
    }


def _config() -> dict:
    """The updater config, with an unreadable one answered as an outage rather
    than as a traceback — every door below reads it before it decides."""
    try:
        return fleet.config()
    except (ReleaseError, ValueError, OSError) as exc:
        raise HTTPException(503, str(exc)) from exc


def build_refusal(root, config: dict) -> str:
    """`build_source`'s own rule, with "not set up yet" folded in."""
    from . import build_source
    if not (root / "config.json").exists():
        return "updates are not enabled on this host"
    try:
        return build_source.build_refusal(root, config)
    except ReleaseError as exc:
        return str(exc)


class SourceRef(BaseModel):
    #: Not optional and not empty. `channel_ref` reads an empty name as
    #: `stable`, which is right for a config key absent since before a hub
    #: could choose — and wrong for a press, where it would silently move the
    #: machine to main.
    ref: str = Field(min_length=1, max_length=128)


@router.get("/updates/source")
def source(request: Request, device_id: str = Depends(current_device)):
    local_admin(request, device_id)
    try:
        return _source()
    except (ReleaseError, ValueError, OSError) as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/updates/source/ref")
def set_source(body: SourceRef, request: Request, device_id: str = Depends(current_device)):
    """Follow a different ref. Pulls nothing and builds nothing.

    The name is validated by `build_source.channel_ref`, which is what the CLI
    verb validates with — one definition of a followable ref, so a name the
    terminal accepts and this refuses cannot exist.
    """
    local_admin(request, device_id)
    from . import build_source
    from .update_supervisor import atomic_json
    path = fleet.root() / "config.json"
    config = _config()
    if not path.exists():
        raise HTTPException(409, "updates are not enabled on this host")
    if managed_access.is_leaf():
        raise HTTPException(409, "a managed machine runs what its parent built")
    try:
        name = build_source.channel_ref({"channel": body.ref})
    except ReleaseError as exc:
        raise HTTPException(400, f"{exc}: {body.ref!r}") from exc
    atomic_json(path, {**config, "channel": name})
    return _source()


def _run_build(root, config: dict) -> None:
    from . import build_source
    from .update_supervisor import atomic_json
    try:
        build_source.build(root, config)
    except Exception as exc:
        # `build()` records its own failures. This covers the ones it raises
        # before it starts recording, which would otherwise leave the marker
        # written below saying "building" until the six-hour stall window.
        if build_source.phase(root).get("state") == "building":
            atomic_json(root / "build.json",
                        {"state": "failed", "detail": str(exc), "finished": time.time()})


@router.post("/updates/source/build")
def build_source_now(request: Request, device_id: str = Depends(current_device)):
    """Build the followed ref. Answers now; the work outlives the request.

    The `building` marker is written here rather than left to the worker: the
    caller reads the phase back immediately, and a marker written by a thread
    that has not been scheduled yet is a window reporting "idle" over a build
    already in flight.
    """
    local_admin(request, device_id)
    from . import build_source
    from .update_supervisor import atomic_json
    root, config = fleet.root(), _config()
    refusal = build_refusal(root, config)
    if refusal:
        raise HTTPException(409, refusal)
    with _build_gate:
        if build_source.phase(root).get("state") == "building":
            raise HTTPException(409, "a build is already running on this hub")
        atomic_json(root / "build.json",
                    {"state": "building", "ref": build_source.channel_ref(config),
                     "started": time.time()})
    threading.Thread(target=_run_build, args=(root, config),
                     name="jstack-source-build", daemon=True).start()
    return _source()


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
