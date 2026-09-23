"""jRemote API router — `/api/jremote/v1/*`.

Every route is gated by the bearer-token dependency. Mounted into the
dashboard FastAPI app; see dashboard/app.py.
"""

import asyncio
import contextlib
import ipaddress
import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .auth import current_device, require_token
from . import board, devices, docfence, hostenv, plugin_paths
from . import managed_access
from .turns import stream_turn, TurnError
from .messages import _blocks_to_segments, _flatten, _is_noise

@contextlib.asynccontextmanager
async def _host_lifespan(_app: FastAPI):
    """Package-owned background work shared by embedded and standalone hosts."""
    from . import fileshare
    task = asyncio.create_task(fileshare.audit_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


router = APIRouter(prefix="/api/jremote/v1", dependencies=[Depends(require_token)],
                   lifespan=_host_lifespan)

_SID_RE = re.compile(r"^[0-9a-f-]{32,40}$")

# The jStack dub adapter — the one implementation of "verbatim-fork a session's
# transcript"; the splitoff endpoint shells out to it rather than porting the
# logic. Resolved at import, so a test pointing HOME elsewhere still finds it
# while the dub itself (which reads $HOME) operates on the test tree.
_DUB_SESSION = plugin_paths.jstack_bin("dub-session")


def _check_sid(sid: str):
    if not _SID_RE.match(sid):
        raise HTTPException(status_code=400, detail="invalid session id")


# ── Features this host may not have ──

# Two screens are drawn from machinery only some hosts carry — the context
# inventory and the control tier's daemon buttons. The other three (allowance
# meters, token spend, the day feed) are this package's own readers, importable
# everywhere the package is; what varies for them is whether the machine has
# anything to show, and each one answers that itself (`available()`, an empty
# scan) rather than through the import system.
#
# Absence is a first-class answer, not an error. Each endpoint below returns its
# normal shape with `available: false` and an empty body, so:
#   - the app can tell "this host does not have a timeline" from "the day was
#     quiet" — an empty feed on a host that HAS no feed is a lie that looks
#     like a slow afternoon,
#   - a client that predates the flag ignores the extra key and draws an empty
#     screen, which is wrong but not broken, and never a 500.
#
# Imported at call time, one module per feature: a top-level import would cost
# the whole API, and one chain would hide the rest behind its first casualty.

def _optional(dotted):
    """Import a feature module, or None on a host that does not have it.

    ImportError only. A module that is present but raises while loading is a
    real fault on a machine that is supposed to have it, and swallowing that
    would turn a broken install into a permanently greyed-out screen with
    nothing anywhere saying why.
    """
    if not dotted:
        return None
    try:
        return __import__(dotted, fromlist=["_"])
    except ImportError:
        return None


def _unavailable(feature: str, **shape) -> dict:
    """The honest payload: the screen's own keys, empty, plus why."""
    return {"available": False,
            "reason": f"{feature} is not available on this host", **shape}


def _is_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def _hub_console(request: Request) -> bool:
    """The caller is the hub's own menu bar — on loopback, on a machine that is
    the hub. This is the authority for device management, and nothing else is.

    Adding, removing and listing devices belongs to the hub menu bar
    alone. A remote (mesh or LAN) is not it and sees only its own row; a leaf's
    own loopback is not it either, because a leaf is not a hub and has no devices
    of its own. `mode.is_hub()` is the same predicate `/host` publishes as
    `device_management`, so the gate on the routes and the flag the app draws
    from cannot disagree."""
    return managed_access.console(request)


#: The optional screens, and the module each one needs. One place, so the
#: `/host` summary below and the guards on the routes themselves cannot drift
#: into disagreeing about what this machine can do.
_FEATURES = {
    "context": "dashboard.shared.context_inventory",
    # These three are this package's own readers, drawing on what a jStack
    # machine already records — Claude Code's own files, the timeline store,
    # the scheduler journal, the checkouts. Import answers only "is the
    # package intact"; whether there is anything to show is each reader's own
    # answer, probed where the route serves it.
    "usage_caps": "jstack_host.allowance",
    "usage_spend": "jstack_host.spend",
    "feed": "jstack_host.feed",
    # The control tier is the embedding host's own machinery; only its
    # profile can name the module that drives it. '' = no such tier.
    "control": hostenv.control_module(),
}


def _file_sharing_available() -> bool:
    from . import fileshare
    return fileshare.serves_files()

def _tags_available() -> bool:
    from . import timeline
    return timeline.available()


def _tunnel_pairing_available() -> bool:
    from . import tunnel
    return tunnel.can_pair()


def _shell_access_available() -> bool:
    from . import shell_access
    return bool(shell_access.public_key())


def _usage_caps_available() -> bool:
    # The same two conditions `/usage/caps` itself answers with. Importability
    # alone said True on any machine with the package installed — while the
    # route, which also asks `allowance.available()`, said False wherever no
    # provider sample and no CLI cache exist to draw. A capability map that
    # disagrees with its own screen is the thing `/host` exists to prevent.
    mod = _optional(_FEATURES["usage_caps"])
    return mod is not None and _has_allowance(mod)


#: Features backed by something other than an importable module, probed the way
#: they are actually used. Tags come from jStack's `log_event` binary, so
#: `_optional()` — which asks the import system — could only ever answer for the
#: wrong thing. Merged into the same `/host` summary so a client still reads one
#: capability map, and each probe stays the same call the route itself makes.
#: `tunnel_pairing` is here rather than in `_FEATURES` because the module always
#: imports — what it needs is the hub's `wg_peer.py` beside it, which only the
#: machine that owns the mesh has.
#: `shell_access` is probed off the machine's own minted identity — the fact
#: that decides whether `ssh` into or out of here can work at all.
_PROBED_FEATURES = {"tags": _tags_available,
                    "tunnel_pairing": _tunnel_pairing_available,
                    "usage_caps": _usage_caps_available,
                    "file_sharing": _file_sharing_available,
                    "shell_access": _shell_access_available}


def _probe(name: str) -> bool:
    try:
        return bool(_PROBED_FEATURES[name]())
    except Exception:
        return False


@router.get("/host")
def get_host(request: Request):
    """Which machine this is, and what it can do — one call, before any screen.

    **Identity, because a URL is not one.** The same Mac is a `.local` name on
    the LAN, an in-tunnel address from a cafe, and `127.0.0.1` to an app
    running on it; and two different Macs are each `127.0.0.1` to their own
    app. The grid needs to hold one row per *machine* across all of that, and
    local-first routing needs to know that the host answering on loopback is
    the host it was asked for — otherwise an app configured for another host
    and running on this one quietly draws the wrong board, every session real
    and none of them the ones asked for.

    Behind the token deliberately. The token is the second half of the proof:
    a loopback host that rejects the configured host's token is not that host,
    whatever id it would have claimed.

    **Capabilities, so the grid can draw a machine before entering it.** The
    same answer the individual endpoints give, hoisted: a client that asks each
    screen in turn learns the same thing four round trips later, and has to
    render four spinners to find out one of them was never coming.
    """
    from . import addresses, mode, sourcestamp
    # The port the caller actually reached, not a constant: a host moved off
    # 9090 would otherwise hand out an address list that is wrong in the one
    # detail nobody checks, on the screen whose whole job is that address.
    port = request.url.port or addresses.DEFAULT_PORT
    # The mesh address does not get that number. It is served by this
    # process's own listener, so its port is ours, not the caller's route to
    # us — and a caller arriving through a forward reaches a port nothing
    # serves on the mesh. `scope["server"]` is the socket this app is bound
    # to, which no forward and no proxy header can rewrite.
    bound = (request.scope.get("server") or (None, None))[1] or port
    return {
        "host_id": hostenv.host_id(),
        "source": sourcestamp.capture(),
        "name": hostenv.host_name(),
        "profile": hostenv.profile().name,
        # Where a SECOND machine should try. Never loopback — see addresses.py.
        "addresses": addresses.reachable(port, mesh_port=bound),
        # local / open / managed — the same verdict `jstack-host mode` prints,
        # hoisted here so the menu bar draws the mode from the one call it
        # already makes rather than a route of its own.
        "mode": mode.current(),
        # `device_management` is caller-aware, not host-level like the rest:
        # true only for the hub's own menu bar (loopback on the hub), so the
        # menu bar draws its administrative controls. Client apps never own
        # device management, even when they happen to run on this Mac.
        "features": {**{k: _optional(m) is not None for k, m in _FEATURES.items()},
                     **{k: _probe(k) for k in _PROBED_FEATURES},
                     "device_management": _hub_console(request),
                     "managed_host": managed_access.is_leaf(),
                     "self_disconnect": managed_access.can_disconnect(
                         getattr(request.state, "authorized_device", ""))},
    }


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


class AgentEngineBody(BaseModel):
    """A partial write: name only what changes.

    `models` is keyed by engine (`{"codex": "gpt-5.6-terra"}`) rather than
    being a single field, because the long-press menu can launch EITHER engine
    for an agent — so each one needs its own remembered model, not one shared
    slot that the last picker to be touched would overwrite.

    Declared up here, not beside the other Body models: annotations are
    evaluated when the route is defined, and this route sits in the read
    surface at the top of the file."""
    engine: str | None = None
    models: dict[str, str] | None = None


# ── Read surface ──

@router.get("/agents")
def get_agents():
    """The agent cards, plus `seats` — the counts and pulse for the seats that
    have a card, and only those.

    Not a catalogue of what exists. This is the fifteen-second backstop poll
    (`RootView.pollSeconds`), so everything on it is re-sent four times a
    minute per device: it carries what changes, which is liveness, and nothing
    that doesn't, which is the tree. A card's label and emoji are the device's
    own synced user meta, and the picker discovers directories live through
    `/agents/{id}/tree`, so no list of seats needs to ride here at all.

    `agents` is unchanged, so a build that predates shortcuts reads this
    payload exactly as it read the old one."""
    return board.roster()


@router.get("/agents/{agent_id}/tree")
def get_agent_tree(agent_id: str, path: str = ""):
    """One directory of an agent's tree, for the shortcut picker.

    Always rooted at the agent's own umbrella dir and confined to it. Each row
    says `is_seat` — whether it holds a CLAUDE.md — because that is the only
    thing that makes a directory pickable: a card minted onto a directory with
    no CLAUDE.md would spawn a session with no identity, no rules and no seat
    timeline. `has_children` is what makes a non-seat row still worth entering.
    """
    from . import seats
    try:
        return seats.browse(agent_id, path)
    except KeyError:
        raise HTTPException(404, f"no such agent or path: {agent_id!r} {path!r}")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/agents/{agent_id}/commands")
def get_commands(agent_id: str):
    from .commands import list_commands
    return {"commands": list_commands(agent_id)}


@router.get("/engines")
def get_engines():
    """The spawnable engines and each one's models — what the app renders its
    provider menu and model pickers from.

    Served rather than shipped in the app on purpose: a model added to the
    roster here reaches every device on the next fetch, where a hardcoded
    Swift list would need an App Store build to add one and would meanwhile
    offer models the CLI refuses."""
    from . import engines
    return engines.roster()


@router.get("/tags")
def get_tags():
    """The timeline's subject vocabulary — what a session can be pinned ON.

    Served, not shipped, for the same reason the engine roster is: the
    vocabulary is minted on the Mac and changes without an App Store build.

    Sorted by use, busiest first, exactly as `log_event tag list` prints it —
    a picker's job here is to steer the next pin toward a tag that already
    means something, and alphabetical order would bury the four tags that
    carry the work under whatever starts with 'a'.

    Absent, not empty, on a host with no jStack: "nobody has minted a tag" and
    "this machine cannot see tags" are different answers, and a picker that
    drew zero rows for the second would be a dead screen with nothing saying
    why."""
    from . import timeline
    if not timeline.available():
        return _unavailable("the timeline", tags=[])
    return {"tags": timeline.tags()}


@router.get("/agents/{agent_id}/engine")
def get_agent_engine(agent_id: str):
    """This agent's default engine and its model per engine — fully resolved,
    so a device with nothing stored still renders exactly what the host would
    spawn."""
    from . import engines
    return engines.defaults(agent_id)


@router.post("/agents/{agent_id}/engine")
def set_agent_engine(agent_id: str, payload: AgentEngineBody):
    """Set either part, or both. Unknown engine/model is a 400 — a stored
    preference that cannot be spawned would fail at the one moment the user is
    waiting on a session to come up."""
    from . import engines
    try:
        return engines.set_defaults(agent_id, payload.engine, payload.models)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/context")
def get_context():
    """Skills, rules and systems — what a session loads, and what runs.

    One payload rather than three routes: the screen shows all three tabs at
    once and the whole inventory is a few tens of KB, so a single round trip
    beats three that can disagree with each other mid-refresh.
    """
    ci = _optional("dashboard.shared.context_inventory")
    if ci is None:
        return _unavailable("the context inventory", skills=[], rules=[],
                            systems=[], seats=[], drift=[], summary={})
    return ci.inventory()


@router.get("/context/file")
def get_context_file(path: str):
    """The text behind one row — a rule, a skill, a SYSTEM.md, a CLAUDE.md.

    Fenced to the roots in `docfence`, and to markdown. The screen only ever
    asks for a path the same payload just handed it, but this is a
    token-authenticated read route on a machine that also stores credentials:
    the fence has to live here, not in the caller's good manners.

    Served by the package rather than by the optional inventory above, and not
    guarded by `_optional`. *Listing* what a session loads is the embedding
    host's knowledge; reading one fenced markdown file is not, and a host that
    503'd here would accept a `jremote://doc` link — `showdoc` fences the path
    and opens the window — and then fail to hand back the file the window was
    opened onto. The link route and the read route have to agree about the
    fence, so they ask the same module.
    """
    try:
        return docfence.file_text(path)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/files/share")
def get_file_share():
    """The host's observed SMB state; mutation remains a local root action."""
    from . import fileshare
    try:
        return fileshare.status()
    except fileshare.FileShareError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/sessions")
def get_sessions(agent: str | None = None, shortcut: str | None = None):
    """`shortcut` is a shortcut's id: the sittings THAT card opened, rather
    than everything its seat has ever run. A shortcut carries a config, so two
    of them onto one seat are two threads of work."""
    return {"sessions": board.list_sessions(agent, shortcut)}


@router.get("/sessions/open")
def get_open_sessions():
    """All currently-open managed sessions across agents — for the board."""
    return {"sessions": board.open_sessions()}


@router.get("/sessions/active")
def get_active_sessions():
    """All active chats (live process and/or managed terminal) across agents —
    the app's main-page Active section. Must stay declared before /sessions/{sid}."""
    return {"sessions": board.active_sessions()}


@router.get("/usage/caps")
def get_usage_caps():
    """How much headroom each AI provider has left — the app's Usage section.

    Not spend. `/usage/spend` answers "what did today cost"; this answers
    "can work happen at all right now", off a provider-side allowance we
    cannot derive from our own transcripts. The two are never merged
    (`allowance.py`).

    Every known provider is a key, and a provider that has never reported is
    `null` — the app draws "not connected" for it. A zeroed meter on an
    unmeasured provider would claim full headroom, which is the single answer
    a headroom meter exists to prevent. Each provider that HAS reported
    carries `age_seconds` + `stale`, and the app is expected to show the age
    rather than present a cold number as current.
    """
    usage_caps = _optional(_FEATURES["usage_caps"])
    if usage_caps is None or not _has_allowance(usage_caps):
        # `providers: {}` rather than the known-provider roster with nulls: on a
        # host with no allowance reporting there is no roster either, and a
        # provider list drawn from the dashboard's config would invent connections this
        # machine has never had.
        return _unavailable("provider allowance", providers={},
                            thresholds={}, stale_after_seconds=None,
                            generated_at=None)
    return usage_caps.read()


def _has_allowance(mod) -> bool:
    """The reader answers `available()` — a host with no sample on file and
    no CLI cache has nothing to draw, and the screen must say so rather than
    show two empty bars. Guarded with getattr so a module without the probe
    simply counts as present."""
    probe = getattr(mod, "available", None)
    return True if probe is None else bool(probe())


def _wire_day(d: dict) -> dict:
    """One day of `token_usage.daily()`, trimmed to what the phone draws.

    The dashboard's own card carries a per-category agent split and the day's
    most expensive sessions. This payload rides the board poll to a phone and
    draws neither, so the trim is deliberate rather than incidental — growing
    it back by accident is a regression nothing else would notice."""
    return {
        "day": d["day"],
        "total": d["total"],
        "output": d["output"],
        "sessions": d["sessions"],
        "autonomous_pct": d["autonomous_pct"],
        "catch_all_sessions": d["catch_all_sessions"],
        "categories": [
            {
                "category": c["category"],
                "label": c["label"],
                "kind": c["kind"],
                "pct": c["pct"],
                "total": c["total"],
            }
            for c in d["categories"]
        ],
    }


@router.get("/usage/spend")
def get_usage_spend(days: int = 7):
    """What today cost and which job spent it — the app's Spend section.

    The other half of the pair, and deliberately a separate route from
    `/usage/caps`: caps answer "can work happen at all right now" off a
    provider-side allowance, this answers "what did the day cost" off our own
    transcripts. They are never merged into one number, because a figure that
    looked like both would be trusted as neither (`spend.py`).

    Same breakdown the dashboard's Token Usage card draws, off the same
    `daily()` — one classification, one set of percentages, so the phone and
    the Mac can never disagree about what a job cost. Trimmed for the wire by
    `_wire_day`, never recomputed.

    **Every day in the window carries its own full breakdown, not just a
    total.** The app's trend is tappable: selecting a bar re-labels the whole
    section to that day. Serving the breakdown for one day and a bare total for
    the rest would make each tap a round trip, on a section that exists to be
    glanced at — and the extra days cost nothing, because they are aggregations
    of the SAME scan the day already needed. The top-level fields are today's,
    unchanged, so a build predating the tappable trend reads this payload
    exactly as it did before.

    A week by default, which is the app's window: seven bars across a phone are
    wide enough to hit with a thumb, where a fortnight's were not.

    `total` is all four token counters summed — input, both cache counters, and
    output — which is what the session actually moved. Reporting input+output
    alone would hide the entire cost structure, since a long session re-sends
    its whole context every turn.

    `catch_all_sessions` is carried rather than swallowed: a non-zero count
    means a job type is missing from `config/token_categories.json` and its
    spend is sitting under a label that does not describe it.
    """
    from collections import defaultdict
    tu = _optional(_FEATURES["usage_spend"])
    if tu is None:
        # No zeroed totals. This host may well be spending tokens — it just has
        # no scanner that can say how much — and a `total: 0` would report the
        # one number the screen exists to show as if it had been measured.
        return _unavailable("token spend", series=[], day=None, total=None,
                            output=None, sessions=None)

    records = tu.scan()
    # Bucketed once, so aggregating fourteen days costs one pass over the scan
    # rather than fourteen. `daily` re-filters what it is handed, so a
    # pre-filtered bucket is the same call with the search already done.
    rows: "dict[str, list]" = defaultdict(list)
    for r in records:
        rows[r["day"]].append(r)

    window = [_wire_day(tu.daily(s["day"], rows[s["day"]]))
              for s in tu.series(days, records)]
    day = tu.today()
    # `series` always ends today, so this is normally the last entry — resolved
    # by name rather than by position so `days=0` (or a clock that rolls over
    # mid-call) still answers with today rather than with whatever is last.
    today = next((d for d in window if d["day"] == day), None)
    if today is None:
        today = _wire_day(tu.daily(day, rows[day]))
    return {**today, "series": window}


@router.get("/app/mac/latest")
def app_mac_latest():
    """The Mac app's update feed — what the newest published build is.

    The app is a thin client of this API and is useless without it, so its
    updates ride the same authenticated connection rather than a second
    channel: any Mac that can run jRemote can update jRemote, at home, through
    the tunnel, or from a machine that has never seen this LAN.

    `available` is whether there is a build to take. `publishes` is whether
    this machine is a place builds come from at all, and the two are not the
    same answer: most hosts are somebody's laptop or desktop running the
    standalone host, and they have never built anything. Both used to say
    `{"available": false}`, which the app read as "up to date" — so a Mac that
    was itself a host asked the one machine that could not know, was told it
    was current, and stopped updating. Absence is a first-class answer here
    like everywhere else in this router.

    A release that IS published but whose artifact is missing or the wrong
    size is a 500 — the app would otherwise download it, fail its own
    checksum, and retry forever with nothing on the Mac saying why.
    """
    from . import releases
    try:
        latest = releases.latest()
    except releases.ReleaseError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if latest is None:
        return {"available": False, "publishes": releases.publishes()}
    return {"available": True, "publishes": True, **latest}


@router.get("/app/mac/download")
def app_mac_download():
    """The published zip. Checked against its manifest before it is served —
    the same gate as `/latest`, so a half-published release can never be
    handed to a Mac that would then replace a working app with it."""
    from . import releases
    try:
        latest = releases.latest()
        if latest is None:
            raise HTTPException(status_code=404, detail="no Mac release published")
        manifest = releases._read_manifest()
        path = releases.artifact_path(manifest)
    except releases.ReleaseError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return FileResponse(path, media_type="application/zip", filename=path.name)


# ── devices: the per-token registry behind the bearer gate ──

class DeviceMintRequest(BaseModel):
    name: str
    #: The physical device's stable, app-generated identity (the iOS app keeps a
    #: UUID in the Keychain and presents it here). Optional — an app that
    #: predates the field sends none. When present it keys the row, so re-pairing
    #: the same device rotates its one credential instead of minting a duplicate.
    identity: str | None = None


class DeviceRenameRequest(BaseModel):
    name: str


def _device_name(raw: str) -> str:
    name = " ".join((raw or "").split())[:60]
    if not name:
        raise HTTPException(status_code=400, detail="a device needs a name")
    return name


# The device identity is machine-minted (a Keychain UUID), never human-typed, so
# its alphabet is narrow on purpose: a value outside it is a mangled paste or a
# probe, not a device. Bounding length and charset keeps a SQL wildcard, a
# newline or an unbounded blob out of the column that now keys the table.
_IDENTITY_RE = re.compile(r"\A[A-Za-z0-9._:-]+\Z")


def _device_identity(raw: str | None) -> str | None:
    """A client-supplied device identity, validated or refused.

    Absent or empty stays None — identity is optional and a row keyed on nothing
    is exactly the pre-field behavior. A present value is length- and
    charset-checked; anything malformed is a 400, the same shape `_device_name`
    raises, rather than something that reaches the store."""
    if raw is None:
        return None
    identity = raw.strip()
    if not identity:
        return None
    if len(identity) > 128 or not _IDENTITY_RE.match(identity):
        raise HTTPException(status_code=400, detail="invalid device identity")
    return identity


@router.get("/devices")
def list_devices(request: Request, device_id: str = Depends(current_device)):
    """The device registry. To the hub console it is the audit surface — every
    device, revoked ones included, nothing hidden. `current` marks the caller's
    own row so the app can label "this device".

    Off the hub console a caller sees ONLY its own row. A remote is a device, not
    an administrator of them, and must not enumerate the others. Enforced here, not in the
    app: the app hiding the screen is polish, the host refusing the roster is the
    boundary."""
    rows = devices.list_all()
    if managed_access.is_leaf():
        return {"devices": []}
    if not _hub_console(request):
        rows = [r for r in rows if r["id"] == device_id]
    for r in rows:
        r["current"] = r["id"] == device_id
    return {"devices": [{k: v for k, v in row.items()
                         if k not in ("token_hash", "authority_grant", "authority_device")}
                        for row in rows]}


@router.post("/devices")
def mint_device(body: DeviceMintRequest, request: Request):
    """Mint a new device token — a HUB MENU BAR action only.

    Adding a device is the hub's alone, so the gate is the hub's
    own console (loopback on the hub), not merely the LAN it used to be. A remote
    or a leaf is refused: a leaf has no devices of its own, and a remote is a
    device, not a minter of them. The token appears in this response and nowhere
    else, ever — the table keeps only its hash."""
    if not _hub_console(request):
        raise HTTPException(
            status_code=403,
            detail="adding a device is a hub menu-bar action — do it on the hub Mac")
    row, token = devices.mint(_device_name(body.name),
                              _device_identity(body.identity))
    row["revoked"] = False
    return {"device": row, "token": token}


@router.post("/devices/{device_id}/rename")
def rename_device(device_id: str, body: DeviceRenameRequest, request: Request,
                  caller: str = Depends(current_device)):
    """Device labels, like membership, are managed at the hub console."""
    managed_access.require_console(request)
    if not devices.rename(device_id, _device_name(body.name)):
        raise HTTPException(status_code=404, detail="unknown device")
    return {"renamed": device_id}


@router.post("/devices/{device_id}/revoke")
def revoke_device(device_id: str, request: Request,
                  caller: str = Depends(current_device)):
    """Revoke a device — from this moment its token opens nothing, and its
    live connections (PTY terminal, SSE streams) are cut, not left to drain.
    Idempotent-safe: revoking an already-revoked device answers 404 and
    changes nothing.

    A caller may revoke ITSELF from anywhere — that is the remote's one device
    action, "disconnect this device". Revoking ANOTHER device is
    a hub menu-bar action: a remote must not reach across and cut a device that
    is not it ("It should NOT ... kill other DEVICES"). Enforced here so the
    refusal holds whatever the app shows."""
    if not _hub_console(request) and (
            device_id != caller or not managed_access.can_disconnect(caller)):
        raise HTTPException(
            status_code=403,
            detail="a device can disconnect only itself; removing another device is a hub menu-bar action")
    if not devices.revoke(device_id):
        raise HTTPException(status_code=404, detail="unknown or already revoked device")
    return {"revoked": device_id, "self": device_id == caller}


class TunnelPairRequest(BaseModel):
    device: str


@router.post("/tunnel/pair")
def tunnel_pair(body: TunnelPairRequest, request: Request):
    """Pair the calling device for off-LAN access and hand back its config.

    LAN-only by design — see `tunnel.py`. The refusal is a 403 with the reason
    in plain words, because the caller is the user's own app and the fix ("get on
    the same Wi-Fi") is something it can tell them.

    A host that does not run the tunnel answers **503, not 500** — the same
    distinction `/control/{action}` draws. Nothing here is broken and no fix
    exists on this machine: it is a leaf, it dials out, and it has no peers to
    mint. A 500 sent the app's user chasing a missing file instead.
    """
    from . import tunnel
    client_ip = request.client.host if request.client else ""
    try:
        return tunnel.pair(body.device, client_ip)
    except tunnel.PairingUnsupported as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except tunnel.PairingRefused as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except tunnel.TunnelError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── enrolment: a credential for a machine that can never be on the LAN ──
#
# `unauthenticated_router` carries exactly ONE route, and it is the only route
# in this package outside the bearer gate besides `/api/health`. It exists
# because redemption cannot be authenticated by definition — the caller is a
# machine that has no token yet, and getting one is what it is here for. The
# code IS the credential; everything protecting it lives in enrolment.py.
#
# Nothing else goes on this router. A route added here by reflex is a route
# with no auth at all, on an API that drives agents.
unauthenticated_router = APIRouter(prefix="/api/jremote/v1")


class EnrolmentCodeRequest(BaseModel):
    name: str
    ttl_seconds: int | None = None
    #: 'device' or 'host'. A host code also writes the enrolled machine into
    #: the synced `hosts` registry, so every device mirroring this host learns
    #: the machine exists. Fixed here at mint and never asserted at redemption.
    kind: str = "device"


class EnrolmentRedeemRequest(BaseModel):
    code: str
    #: The redeeming machine's own `/host` id, sent whenever it has one. It is
    #: the CODE that decides whether this matters; a device leaves it empty.
    host_key: str = ""
    port: int = 9090
    #: The credential this device already holds here, when it is pairing again
    #: rather than for the first time — the host re-keys that row instead of
    #: leaving a second one behind (enrolment.py, RE-PAIRING). In the body and
    #: not an `Authorization` header on purpose: this route is unauthenticated
    #: and must stay that way to a reader, and a bearer header on it would read
    #: like auth that had been added.
    device_token: str = ""
    #: A credential the redeeming MACHINE minted on itself and is handing over,
    #: so this host can mint device tokens there for devices it already trusts
    #: (grants.py). Only meaningful alongside a host code; optional always.
    grant_token: str = ""
    #: The redeeming app's stable per-install id (`AppInstance.id`). Sent so a
    #: device that re-pairs without carrying its old token still lands on its
    #: EXISTING row instead of minting a new one — the fix for a devices list
    #: that grows a fresh duplicate on every reconnect. Empty from an older app,
    #: which mints a fresh row exactly as before.
    identity: str = ""


class EnrolmentRevokeRequest(BaseModel):
    code: str


class HostRenameRequest(BaseModel):
    name: str


@router.post("/enrolment/codes")
def mint_enrolment_code(body: EnrolmentCodeRequest, request: Request,
                        device_id: str = Depends(current_device)):
    """Issue an invitation from the authenticated hub console only.

    The joining device spends the one-time code from any network. Only the
    host decides membership; a remote client cannot create invitations.
    """
    from . import enrolment
    if not _hub_console(request):
        raise HTTPException(
            status_code=403,
            detail="adding or adopting a machine is a hub menu-bar action — do it on the hub Mac")
    if body.kind not in enrolment.KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"kind must be one of {', '.join(enrolment.KINDS)}")
    return enrolment.mint_code(_device_name(body.name), device_id,
                               body.ttl_seconds or enrolment.DEFAULT_TTL,
                               body.kind)


@router.get("/enrolment/codes")
def list_enrolment_codes(request: Request,
                         device_id: str = Depends(current_device)):
    """Outstanding and spent codes — who minted each, and what redeemed it.
    Never the code or its digest.

    A HUB MENU BAR view: the outstanding-codes list is the roster of pending
    device additions, and enumerating them is the same authority as minting one
    — the hub's. A remote is a device, not an administrator of the estate's
    pairings, and gets nothing here."""
    from . import enrolment
    if not _hub_console(request):
        raise HTTPException(
            status_code=403,
            detail="listing enrolment codes is a hub menu-bar action — do it on the hub Mac")
    return {"codes": enrolment.list_codes()}


@router.post("/enrolment/codes/revoke")
def revoke_enrolment_code(body: EnrolmentRevokeRequest, request: Request,
                          device_id: str = Depends(current_device)):
    """Withdraw an unused code, named by the code itself — a HUB MENU BAR action.
    Cancelling a pending device addition is the hub's authority, the same as
    minting it; a remote cannot reach into the estate's outstanding pairings."""
    from . import enrolment
    if not _hub_console(request):
        raise HTTPException(
            status_code=403,
            detail="revoking an enrolment code is a hub menu-bar action — do it on the hub Mac")
    if not enrolment.revoke(body.code):
        raise HTTPException(status_code=404,
                            detail="no unused code matches that")
    return {"revoked": True}


@unauthenticated_router.post("/enrolment/redeem")
def redeem_enrolment_code(body: EnrolmentRedeemRequest, request: Request):
    """Spend a code for this machine's own device token, and its peer config
    where the host owns a mesh.

    401 for every bad code, whatever made it bad — an endpoint that told a
    guesser which of unknown/expired/already-used it hit would be an oracle.
    429 carries the lockout the two-tier limiter is holding. **400 is the one
    specific answer**, and it is safe to be specific because it is decided from
    the request alone, before this host has looked the code up at all.
    """
    from . import enrolment
    if managed_access.is_leaf() and not _is_loopback(request.client.host if request.client else ""):
        raise HTTPException(403, "pair devices at the parent hub")
    client_ip = request.client.host if request.client else ""
    try:
        return enrolment.redeem(body.code, client_ip,
                                body.host_key, body.port, body.device_token,
                                body.grant_token, body.identity,
                                parent_port=request.url.port or enrolment.DEFAULT_PORT)
    except enrolment.HostKeyRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except enrolment.EnrolmentLockedOut as exc:
        raise HTTPException(status_code=429, detail=str(exc),
                            headers={"Retry-After": str(exc.seconds)})
    except enrolment.EnrolmentError as exc:
        raise HTTPException(status_code=401, detail=str(exc))


# ── hosts: the machines this one has let in ──
#
# Management only. Devices READ this table through `/sync` and cannot write it:
# `SyncPush` has no `hosts` field and `apply_push` no branch for one, so every
# change to it comes through a route that proved a bearer token first. A device
# that could push a host row could invent a machine.

def _serve_host(row: dict, delegated: bool = False, *, policy: bool = False) -> dict:
    # Shell material stays off the device wire: the pubkey and account are
    # public halves, but devices have no use for them and the console does.
    public = {k: v for k, v in row.items()
              if k not in ("device_id", "sees_home", "sees_leaves",
                           "shell_pubkey", "shell_user")}
    if policy:
        from .store import get_store
        public.update(sees_home=bool(row.get("sees_home", True)),
                      sees_leaves=bool(row.get("sees_leaves", True)),
                      shell_user=row.get("shell_user", ""),
                      shell_sources=get_store().shell_sources_for(row["key"]))
    return {**public, "deleted": bool(row["deleted"]), "delegated": delegated,
            "managed_access": True}


def _leaf_parent_row() -> dict | None:
    """This leaf's parent, as a host row a device can tile — or None on a
    machine that has no parent or holds no live grant for one.

    The #62 direction. A hub lists the machines it adopted and hands its
    devices access to them; a leaf's devices want the same reach the other
    way — to the one machine that adopted THIS one. That machine is not in this
    host's `hosts` table (it did not adopt the parent), so it is not synced to
    devices and cannot mirror as a blind tile. It is synthesised here, from the
    identity `attach` recorded and only while a grant actually backs it, so the
    row a device sees is always one it can spend: `delegated` is true because
    the row does not exist otherwise. A parent whose grant was revoked, or a
    machine that was never attached, simply has no row — the same silence a
    forgotten host gets, for the same reason.
    """
    from . import attach_parent, grants
    rec = attach_parent.parent_record()
    key = (rec.get("parent_key") or "").strip()
    if not key or not grants.held(key):
        return None
    return {"key": key, "name": rec.get("parent_name") or "home",
            "address": rec.get("parent_address") or "",
            "port": int(rec.get("parent_port") or 9090), "deleted": 0}


@router.get("/hosts")
def list_hosts(request: Request, device_id: str = Depends(current_device)):
    """The machines this host has enrolled. Forgotten ones are excluded — the
    tombstone exists for the mirror, not for the reader.

    `delegated` rides with each row because the tile and the access are two
    different facts and the row that looks fine is the one that lies: a machine
    enrolled by an older build has a tile every device shows and no grant behind
    it, so asking for access 502s at the moment somebody taps it. A caller that
    can see the machine can see whether it will let them in.

    From `host_grants`, which never rides `/sync` — so this is a boolean
    computed on the way out and not a column any device can mirror, let alone
    push back.
    """
    from . import grants
    from .store import get_store
    if managed_access.is_leaf():
        if not _is_loopback(request.client.host if request.client else ""):
            return {"hosts": []}
        return managed_access.visible_hosts()
    live = {h["host_key"] for h in grants.holdings() if h["revoked_at"] is None}
    rows = [_serve_host(r, r["key"] in live, policy=_hub_console(request))
            for r in get_store().list_hosts()
            if managed_access.may_reach(device_id, r["key"])]
    return {"hosts": rows}


@router.post("/hosts/{key}/rename")
def rename_host(key: str, body: HostRenameRequest, request: Request,
                device_id: str = Depends(current_device)):
    managed_access.require_console(request)
    from .store import get_store
    if not get_store().rename_host(key, _device_name(body.name)):
        raise HTTPException(status_code=404, detail="unknown host")
    return {"renamed": key}


@router.post("/hosts/{key}/forget")
def forget_host(key: str, request: Request, device_id: str = Depends(current_device)):
    """Withdraw a machine and its delegation from the hub.

    The tombstone hides it from clients and rejects its control credential,
    so cached projected credentials on the withdrawn leaf also stop working.

    Console-only, with one exception: a machine's own credential may forget
    the machine it is bound to — that is `detach` telling this hub goodbye
    from the mesh, where there is no console to speak from.
    """
    from . import grants, shell_grants
    from .store import get_store
    own = managed_access.leaf_for_device(device_id)
    if own is None or own["key"] != key:
        managed_access.require_console(request)
    if not get_store().forget_host(key):
        raise HTTPException(status_code=404,
                            detail="unknown or already forgotten host")
    return {"forgotten": key, "grant_dropped": grants.forget(key),
            "shell_steps": shell_grants.machine_forgotten(key)}


class HostGrantRequest(BaseModel):
    #: What the credential will be called in the LEAF's device roster — the
    #: name the user will later read when revoking it there. Defaulted, never
    #: required: a device asking for access should not have to also name itself.
    name: str = ""


@router.post("/hosts/{key}/grant")
def grant_host_access(key: str, body: HostGrantRequest, request: Request,
                      device_id: str = Depends(current_device)):
    """Return this caller\'s hub-owned credential for an adopted machine.

    Clients never receive the delegation grant. Managed Macs ask through
    their local host, which proxies the parent\'s policy-filtered answer.
    """
    from . import grants
    from .store import get_store
    if managed_access.is_leaf():
        if not _is_loopback(request.client.host if request.client else ""):
            raise HTTPException(403, "request access from the parent hub")
        return managed_access.parent_grant(key)
    row = get_store().host_row(key)
    if row is None or row["deleted"]:
        raise HTTPException(status_code=404, detail="unknown machine")
    if not managed_access.may_reach(device_id, key):
        raise HTTPException(404, "unknown machine")
    name = _device_name(body.name) if body.name else ""
    if not name:
        # The row the leaf will show is named after the device that asked, so
        # revoking it there is legible. Falling back to this host's own name for
        # the row would put this host's own name in the leaf's roster, for
        # every device, however many asked.
        asker = devices.row(device_id) or {}
        name = _device_name(asker.get("name") or "a device")
    try:
        return grants.mint_on(dict(row), name, owner_id=device_id)
    except grants.GrantError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ── the grant gate: one route, its own credential ──

class LeafVisibilityRequest(BaseModel):
    sees_home: bool
    sees_leaves: bool


@router.post("/hosts/{key}/visibility")
def set_leaf_visibility(key: str, body: LeafVisibilityRequest, request: Request):
    managed_access.require_console(request)
    from .store import get_store
    if not get_store().set_host_visibility(key, sees_home=body.sees_home,
                                           sees_leaves=body.sees_leaves):
        raise HTTPException(404, "unknown machine")
    return {"key": key, **body.model_dump()}


class LeafShellGrantRequest(BaseModel):
    #: The machine whose agents get (or lose) shell on `{key}` — the same
    #: parent-held per-pair shape as sees-leaves, one pair per call.
    src: str
    allowed: bool


@router.post("/hosts/{key}/shell")
def set_host_shell_grant(key: str, body: LeafShellGrantRequest, request: Request):
    managed_access.require_console(request)
    from . import shell_grants
    try:
        return shell_grants.flip(body.src, key, body.allowed)
    except shell_grants.ShellGrantError as exc:
        raise HTTPException(404, str(exc))


def _managed_leaf(device_id: str) -> dict:
    leaf = managed_access.leaf_for_device(device_id)
    if managed_access.is_leaf() or leaf is None or leaf["deleted"]:
        raise HTTPException(403, "this credential does not belong to an adopted machine")
    return leaf


@router.post("/managed/hosts")
def managed_hosts(request: Request, device_id: str = Depends(current_device)):
    from . import grants, enrolment
    from .store import get_store
    leaf = _managed_leaf(device_id)
    rows = []
    if leaf["sees_home"]:
        rows.append({"key": hostenv.host_id(), "name": hostenv.host_name(),
                     "address": enrolment._own_mesh_address(), "port": request.url.port or 9090,
                     "deleted": False, "delegated": True, "managed_access": True})
    if leaf["sees_leaves"]:
        rows.extend(_serve_host(row, bool(grants.held(row["key"])))
                    for row in get_store().list_hosts() if row["key"] != leaf["key"])
    return {"hosts": rows}


class ManagedGrantRequest(BaseModel):
    key: str


@router.post("/managed/grant")
def managed_grant(body: ManagedGrantRequest, request: Request,
                  device_id: str = Depends(current_device)):
    from . import grants, enrolment
    from .store import get_store
    _managed_leaf(device_id)
    if not managed_access.may_reach(device_id, body.key):
        raise HTTPException(404, "unknown machine")
    if body.key == hostenv.host_id():
        return {"host": body.key, "address": enrolment._own_mesh_address(),
                "port": request.url.port or 9090, "token": request.headers["authorization"][7:]}
    row = get_store().host_row(body.key)
    if row is None or row["deleted"]:
        raise HTTPException(404, "unknown machine")
    try:
        return grants.mint_on(row, (devices.row(device_id) or {}).get("name", "a device"),
                              owner_id=device_id)
    except grants.GrantError as exc:
        raise HTTPException(502, str(exc))


class ManagedAuthorizeRequest(BaseModel):
    device_id: str


@router.post("/managed/authorize")
def managed_authorize(body: ManagedAuthorizeRequest,
                      device_id: str = Depends(current_device)):
    leaf = _managed_leaf(device_id)
    return {"allowed": managed_access.may_reach(body.device_id, leaf["key"])}


@router.post("/managed/shell")
def managed_shell(device_id: str = Depends(current_device)):
    """A machine pulls its own shell set — the same compute the adoption
    handshake answered, so a flip and a joiner run can never disagree."""
    from . import shell_grants
    return shell_grants.leaf_shell(_managed_leaf(device_id)["key"])


@router.post("/shell/refresh")
def shell_refresh(device_id: str = Depends(current_device)):
    """A poke, not a payload: the poked machine pulls its set from the parent
    and rewrites the user-writable half. Key material never rides the poke,
    and no root is spent — that happened once, at adoption."""
    from . import shell_access
    if not managed_access.is_leaf():
        raise HTTPException(409, "only a managed machine refreshes shell grants")
    shell = managed_access.parent_shell()
    if not shell.get("authorized"):
        return {"steps": []}
    return {"steps": shell_access.apply_material(shell, Path.home())}


@router.post("/device/disconnect")
def disconnect_self(device_id: str = Depends(current_device)):
    if not managed_access.can_disconnect(device_id):
        raise HTTPException(403, "the host's own credential cannot disconnect itself")
    devices.revoke(device_id)
    return {"disconnected": True}

#
# `grant_router` is the third router in this package and the second one outside
# the bearer gate, and like `unauthenticated_router` it carries exactly one
# route. It is not unauthenticated — it authenticates a *grant* (grants.py), a
# credential that exists to do this and nothing else. Mounting `/delegate/mint`
# on `router` would have meant a device token could mint on a parent's behalf;
# leaving it on `unauthenticated_router` would have meant anybody could. It
# needs its own gate because it is its own kind of caller.
#
# Nothing else goes here either. A second route on this router is a route a
# parent hub can reach with a credential the user believes only mints.
grant_router = APIRouter(prefix="/api/jremote/v1")


class DelegateMintRequest(BaseModel):
    name: str = ""
    owner_id: str = ""


@grant_router.post("/delegate/access")
@grant_router.post("/delegate/mint")
def delegate_mint(body: DelegateMintRequest, request: Request):
    """Project one hub device onto this leaf, gated by its adoption grant.

    The projection is stable across retries, cannot administer devices, and
    rechecks the parent on every use. The grant itself opens no ordinary API.
    """
    from . import grants
    header = request.headers.get("authorization", "")
    presented = header[7:] if header.startswith("Bearer ") else ""
    parent = grants.authenticate(presented)
    if parent is None:
        client_ip = request.client.host if request.client else ""
        print(f"jremote grant: 401 from {client_ip or 'local'} — "
              "no live grant matches that credential", flush=True)
        raise HTTPException(status_code=401,
                            detail="invalid or missing grant")
    if not managed_access.is_leaf():
        raise HTTPException(403, "only a managed machine accepts delegated devices")
    if not body.owner_id:
        raise HTTPException(400, "a hub device identity is required")
    if managed_access._post_parent("authorize", {"device_id": body.owner_id}).get("allowed") is not True:
        raise HTTPException(403, "the hub refused this device")
    row, token = managed_access.mint_projection(
        presented, _device_name(body.name or parent), body.owner_id)
    grants.note_used(presented)
    print(f"jremote grant: minted {row['id']} ({row['name']}) for {parent}",
          flush=True)
    # Authorization metadata remains private on every device-list surface.
    served = {k: v for k, v in row.items()
              if k not in ("token_hash", "authority_grant", "authority_device")}
    return {"device": {**served, "revoked": False}, "token": token}


@router.get("/sessions/history")
def get_session_history(agent: str | None = None, q: str = "",
                        before: str = "", limit: int = 50):
    """On-demand session index — the store answers, no transcript is read.
    The app eagerly mirrors only its hot window; anything older or searched
    (`q`) is paged from here (`before` = ISO cursor past the last row held).
    Declared before /sessions/{sid}."""
    from .board import _session_tags
    from .store import get_store
    rows = get_store().query_sessions(agent=agent, q=q, before=before, limit=limit)
    # Tags live in the timeline db, not the session index — so this payload has
    # to be enriched or it lands on the device asserting every session is
    # untagged. This IS the app's summary source (`refreshSessions`), and its
    # upsert writes every host-owned field it receives, so an absent `tags`
    # does not read as "unchanged" — it wipes what the board sync just set.
    # One reader for both payloads, the same one /sessions uses.
    tags = _session_tags()
    for r in rows:
        r["tags"] = tags.get(r.get("session_id", ""), [])
    return {"sessions": rows}


@router.get("/sessions/closes")
def get_session_closes(sid: str = "", limit: int = 50):
    """Who ended what, newest first — every close attempt, including the ones
    that changed nothing. Omit `sid` for the whole host. Declared before
    /sessions/{sid}."""
    from .store import get_store
    return {"closes": get_store().recent_closes(session_id=sid, limit=limit)}


class SyncPush(BaseModel):
    marks: list[dict] | None = None
    session_meta: list[dict] | None = None
    settings: list[dict] | dict | None = None
    # The ways in the user keeps — a seat, optionally narrowed to a subject
    # (`tag`). Rides here and not on its own endpoint because it is the same
    # kind of row as the rest: user-authored, primary here, mirrored to every
    # device on the one cursor. Home pins deliberately do NOT — a device's Home
    # is its own focus and space on it is worth more than agreement between
    # devices, so a shortcut travels and which of them are pinned does not.
    shortcuts: list[dict] | None = None


@router.get("/sync")
def sync_pull(since: int = 0, device_id: str = Depends(current_device)):
    """User-meta mirror: every marks/filings/settings row changed past the
    device's cursor, plus the cursor to store next. Tiny by construction —
    sessions never ride this, they're queried on demand."""
    from .store import get_store
    result = get_store().changes_since(since)
    result["hosts"] = [{k: v for k, v in row.items()
                        if k not in ("device_id", "sees_home", "sees_leaves")}
                       for row in result.get("hosts", [])
                       if not managed_access.is_leaf()
                       and managed_access.may_reach(device_id, row["key"])]
    return result


@router.post("/sync")
def sync_push(payload: SyncPush):
    """Land a device's user-meta writes (newest-wins; twin marks merge).
    Returns the new seq — the pusher pulls from its old cursor to see its
    own writes confirmed alongside anything it missed."""
    from .store import get_store
    seq = get_store().apply_push(payload.model_dump())
    return {"seq": seq}


def _board_changed() -> None:
    """Tell the board watcher this endpoint just changed what it watches, so a
    connected app repaints on the action instead of on the next tick."""
    from . import board_watch
    board_watch.poke()


@router.get("/sessions/active/stream")
async def stream_active_sessions(request: Request,
                                 device_id: str = Depends(current_device)):
    """SSE board: the same payload as `/sessions/active`, pushed the moment it
    changes on the Mac.

    This is what makes the app's board match the desk. A poll can only be as
    fresh as its interval — a window closed right after a tick shows as open
    until the next one, and the phone says a session is on the Mac that ended
    seconds ago. Here the change *is* the event: one shared watcher observes
    the Mac and every connected app gets the new board within a tick of the
    fact, while an unchanged board sends nothing at all.

    Whole snapshots, never deltas, so a reconnecting app is correct on its
    first frame. `: ping` comments keep the connection (and any proxy in
    between) alive through a quiet board, and give a dead client something to
    fail on."""
    from . import board_watch

    async def body():
        async with board_watch.subscription() as sub:
            while True:
                # Revocation must cut this stream, not just the next request.
                # devices.revoke() pokes the watcher, so a revoked device's
                # check runs now, not at the next board change.
                if not await asyncio.to_thread(managed_access.stream_allowed, device_id):
                    return
                try:
                    rows = await asyncio.wait_for(sub.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if await request.is_disconnected():
                    return
                if not await asyncio.to_thread(managed_access.stream_allowed, device_id):
                    return
                yield _sse("board", {"sessions": rows})

    return StreamingResponse(body(), media_type="text/event-stream")


# ── The org feed — the app's Timeline tab ──

# One day's payload, keyed by the signature it was computed at. Two devices
# streaming the same day recompute nothing: the second finds the first's work
# already here. Holding one day is the whole cache — a client scrolling back
# through the week is a GET each, and the day it lands on is the one worth
# keeping warm.
_feed_cache: "dict[str, tuple[str, dict]]" = {}


def _feed_day(date: str, limit: int) -> dict:
    orgfeed = _optional(_FEATURES["feed"])
    if orgfeed is None:
        return _unavailable("the org feed", events=[], sources=[], date=date,
                            signature="")
    sig = orgfeed.signature(date)
    hit = _feed_cache.get(date)
    if hit and hit[0] == sig and len(hit[1].get("events") or []) <= limit:
        return hit[1]
    payload = orgfeed.day(date, limit=limit)
    _feed_cache.clear()
    _feed_cache[date] = (payload.get("signature") or sig, payload)
    return payload


@router.get("/feed")
def get_feed(date: str = "", limit: int = 1500):
    """A day of the org, every source merged, newest-first.

    The whole day ships in one payload and the app filters in memory. A day is
    a few hundred events, and filtering server-side would make toggling a chip
    — the cheapest thing the screen does — cost a round trip. The `sources`
    roster rides along so the filter UI is built from what the host actually
    serves: adding a producer reaches every device on the next fetch, with no
    app build, the same reason `/engines` is served rather than shipped."""
    return _feed_day(date, limit)


@router.get("/feed/stream")
async def stream_feed(request: Request, date: str = "", limit: int = 1500,
                      device_id: str = Depends(current_device)):
    """SSE feed: the day, re-pushed whenever anything in it moves.

    The change detector is a signature — a handful of indexed counts and maxes
    across the stores the day is drawn from, ~4ms a probe — so an idle org
    sends nothing and costs a rounding error. Only when it moves is the day
    recomputed, and the recompute is shared through `_feed_cache`, so a second
    device streaming the same day adds probes and no work.

    The tick is slower than the board's on purpose: a window closing has to
    feel instant, a day of history does not.

    Whole days, never deltas: a client that reconnects mid-stream is correct on
    its first frame with no replay to get wrong — the board stream's contract,
    for the same reason."""
    orgfeed = _optional(_FEATURES["feed"])

    day = date or datetime.now().strftime("%Y-%m-%d")

    if orgfeed is None:
        # One frame, then hold the connection open with pings. Closing the
        # stream instead would put the app into its reconnect loop against a
        # host that will never have a feed — a retry every few seconds, forever,
        # for a screen whose answer is already final.
        async def absent():
            yield _sse("feed", _feed_day(day, limit))
            while not await request.is_disconnected():
                if not await asyncio.to_thread(managed_access.stream_allowed, device_id):
                    return
                yield ": ping\n\n"
                await asyncio.sleep(16.0)
        return StreamingResponse(absent(), media_type="text/event-stream")

    async def body():
        last = ""
        quiet = 0
        while True:
            if await request.is_disconnected():
                return
            if not await asyncio.to_thread(managed_access.stream_allowed, device_id):
                return
            try:
                sig = await asyncio.to_thread(orgfeed.signature, day)
            except Exception:                               # noqa: BLE001
                # A failed probe is not an empty day. Skip the tick and let
                # the last good payload stand rather than blanking the screen.
                sig = last
            if sig != last:
                last = sig
                quiet = 0
                payload = await asyncio.to_thread(_feed_day, day, limit)
                yield _sse("feed", payload)
            else:
                quiet += 1
                if quiet >= 8:       # ~16s of nothing — hold the connection
                    quiet = 0
                    yield ": ping\n\n"
            await asyncio.sleep(2.0)

    return StreamingResponse(body(), media_type="text/event-stream")


# ── Notifications ──

class RegisterBody(BaseModel):
    token: str


class ForegroundBody(BaseModel):
    session_id: str | None = None


class MuteBody(BaseModel):
    agent_id: str
    muted: bool
    # "done" = the finish/waiting pings, "progress" = mid-turn updates.
    scope: str = "done"


@router.post("/notify/register")
def notify_register(payload: RegisterBody):
    """The app hands over its APNs device token (every launch — tokens rotate)."""
    from . import notify
    notify.register(payload.token)
    return {"ok": True}


@router.post("/notify/foreground")
def notify_foreground(payload: ForegroundBody):
    """The app reports which thread is on screen (null = none). Suppresses
    pushes for that session and clears the badge."""
    from . import notify
    notify.set_foreground(payload.session_id)
    return {"ok": True}


def _prefs() -> dict:
    from . import notify
    return {"muted_agents": sorted(notify.muted_agents()),
            "progress_muted_agents": sorted(notify.progress_muted_agents())}


@router.get("/notify/prefs")
def notify_get_prefs():
    return _prefs()


@router.post("/notify/prefs")
def notify_set_prefs(payload: MuteBody):
    from . import notify
    if payload.scope == "progress":
        notify.set_progress_muted(payload.agent_id, payload.muted)
    else:
        notify.set_muted(payload.agent_id, payload.muted)
    return _prefs()


class AgentPrefBody(BaseModel):
    agent_id: str
    flag: str
    on: bool


@router.get("/agents/prefs")
def agent_prefs_get():
    """Per-agent session behaviour — every flag and who it is on for."""
    from . import agent_prefs
    return agent_prefs.prefs()


@router.post("/agents/prefs")
def agent_prefs_set(payload: AgentPrefBody):
    """Flip one flag for one agent, and answer with the whole stored state.

    An unknown flag is a 400 rather than a stored key nobody reads: a switch
    that posts 200 and changes nothing on the Mac is the worst outcome
    available here, because it looks exactly like a switch that worked."""
    from . import agent_prefs
    try:
        agent_prefs.set_flag(payload.flag, payload.agent_id, payload.on)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return agent_prefs.prefs()


@router.get("/notify/events")
def notify_events(since: str = ""):
    """The raw notification event log — what notify_watch detected, with the
    pushed/suppressed outcome. The app renders `/sessions/{sid}/timeline`;
    this stays as the ops view of the log itself. `since` (ISO, exclusive)
    returns only newer entries."""
    from . import events
    return {"events": events.since(since)}


@router.get("/sessions/{sid}/timeline")
def session_timeline(sid: str):
    """The session's notification timeline — derived from the transcript at
    the push cadence (turn ends + selected narration), with the event log
    overlaid for the facts the transcript can't carry (dialog waits, the
    pushed flag). Whole-history, any session, no recorder uptime needed.

    Carries the session's `load` reading (context size + turns) because the
    panel already polls this while open — the meter needs a *live* number,
    and the thread's own board row is a snapshot taken when it opened. A
    reading that failed is omitted, never zeroed."""
    _check_sid(sid)
    from . import events, load
    body = {"events": events.timeline(sid)}
    reading = load.reading(sid)
    if reading is not None:
        body["load"] = reading
    return body


@router.get("/sessions/{sid}")
def get_session(sid: str):
    _check_sid(sid)
    from .messages import parse_session
    return parse_session(sid)


class OpenPathBody(BaseModel):
    path: str


@router.post("/sessions/{sid}/open-path")
def session_open_path(sid: str, payload: OpenPathBody):
    """A file link clicked in the session's terminal opens on the Mac — the
    machine the path names. The body carries the raw token from under the
    click; resolution (line-suffix strip, ~, relative against the pane's
    cwd) is the host's reading, not the app's (`open_path.py`). URLs never
    come here — the device that clicked opens those itself."""
    from . import open_path
    try:
        path = open_path.open_on_mac(sid, payload.path)
    except ValueError:
        raise HTTPException(status_code=400, detail="empty path")
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown session {sid!r}")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=f"no such file: {e}")
    return {"ok": True, "path": str(path)}


class ComposerLiftBody(BaseModel):
    # What the CLI's input box should hold once its contents have been lifted
    # out. Empty is the compose case: the words move to the sheet, and leaving
    # a copy behind would submit them twice.
    replacement: str = ""


@router.post("/sessions/{sid}/composer/lift")
async def composer_lift(sid: str, payload: ComposerLiftBody):
    """Hand the session's whole input buffer to the caller (`composer.py`).

    The screen is not the buffer — the box scrolls inside itself and folds
    pastes into chips — so compose asks the CLI for its buffer through the
    CLI's own external-editor handoff rather than reading rows off the grid.

    `{"lifted": false, "reason": ...}` is a real answer, not an error: the
    session may predate the shim or have a dialog up. It is also a guarantee
    that nothing in the input box was touched, which is what lets the app
    fall back to leaving the text where it is.
    """
    from . import composer
    return await run_in_threadpool(composer.lift, sid, payload.replacement)


# ── Where a desk-side spawn opens, and closing a thread elsewhere ──

class RouteSpawnBody(BaseModel):
    new_sid: str
    cwd: str = ""


@router.post("/sessions/{sid}/route-spawn")
async def route_spawn(sid: str, payload: RouteSpawnBody):
    """Which machine shows the window for a spawn driven from inside `sid`.

    The spawn CLI asks after creating the session: a handoff typed on the
    iPad opens on the iPad (an open frame goes down the driver's own PTY
    socket; the app decides window-or-nothing by its own setting), a
    desk-driven one keeps the Mac window ("mac"), and a device driver whose
    socket died gets "none" — created quietly, the board row is the
    visibility. Async: the attachment registry lives on the event loop."""
    _check_sid(sid)
    from . import attach, desk
    route, driver = attach.spawn_route(sid)
    if route == "device":
        attach.send_open(driver, desk.thread_url(payload.new_sid, payload.cwd))
    return {"route": route}


class RouteOpenBody(BaseModel):
    url: str


@router.post("/sessions/{sid}/route-open")
async def route_open(sid: str, payload: RouteOpenBody):
    """Which screen shows a `jremote://` link raised from inside `sid`.

    `route-spawn` above answers this for a spawn's own window; this answers
    it for anything else a session puts on screen — today a `jremote://doc`
    render from `/pict`. Same question, same rule: a document asked for on
    the iPad belongs on the iPad, and the Mac is the answer for everything
    the host cannot see.

    One deliberate difference from a spawn. "none" — a device drove it, its
    socket is gone — means *create quietly* for a spawn, because the session
    is already visible as a board row. A document has no board row and no
    second way to be found, so the caller is told "mac" and opens it there:
    a window on the desk is worth more than nothing on any screen, and it
    displaces nothing, which was the whole reason a spawn refuses.

    Async: the attachment registry lives on the event loop."""
    _check_sid(sid)
    url = payload.url.strip()
    if not url.startswith("jremote://"):
        raise HTTPException(status_code=400, detail="jremote:// url required")
    from . import attach
    route, driver = attach.spawn_route(sid)
    if route == "device":
        attach.send_open(driver, url)
        return {"route": "device"}
    return {"route": "mac"}


class DismissBody(BaseModel):
    instance: str


@router.post("/sessions/{sid}/dismiss-elsewhere")
async def dismiss_elsewhere(sid: str, payload: DismissBody):
    """Close this thread's view on every *other* app instance (4412) — the
    windows dismiss, the session keeps running. `instance` is the asker,
    spared. Async: the attachment registry lives on the event loop."""
    _check_sid(sid)
    if not payload.instance:
        raise HTTPException(status_code=400, detail="instance required")
    from . import attach
    return {"dismissed": attach.close_others(sid, payload.instance)}


def _tag_cli(*args: str) -> str:
    """Run `bin/log_event tag …` — the timeline's one writer — and turn its
    exit code into an answer.

    See `timeline.py`: reading the sqlite file directly is a deliberate,
    documented read-only exception there; writing it is never one. So every
    vocabulary edit shells out, and inherits the binary's own gates —
    the description requirement, the name rule, the refusal to delete a
    carried tag — rather than growing a second copy of them here that would
    quietly drift out of agreement with the CLI. Its refusal text is
    surfaced verbatim for the same reason: `log_event` already says why in a
    sentence written for a person, and rephrasing it would only lose the
    part that says what to do next."""
    from . import timeline as tl
    binary = tl.log_event_bin()
    if binary is None:
        raise HTTPException(status_code=503,
                            detail="this host has no timeline to keep subjects in")
    try:
        r = subprocess.run([str(binary), "tag", *args],
                           capture_output=True, text=True, timeout=tl._TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        raise HTTPException(status_code=503, detail=f"couldn't run log_event: {e}")
    if r.returncode != 0:
        raise HTTPException(status_code=400,
                            detail=(r.stderr or r.stdout or "log_event refused").strip())
    return (r.stdout or "").strip()


class TagBody(BaseModel):
    name: str = ""
    description: str = ""


@router.post("/tags")
def create_tag(payload: TagBody):
    """Mint a subject.

    A session card's toggle list still may not mint, and that restriction is
    not softened by this route existing — a name typed while filing, in a
    hurry, against a list that didn't have what you wanted, is exactly how a
    vocabulary forks into synonyms and stops answering. This is the other
    thing: a deliberate editor where the description is a field you must
    fill, which is the entire gate `log_event tag new` enforces. Same bar,
    different keyboard."""
    from . import timeline as tl
    name = tl.normalize(payload.name)
    if not name:
        raise HTTPException(status_code=400, detail="tag name required")
    _tag_cli("new", name, "--description", payload.description)
    _board_changed()
    return {"ok": True, "name": name}


@router.patch("/tags/{name}")
def edit_tag(name: str, payload: TagBody):
    """Rewrite what belongs under a subject, rename it, or both.

    A rename carries every session filed under the tag, because sessions
    reach it by id and never by name — which is why an editor can offer one
    at all instead of making a misnamed subject permanent."""
    from . import timeline as tl
    old = tl.normalize(name)
    if not old:
        raise HTTPException(status_code=400, detail="tag name required")
    new = tl.normalize(payload.name)
    description = payload.description.strip()
    if not new and not description:
        raise HTTPException(status_code=400, detail="nothing to change")
    # Description first, while `old` still resolves: it is addressed by the
    # name the caller sent, and a rename would move the target out from under
    # it. The reverse order fails on a screen that renames and rewords at once.
    if description:
        _tag_cli("describe", old, "--description", description)
    if new and new != old:
        _tag_cli("rename", old, new)
    _board_changed()
    return {"ok": True, "name": new or old}


@router.delete("/tags/{name}")
def delete_tag(name: str, force: bool = False):
    """Retire a subject. `force` is required once any session carries it.

    The binary's refusal names the number of sittings the delete would
    unfile, and that number is surfaced rather than re-derived so the app can
    put it in the confirmation — asking the user to accept a loss whose size
    nobody stated is not a confirmation."""
    from . import timeline as tl
    tag = tl.normalize(name)
    if not tag:
        raise HTTPException(status_code=400, detail="tag name required")
    _tag_cli("delete", tag, *(["--force"] if force else []))
    _board_changed()
    return {"ok": True}


class SessionTagBody(BaseModel):
    verb: str
    name: str


@router.post("/sessions/{sid}/tags")
def session_tag_write(sid: str, payload: SessionTagBody):
    """File or unfile a timeline tag on this session — `bin/log_event tag
    set|unset`, the timeline's one writer. `timeline.py`'s own docstring is
    why this never touches the sqlite file directly: reads are the
    deliberate read-only exception there, writes are not, so this shells out
    exactly as `open-new`'s tag pin does.

    `verb` is restricted here, not left to the binary — "set"/"unset" are
    the only two this route may ever reach through, and filing is the only
    thing this surface does. Minting belongs to `POST /tags`, behind an
    editor with a required description; a name typed into a filing control
    is the fork this gate exists to prevent, and the editor existing does
    not soften it. Beyond that, `log_event tag set` already refuses a name nobody
    minted, so the route inherits it for free: its stderr is surfaced
    verbatim rather than duplicated as a second vocabulary check."""
    _check_sid(sid)
    from . import timeline as tl
    verb = payload.verb.strip().lower()
    if verb not in ("set", "unset"):
        raise HTTPException(status_code=400, detail=f"unknown verb {payload.verb!r}")
    name = tl.normalize(payload.name)
    if not name:
        raise HTTPException(status_code=400, detail="tag name required")
    binary = tl.log_event_bin()
    if binary is None:
        raise HTTPException(status_code=503,
                            detail="this host has no timeline, so it cannot file a tag")
    try:
        r = subprocess.run([str(binary), "tag", verb, name, "--session", sid],
                           capture_output=True, text=True, timeout=tl._TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        raise HTTPException(status_code=503, detail=f"couldn't run log_event: {e}")
    if r.returncode != 0:
        raise HTTPException(status_code=400,
                            detail=(r.stderr or r.stdout or "log_event refused").strip())
    _board_changed()
    return {"ok": True}


# ── File drops (phone → Mac) ──

@router.post("/upload")
async def upload_file(request: Request, agent_id: str, filename: str = "",
                      message: str = ""):
    """Receive a session-less drop (the share sheet); return its Mac path.

    Body is the raw bytes (no multipart — one file, one request). The path is
    the whole product: the app pastes it into a session's input the way a file
    dragged into the terminal pastes on the Mac, and the agent Reads it from
    there. The bytes land in the seat's pad
    (`scratchpad.save_drop`) — the one shared folder the Files pane shows,
    marked as the user's so an agent's sweep leaves it. In-session uploads use
    `/sessions/{sid}/scratchpad/upload`.

    With `message`, the drop is also filed as an **update from the user** with the
    file attached (`bin/msg`), so a Drop Only share is not a dead-letter — it
    appears once in that agent's next session and obliges nobody. It is never
    a task: the normal share already opens a live board session, which is
    strictly more than a headless wake could give. Without `message` the
    behavior is unchanged: bytes land, path is returned, nothing is filed.
    `filed` in the response says which happened."""
    from . import scratchpad
    length = request.headers.get("content-length")
    if length and int(length) > scratchpad.MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="file too large")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(data) > scratchpad.MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="file too large")
    try:
        path = scratchpad.save_drop(agent_id, filename, data)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown agent {agent_id!r}")
    out = {"ok": True, "path": str(path)}
    if message.strip():
        out["filed"] = _file_drop_as_mail(agent_id, message, str(path))
    return out


#: The inbox CLI. A drop filed as mail rides the same store, injection and Stop
#: guard as any agent-to-agent message — there is no second mail path.
_MSG_BIN = plugin_paths.jstack_bin("msg")


def _file_drop_as_mail(agent_id: str, message: str, path: str) -> "str|None":
    """File a share-sheet drop as an update from the user. None if it couldn't be.

    Never raises: the bytes are already safely on disk and the path is already
    the caller's answer, so a mail failure must degrade to today's behavior
    (a parked file) rather than fail an upload that actually succeeded."""
    base = agent_id.split("-")[0]
    argv = [str(_MSG_BIN), "send", f"@{base}", message.strip(),
            "--file", path, "--from", "boss"]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


# ── Scratchpad (Mac → phone) ──

@router.get("/sessions/{sid}/scratchpad")
def scratchpad_list(sid: str):
    """The session's scratchpad as a newest-first listing — the Files tab.

    A session's scratchpad IS the seat's pad — one shared folder per seat,
    not one per session (`scratchpad.py`)."""
    _check_sid(sid)
    from . import scratchpad
    try:
        return {"files": scratchpad.list_files(sid)}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown session {sid!r}")


@router.get("/sessions/{sid}/scratchpad/file")
def scratchpad_fetch(sid: str, rel: str):
    """One file's bytes by its rel, typed so the phone picks the viewer."""
    _check_sid(sid)
    from . import scratchpad
    try:
        path = scratchpad.file_path(scratchpad.session_pad(sid), rel)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown session {sid!r}")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"no scratchpad file {rel!r}")
    return FileResponse(path, media_type=scratchpad._mime(path), filename=path.name)


@router.post("/sessions/{sid}/scratchpad/upload")
async def scratchpad_upload(request: Request, sid: str, filename: str = ""):
    """Receive a file from the phone into the viewing session's own pad —
    the Files tab as a two-way shelf. Same raw-body contract as `/upload`:
    the file lists right back in the pane (plain name, collision-suffixed),
    where it can later be dragged onto a chat to type its path into the CLI."""
    _check_sid(sid)
    from . import scratchpad
    length = request.headers.get("content-length")
    if length and int(length) > scratchpad.MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="file too large")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(data) > scratchpad.MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="file too large")
    try:
        path = scratchpad.save(scratchpad.session_pad(sid), "", filename, data)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown session {sid!r}")
    return {"ok": True, "path": str(path)}


class ScratchpadRelBody(BaseModel):
    rel: str


@router.post("/sessions/{sid}/scratchpad/delete")
def scratchpad_delete(sid: str, payload: ScratchpadRelBody):
    """Remove one file — the row's swipe-to-delete."""
    _check_sid(sid)
    from . import scratchpad
    try:
        scratchpad.delete(scratchpad.session_pad(sid), payload.rel)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown session {sid!r}")
    except FileNotFoundError:
        raise HTTPException(status_code=404,
                            detail=f"no scratchpad file {payload.rel!r}")
    return {"ok": True}


@router.post("/sessions/{sid}/scratchpad/clear")
def scratchpad_clear(sid: str):
    """Empty the session's pad — the tab's Clear button."""
    _check_sid(sid)
    from . import scratchpad
    try:
        return {"ok": True, "cleared": scratchpad.clear_dir(scratchpad.session_pad(sid))}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown session {sid!r}")


# ── Files (the seat's, browsed a directory at a time) ──
#
# Addressed by agent, not by session: the pad belongs to the seat, so it has
# something to show the moment the pane opens and keeps showing the same thing
# whichever thread the user came in through. One rel shape throughout — a path
# relative to the pad, empty meaning the pad itself.


def _pad(agent_id: str):
    from . import scratchpad
    try:
        return scratchpad.agent_pad(agent_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown agent {agent_id!r}")


@router.get("/agents/{agent_id}/files")
def agent_files(agent_id: str, path: str = ""):
    """One directory of the seat — folders and files, never a descent.

    This is the whole Files pane: `path=""` gives the seat's one shared
    folder, and any folder's rel opens that folder. Cost is one `scandir`
    regardless of what the tree below holds, which is the point — a pad can
    carry a build tree of a quarter-million files and still open
    instantly."""
    from . import scratchpad
    try:
        return scratchpad.list_dir(_pad(agent_id), path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"no folder {path!r}")


@router.get("/agents/{agent_id}/files/content")
def agent_file_content(agent_id: str, rel: str):
    """One file's bytes by its rel, typed so the phone picks the viewer."""
    from . import scratchpad
    try:
        path = scratchpad.file_path(_pad(agent_id), rel)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"no file {rel!r}")
    return FileResponse(path, media_type=scratchpad._mime(path), filename=path.name)


class FilesRelBody(BaseModel):
    rel: str


@router.post("/agents/{agent_id}/files/delete")
def agent_file_delete(agent_id: str, payload: FilesRelBody):
    """Remove one file — the row's swipe-to-delete."""
    from . import scratchpad
    try:
        scratchpad.delete(_pad(agent_id), payload.rel)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"no file {payload.rel!r}")
    return {"ok": True}


@router.post("/agents/{agent_id}/files/clear")
def agent_files_clear(agent_id: str, payload: FilesRelBody):
    """Empty the folder being shown, the user's own folder and all of it.
    Scoped to a rel on purpose — Clear can never take more than the screen it
    was pressed on displayed."""
    from . import scratchpad
    try:
        return {"ok": True, "cleared": scratchpad.clear_dir(_pad(agent_id),
                                                            payload.rel)}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"no folder {payload.rel!r}")


@router.post("/agents/{agent_id}/files/upload")
async def agent_files_upload(request: Request, agent_id: str,
                             rel: str = "", filename: str = ""):
    """Receive a file from the phone into the folder being shown — the pane
    as a two-way shelf. Same raw-body contract as `/upload`."""
    from . import scratchpad
    length = request.headers.get("content-length")
    if length and int(length) > scratchpad.MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="file too large")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(data) > scratchpad.MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="file too large")
    try:
        path = scratchpad.save(_pad(agent_id), rel, filename, data)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"no folder {rel!r}")
    return {"ok": True, "path": str(path)}


@router.post("/agents/{agent_id}/files/save")
async def agent_file_save(request: Request, agent_id: str, rel: str):
    """Put an edited file back on the name it was opened from.

    The return leg of the viewer: the phone fetched these bytes, the user marked
    them up, and they belong on the original rather than beside it. Separate
    from `upload` because the two want opposite things from a name that
    already exists — upload steps around it, this one lands on it. A rel that
    names nothing is a 404, never a create."""
    from . import scratchpad
    length = request.headers.get("content-length")
    if length and int(length) > scratchpad.MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="file too large")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(data) > scratchpad.MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="file too large")
    try:
        path = scratchpad.write_back(_pad(agent_id), rel, data)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"no file {rel!r}")
    return {"ok": True, "path": str(path)}


# ── Control ──

class ControlBody(BaseModel):
    target: str | None = None


def _control():
    """The profile-named control module, or None where there is no such tier.

    The convenience tier is not generic: its actions are the embedding
    host's own daemons. A host with no profile answer has no such daemons to
    relaunch, so its absence costs the two endpoints below and not the whole
    API — the same bargain as the features at the top of this file.
    """
    return _optional(hostenv.control_module())


@router.get("/control/actions")
def control_actions():
    """Metadata for the convenience-tier controls, so the app renders the list
    dynamically (new relaunch targets appear without an app update).

    An empty list on a host with no control tier is the honest answer, not a
    degraded one: the actions genuinely do not exist there, so the app renders
    no buttons rather than buttons that cannot work.
    """
    control = _control()
    return control.list_actions() if control else []


@router.post("/control/{action}")
def control_dispatch(action: str, payload: ControlBody | None = None):
    """Run a convenience-tier action. System-level actions are NOT here — those
    go to the root deadman daemon. Unknown action → 400.

    503 rather than 400 where there is no control tier: the action is not
    misspelled, it is unsupported on this host, and a client that cannot tell
    those apart retries the wrong one.
    """
    control = _control()
    if control is None:
        raise HTTPException(status_code=503,
                            detail="this host has no convenience-tier controls")
    target = payload.target if payload else None
    try:
        return control.dispatch(action, target=target)
    except control.UnknownAction as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Turn (streaming) ──

class TurnBody(BaseModel):
    text: str


async def _stream_response(agen) -> StreamingResponse:
    """Prime the generator so pre-flight TurnErrors surface as HTTP status
    codes before the streaming response begins, then stream the rest."""
    iterator = agen.__aiter__()
    try:
        first = await iterator.__anext__()
    except TurnError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
    except StopAsyncIteration:
        first = None

    async def body():
        if first is not None:
            yield _sse(*first)
        async for event in iterator:
            yield _sse(*event)

    return StreamingResponse(body(), media_type="text/event-stream")


@router.post("/sessions/{sid}/turn")
async def post_turn(sid: str, payload: TurnBody):
    _check_sid(sid)
    return await _stream_response(stream_turn(sid, payload.text, resume=True))


class NewSessionBody(BaseModel):
    agent_id: str
    text: str


@router.post("/sessions/new")
async def post_new_session(payload: NewSessionBody):
    import uuid
    sid = str(uuid.uuid4())
    return await _stream_response(
        stream_turn(sid, payload.text, resume=False, agent_id=payload.agent_id)
    )


# ── Live tail ──

def _tail_path(sid: str) -> Path | None:
    """The session's Claude JSONL, or None while it has yet to be written.

    Deliberately only the projects-dir scan, not `messages._find_session_file`:
    the tail parser reads Claude's line shape, and that function also resolves
    Codex rollouts, which it would mis-parse."""
    claude_projects = Path.home() / ".claude" / "projects"
    if not claude_projects.exists():
        return None
    for pd in claude_projects.iterdir():
        cand = pd / f"{sid}.jsonl"
        if cand.exists():
            return cand
    return None


@router.get("/sessions/{sid}/stream")
async def stream_session(sid: str, request: Request):
    """SSE live-tail: emit new user/assistant messages as they're appended to
    a running session's JSONL. Watch an agent work in real time.

    **A booting session is not a missing one.** The board carries a managed sid
    from the instant it spawns (`record_open` lands it before the pane execs
    `claude`), and the CLI takes ~10s to flush its first line — so for that
    window this endpoint used to 404 a session that was running fine. The cost
    was not the status code: the client's watch task dies on one throw and
    nothing restarts it, so a thread opened during boot stayed deaf for its
    whole life — no streamed messages, no busy truth — until the user closed and
    reopened the window. Wait for the file instead, and take it from the top
    when it lands, since nothing in it was ever sent to this client."""
    _check_sid(sid)
    from .managed import is_open

    path = _tail_path(sid)
    if path is None and not is_open(sid):
        raise HTTPException(status_code=404, detail="session file not found")
    if path is not None:
        from .transcripts import _find_session_cwd
        if not _find_session_cwd(sid):
            raise HTTPException(status_code=404, detail="session not found")

    async def body():
        from . import turns
        nonlocal path
        # Start from the current end of the file — only emit new activity. A
        # session still booting has no file yet, so it starts at 0: everything
        # it eventually writes is new to this client.
        offset = path.stat().st_size if path is not None else 0
        idle_deadline = time.time() + 600  # close after 10 min idle
        # The single source of truth for "a turn is running for this session":
        # the host's in-flight set. Emit it up front and on every change so the
        # phone never has to guess (kills phantom "still working" + lost sends).
        busy = None
        while True:
            if await request.is_disconnected():
                return
            now_busy = sid in turns._in_flight
            if now_busy != busy:
                busy = now_busy
                yield _sse("busy", {"running": busy})
            if path is None:
                # Still booting. Busy truth keeps flowing above; the idle
                # deadline still governs, so a session that never writes one
                # closes the stream rather than polling forever.
                path = _tail_path(sid)
                if path is None:
                    if time.time() > idle_deadline:
                        yield _sse("idle", {"message": "closed after idle timeout"})
                        return
                    await asyncio.sleep(0.2)
                    continue
                idle_deadline = time.time() + 600
            size = path.stat().st_size
            if size > offset:
                idle_deadline = time.time() + 600
                with path.open("r") as f:
                    f.seek(offset)
                    chunk = f.read()
                    offset = f.tell()
                for line in chunk.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if o.get("type") not in ("user", "assistant"):
                        continue
                    msg = o.get("message", {})
                    role = msg.get("role", o["type"])
                    segs = _blocks_to_segments(msg.get("content", ""))
                    if not segs:
                        continue
                    prose = _flatten(segs)
                    if _is_noise(role, prose):
                        continue
                    if not prose and not any(s["type"] in ("code", "tool") for s in segs):
                        continue
                    yield _sse("message", {"role": role, "segments": segs,
                                           "text": prose[:4000]})
            if time.time() > idle_deadline:
                yield _sse("idle", {"message": "closed after idle timeout"})
                return
            # Poll fast so multi-block replies reach the phone block-by-block, in
            # step with the CLI, rather than batched once a second.
            await asyncio.sleep(0.2)

    return StreamingResponse(body(), media_type="text/event-stream")


def _flatten_content(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    parts.append(f"[tool: {block.get('name', '?')}]")
        return "\n".join(p for p in parts if p).strip()
    return ""


@router.post("/sessions/{sid}/interrupt")
def interrupt_session(sid: str):
    """The phone's ESC: stop what the session is doing without ending it.
    Managed → tmux ESC (claude interrupts the turn, stays alive). Direct
    in-flight turn → kill the headless process. Raw window → nothing we can
    inject into; 409."""
    _check_sid(sid)
    import subprocess as _sp
    from . import managed
    from . import turns
    if managed.is_open(sid):
        _sp.run(managed._t("send-keys", "-t", managed._name(sid), "Escape"),
                capture_output=True)
        return {"ok": True, "mode": "managed"}
    if turns.interrupt_turn(sid):
        return {"ok": True, "mode": "direct"}
    raise HTTPException(status_code=409,
                        detail="nothing interruptible — session is a raw Mac window")


# ── Mac integration ──

@router.post("/sessions/{sid}/focus")
def focus_session(sid: str):
    """**Open in iTerm** — put an iTerm window on this session, on demand.

    The window is a viewer, not life support (the board invariant): closing it
    later just detaches. Never spawns a second claude on a live transcript:
      managed → attach an iTerm window to its tmux (idempotent)
      live in a raw window → activate iTerm (the window is already there;
        per-window raising needs Apple events, which TCC keeps revoking)
      idle → open it managed with a window attached — registered, drivable,
        and showing in iTerm, instead of the old raw-window resume
    """
    _check_sid(sid)
    import subprocess
    from . import managed
    from .board import _live_session_ids
    from .transcripts import _find_session_cwd
    from .hostenv import project_dir_to_agent
    if managed.is_open(sid):
        managed.launch_terminal(sid)
        return {"ok": True, "mode": "managed"}
    if sid in _live_session_ids():
        subprocess.run(["open", "-a", "iTerm"], check=False)
        return {"ok": True, "mode": "raise"}
    target = managed.reopen_target(sid)
    if not target:
        raise HTTPException(status_code=404, detail="session not found")
    managed.record_open(sid, target["agent"], engine=target["engine"])
    try:
        managed.open_managed(sid, target["cwd"], resume=True, window=True,
                             engine=target["engine"],
                             resume_id=target["resume_id"])
    except managed.WindowRequired as e:
        raise HTTPException(status_code=503, detail=str(e))
    _board_changed()
    return {"ok": True, "mode": "opened"}


# ── Managed (phone-drivable) terminal sessions ──

class InputBody(BaseModel):
    text: str


# Typed into a taken-over session that was killed mid-turn, once its resumed
# claude is back at the prompt — whatever the old process had in flight but not
# yet persisted died with it, so the first order is re-verification.
TAKEOVER_CONTINUE = (
    "This session was just moved to a different terminal and the previous "
    "claude process was interrupted mid-turn. Continue where you left off — "
    "re-verify the step that was in flight before building on it, since work "
    "not yet persisted was lost with the old process.")


class OpenNewBody(BaseModel):
    agent_id: str
    # Optional first message, typed into claude once its prompt is up (the
    # same prompt-gated typer as the takeover nudge). This is the share-sheet
    # path: spawn the seat's session and hand it the shared file's path +
    # comment as the opening turn. Empty/None = spawn waiting, unchanged.
    text: str | None = None
    # Which agent CLI runs this seat — "claude" or "codex". Both read the same
    # CLAUDE.md walk-up, rules, skills and seat timeline, so this picks the
    # engine, never the identity.
    #
    # Omitted (None) = **this agent's default**, not a hardcoded claude. That
    # is the whole point of resolving host-side: the app's new-chat tap, the
    # share sheet, a pinned shortcut and a desk-side spawn all get the user's
    # per-agent choice without any of them knowing it exists. A client that
    # names an engine (the long-press menu) still overrides it for that spawn.
    engine: str | None = None
    # Likewise for the model. Omitted = the agent's model for whichever engine
    # resolved — so long-pressing to the non-default engine still lands on the
    # model chosen for THAT engine, never the other one's.
    model: str | None = None
    # The subject this session is opened ON — a timeline tag. It replaces the
    # seat's injected history with that tag's, across every agent that worked
    # it, and the session files its own entries under it. A seat is where the
    # terminal runs; the tag is what the sitting is about, and the two are
    # independent: the same seat pinned to two subjects is two cockpits.
    #
    # Validated against the minted vocabulary and refused if unknown — see the
    # route. Empty/None = the ordinary seat session, unchanged.
    tag: str | None = None
    # Subjects the session is FILED under from its first instant, on top of
    # whatever it is opened on. Not the same fact as `tag` and deliberately a
    # second field, the same split the board already draws: `tag` is the one
    # subject whose history the cockpit was built from — a session has one, it
    # is what `JSTACK_TIMELINE_TAG` carries, and the injector reads exactly one
    # — while these are filing, of which a session may carry many.
    #
    # Same vocabulary gate as `tag`: unknown is a 400, never minted, never
    # dropped. A shortcut that named a filing nobody minted would look like it
    # worked and file nowhere.
    tags: list[str] | None = None
    # Which saved shortcut launched this — recorded so that shortcut can show
    # its own sittings later. Purely provenance: nothing about how the session
    # runs is read from it, because every launch setting a shortcut holds is
    # already spelled out in the fields above. The app resolves the config; the
    # host resolves the engine, the model and the vocabulary.
    shortcut_id: str | None = None


@router.post("/sessions/{sid}/open")
def open_session_managed(sid: str):
    """Launch this session in a managed tmux terminal so the app can drive the
    native CLI. Idempotent. No terminal window is involved — the session is
    registered on the board before claude starts, and any client (app, phone,
    an on-demand iTerm window) is a viewer.

    This is also the **takeover** path, and it is transactional: a session
    live in a raw Mac terminal already has a `claude` holding its transcript,
    and that process is ended only once the managed session it is moving to
    exists — so a takeover that fails costs nothing. `409` means the old
    process would not exit and the work is still running where it was.

    A session taken mid-turn auto-continues: the working judgment is read from
    the transcript tail before the kill, and the resumed claude gets a continue
    message typed in once it is back at its prompt — taking over a working
    session must not silently park its work."""
    _check_sid(sid)
    import signal
    from . import managed
    from .transcripts import _find_session_cwd
    from .hostenv import project_dir_to_agent
    cwd = _find_session_cwd(sid)
    if not cwd:
        raise HTTPException(status_code=404, detail="session not found")
    # A raw claude and a `--resume` would be two writers on one transcript, so
    # the raw one is displaced — but from inside `open_managed`, after the
    # window, never before it. Reopening an already-managed session is idempotent
    # and displaces nothing.
    from .board import (_live_session_ids, end_raw_holders, mid_turn,
                        pids_holding, window_ttys)
    displace = None
    nudge = None
    if not managed.is_open(sid) and sid in _live_session_ids():
        # A session taken while working keeps working: judged here, before
        # anything is signalled (mid-turn needs the old process's tool children
        # alive to read), and delivered as a continue nudge once the resumed
        # claude is back at its prompt. One taken at rest comes up waiting.
        if mid_turn(sid):
            nudge = TAKEOVER_CONTINUE
        # SIGKILL, not SIGTERM: a clean exit fires the SessionEnd review hook,
        # and this session is being moved, not ended.
        def displace():
            # The raw window's tty, read before the kill — afterwards the
            # dead process can't name it. The takeover empties that window,
            # so it goes with it: a bare prompt left on the desk reads as
            # work and is the husk the user otherwise closes by hand.
            ttys = window_ttys(pids_holding(sid))
            if not end_raw_holders(sid, signal.SIGKILL):
                return False
            managed.close_windows(ttys)
            return True
    base = ""
    for pd in (Path.home() / ".claude" / "projects").iterdir():
        if (pd / f"{sid}.jsonl").exists():
            parsed = project_dir_to_agent(pd.name)
            if parsed:
                base = parsed[0]
            break
    # Registered-first: the board row is the visibility, so it exists before
    # claude does. open_managed's failure paths clear the registration.
    managed.record_open(sid, base)
    try:
        managed.open_managed(sid, cwd, resume=True, displace=displace,
                             nudge=nudge)
    except managed.TakeoverFailed as e:
        raise HTTPException(status_code=409, detail=str(e))
    _board_changed()
    return {"ok": True, "open": True}


@router.post("/sessions/open-new")
def open_new_managed(payload: OpenNewBody):
    """Start a fresh managed session for an agent. No terminal window — the
    session is registered on the board before claude starts, and the board
    (pushed to every device) is what makes remote-started work visible."""
    import uuid
    from . import managed
    from .hostenv import workspace, split_id
    try:
        cwd = str(workspace(payload.agent_id))
    except (KeyError, TypeError):
        raise HTTPException(status_code=404, detail=f"unknown agent {payload.agent_id!r}")
    if not Path(cwd).exists():
        raise HTTPException(status_code=404, detail="agent workspace missing")
    from . import engines as eng
    try:
        # One fallback point, host-side: an omitted engine/model becomes this
        # agent's default here and nowhere else. Anything NAMED and unknown
        # refuses rather than falling back — silently running a different
        # engine or model than the caller asked for is the kind of wrong that
        # only surfaces much later, in a transcript nobody can explain.
        engine, model = eng.resolve(payload.agent_id, payload.engine,
                                    payload.model)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    from . import timeline as tl
    tag = tl.normalize(payload.tag or "")
    # The subject it opens on and the subjects it files under go through one
    # gate: both name the vocabulary, and both are refused for the same reason.
    # The opened-on tag is checked first so its 400 is the one the user sees when a
    # shortcut carries several stale names.
    extra = [t for t in (tl.normalize(t) for t in (payload.tags or []))
             if t and t != tag]
    for name in ([tag] if tag else []) + extra:
        # An unknown tag is refused, never minted and never dropped. Minting
        # from a spawn would grow the vocabulary by typo, and the vocabulary
        # meaning one thing to every writer is the whole value of a tag.
        # Dropping it would be worse than either: the session opens looking
        # exactly like the pin worked, on the wrong history.
        if not tl.available():
            raise HTTPException(
                status_code=503,
                detail="this host has no timeline, so it cannot open a session "
                       "on a subject")
        if not tl.known(name):
            raise HTTPException(
                status_code=400,
                detail=f"no timeline tag {name!r} — mint it deliberately with "
                       "`log_event tag new`")
    sid = str(uuid.uuid4())
    managed.record_open(sid, split_id(payload.agent_id)[0], engine=engine,
                        model=model, tag=tag)
    managed.open_managed(sid, cwd, resume=False,
                         nudge=(payload.text or "").strip() or None,
                         engine=engine, model=model, tag=tag)
    # After the session exists, and non-fatally. The filing is worth having and
    # never worth losing the session over: the spawn has already happened by
    # here, and a 500 raised now would leave the user looking at an error for a
    # chat that is up and running. The opened-on tag is the hook's own job
    # (`carry_tag`) and is not re-filed here.
    if extra:
        _file_tags(sid, extra)
    if payload.shortcut_id:
        from .store import get_store
        get_store().record_launch(sid, payload.shortcut_id)
    _board_changed()
    return {"ok": True, "session_id": sid, "open": True, "engine": engine,
            "model": model, "tag": tag, "tags": extra}


def _file_tags(sid: str, names: list[str]) -> None:
    """File a fresh session under subjects it was launched with.

    `bin/log_event` is the timeline's one writer here as everywhere else, and
    it takes every name in one call — one fork for a shortcut carrying four
    subjects, not four. Failures are logged and swallowed: see the call site."""
    import logging
    from . import timeline as tl
    binary = tl.log_event_bin()
    if binary is None:
        return
    try:
        r = subprocess.run([str(binary), "tag", "set", *names, "--session", sid],
                           capture_output=True, text=True, timeout=tl._TIMEOUT)
        if r.returncode != 0:
            logging.getLogger(__name__).warning(
                "jremote: filing %s on %s: %s", names, sid[:8],
                (r.stderr or r.stdout or "").strip())
    except (OSError, subprocess.SubprocessError) as e:
        logging.getLogger(__name__).warning(
            "jremote: couldn't file %s on %s: %s", names, sid[:8], e)


@router.post("/sessions/{sid}/splitoff")
def splitoff_session(sid: str):
    """Verbatim-fork this session into a fresh id and open the copy in its own
    managed terminal — the phone face of /jstack:splitoff.

    The dub is the jStack adapter (`dub-session`): the transcript copied to a
    new UUID in the same project dir, its internal sessionId rewritten, the
    picker title suffixed " - copy". The source session is untouched — live or
    not — and the copy diverges forward once resumed. A transcript flushes in
    whole turns, so a fork taken mid-turn carries everything up to the last
    completed turn; the in-flight one stays with the original.

    `404` = no transcript to fork (a fresh window has no file yet). Any
    failure to open the copy rolls the dub back, so failure leaves nothing
    behind."""
    _check_sid(sid)
    import subprocess
    from . import managed
    from .transcripts import _find_session_cwd
    from .hostenv import project_dir_to_agent
    cwd = _find_session_cwd(sid)
    if not cwd:
        raise HTTPException(status_code=404, detail="session not found")
    key, base = "", ""
    for pd in (Path.home() / ".claude" / "projects").iterdir():
        if (pd / f"{sid}.jsonl").exists():
            key = pd.name
            parsed = project_dir_to_agent(pd.name)
            base = parsed[0] if parsed else ""
            break
    if not key:
        raise HTTPException(status_code=404, detail="no transcript to fork")
    # The dub is a jStack plugin binary, and `host/install.sh` installs the
    # host without the plugins tree — so a standalone host does not have one.
    # Asked for it anyway, `subprocess.run` raised FileNotFoundError straight
    # out of the route and the phone got a bare 500 for a feature the machine
    # simply does not carry. Every other optional tier here answers honestly
    # instead (control 503, pict 501, review 503), and this one now says the
    # same thing in the same shape.
    if not _DUB_SESSION or not Path(_DUB_SESSION).exists():
        raise HTTPException(
            status_code=501,
            detail="splitoff needs jStack's dub-session, which this host does "
                   "not have installed")
    out = subprocess.run([str(_DUB_SESSION), sid, key],
                         capture_output=True, text=True)
    new_sid = out.stdout.strip()
    if out.returncode != 0 or not new_sid:
        raise HTTPException(status_code=500,
                            detail=out.stderr.strip() or "dub failed")
    managed.record_open(new_sid, base)
    try:
        managed.open_managed(new_sid, cwd, resume=True)
    except Exception as e:  # noqa: BLE001 — a failed open must roll the dub back
        managed.record_close(new_sid)
        (Path.home() / ".claude" / "projects" / key
         / f"{new_sid}.jsonl").unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"open failed: {e}")
    _board_changed()
    return {"ok": True, "session_id": new_sid, "open": True}


@router.post("/sessions/{sid}/pict")
def pict_session(sid: str, full: bool = False):
    """Render what this session opens with, and name the file it landed in.

    The phone face of `pict`: the thread's ⋯ menu asks, the host renders the
    session's own directory into that seat's pad, and the app opens the path
    this returns. The document goes back as a path and never as text — it is
    served through `/context/file` like every other document the app reads, so
    the read fence stays the one place that decides what may be shown.

    The cwd is asked of the transcript first and of the live tmux pane second,
    because neither covers a session's whole life: a session spawned a moment
    ago has a pane and no transcript, and one long closed has the reverse.

    `404` = a session no longer placeable, or one running outside an agent
    workspace — there is no pad to write into, and a render behind the fence
    would open onto a refusal. `501` = a host without jStack's renderer."""
    _check_sid(sid)
    from . import open_path, pict
    from .scratchpad import session_pad
    from .transcripts import _find_session_cwd

    cwd = _find_session_cwd(sid)
    if not cwd:
        try:
            cwd = str(Path(open_path.pane_cwd(sid)).resolve())
        except (KeyError, OSError):
            raise HTTPException(status_code=404, detail="session not found")
    try:
        pad = session_pad(sid)
    except KeyError:
        raise HTTPException(status_code=404,
                            detail="this session isn't running in an agent "
                                   "workspace — nowhere to put the render")
    try:
        from . import managed, messages
        from .codex_transcript import metadata
        source = messages._find_session_file(sid)
        provider = "codex" if source and metadata(source) else (
            (managed.open_registry().get(sid) or {}).get("engine", "claude"))
        path, title = pict.render(cwd, pad, full=full, engine=provider)
    except FileNotFoundError:
        raise HTTPException(status_code=501, detail="pict isn't installed on this host")
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
        raise HTTPException(status_code=500, detail=str(e) or "pict failed")
    return {"ok": True, "path": str(path), "title": title}


@router.post("/sessions/{sid}/input")
def input_session(sid: str, payload: InputBody):
    """Type text into a managed session's stdin (single gated path)."""
    _check_sid(sid)
    from . import managed
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty input")
    if not managed.send_input(sid, text):
        raise HTTPException(status_code=409, detail="session is not open (managed)")
    return {"ok": True}


def _audit_close(sid, request, review, pristine, outcome) -> None:
    """Record one close attempt in the session store — who asked, for what,
    and what it did.

    Every end of a session on this Mac comes through the endpoint below (Kill
    is the same call with `review=false`), and until this existed nothing
    recorded any of them: answering "who closed my session" meant correlating
    the review log against process forensics against another agent's
    transcript. The client is what actually matters — a UI build verifying
    session controls drives the same production endpoint the user's phone does,
    and the User-Agent is the only thing that tells them apart.

    It lands in `store.session_closes`, keyed by the same sid the rest of the
    store is: a close is a fact about a session, and the store is already the
    long-term home for those. The seat is read back off the session's own row
    at query time, never copied in here.

    Attempts that changed nothing (`kept`, `gone`, `idle`) are recorded too: a
    close that was asked for and declined is exactly what you want to see when
    a session dies a minute later. Never raises — an audit that can break the
    action it records is worse than no audit.

    No request, no row. The whole value here is naming the client, and an
    in-process call has none."""
    if request is None:
        return
    mode = (outcome or {}).get("mode", "error")
    closed = bool((outcome or {}).get("closed"))
    # Nothing ended, so nothing to name. `gone` reached no process at all, and
    # a successful pristine reap is the host's own proof that nothing was ever
    # said in that session — the open-then-back-out flow fires it constantly
    # in ordinary phone use. Recording either buries the rows that are real.
    if mode == "gone" or (pristine and closed):
        return
    try:
        from .store import get_store
        get_store().record_close(
            sid, mode=mode, closed=closed, review=review, pristine=pristine,
            client=getattr(getattr(request, "client", None), "host", "") or "",
            user_agent=request.headers.get("user-agent") or "")
    except Exception:
        pass


@router.post("/sessions/{sid}/close")
def close_session_managed(sid: str, request: Request = None, review: bool = True,
                          pristine: bool = False):
    """Close this session on the Mac — **and the window it was running in**.

    Managed → tmux teardown (review=true exits cleanly so the review hook
    fires; review=false hard-kills for a mode switch). Not managed but live
    in a raw window → SIGTERM the claude holding it and wait for exit.

    `pristine=true` is the conditional reap behind the app's open-then-back-out
    flow: kill only if **nothing was ever said** in this session — no
    transcript, or one holding nothing but harness metadata and hook
    injections (`board.transcript_pristine`). The phone judges "no keystrokes
    ever reached the terminal" on its side, but that judgment goes stale the
    moment someone types into the session's Mac window, so the transcript is
    re-checked here, where the fact lives. Not provably pristine → the session
    is kept (`closed=false, mode="kept"`) — never an error, the caller asked
    a question. A `pid-` row can't prove anything (a raw window's transcript
    may simply not be correlated yet), so it is always kept.

    Closing the window on the Mac already ends the session; this is the same
    event pulled from the other end, so it ends the same way — no window left
    sitting at a dead prompt, looking like work on the desk and like a row on
    the board. The window's tty is read from the process before it is
    signalled and closed once it is actually gone: a kill that fails leaves
    the window exactly where it was.

    A `pid-<n>` row is a running agent CLI with no session identity — a fresh
    window, or a Codex that names itself nowhere. The process IS the session,
    so ending the row means signalling that pid, and `review` picks the signal
    exactly as it does everywhere else. Only pids the process scan currently
    reports are ever signalled: a stale row can't reach an unrelated
    process."""
    outcome = None
    try:
        outcome = _close_session(sid, review, pristine)
        return outcome
    finally:
        # `finally`, not a tail call: the paths below raise 504, and a close
        # that timed out mid-signal is exactly the one worth having a line
        # for. Those land as mode=error.
        _audit_close(sid, request, review, pristine, outcome)


def _close_session(sid: str, review: bool, pristine: bool):
    """The close itself. Contract and rationale live on the endpoint above."""
    import os
    import signal
    import time as _time
    from . import managed
    from .board import pids_holding, end_raw_holders, window_ttys
    m = re.match(r"^pid-(\d+)$", sid)
    if m:
        if pristine:
            return {"ok": True, "closed": False, "mode": "kept"}
        pid = int(m.group(1))
        from .procscan import get_claude_processes
        try:
            procs = get_claude_processes().get("processes", [])
        except Exception:
            procs = []
        if not any(p.get("pid") == pid for p in procs):
            return {"ok": True, "closed": False, "mode": "gone"}
        ttys = window_ttys([pid])
        # `review` is the caller's choice of end, on this row like any other:
        # SIGTERM lets the CLI exit cleanly, SIGKILL is for one that won't.
        # This branch used to always SIGTERM, so Kill on an unnamed row was a
        # softer end wearing the hard end's label — and a wedged process (the
        # exact reason to reach for Kill) survived it.
        try:
            os.kill(pid, signal.SIGTERM if review else signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            return {"ok": True, "closed": False, "mode": "gone"}
        deadline = _time.time() + 10
        while _time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                managed.close_windows(ttys)
                _board_changed()
                return {"ok": True, "closed": True, "mode": "window"}
            _time.sleep(0.5)
        raise HTTPException(status_code=504,
                            detail="claude on the Mac didn't exit — close the window there")

    _check_sid(sid)
    if pristine and not board.transcript_pristine(sid):
        return {"ok": True, "closed": False, "mode": "kept"}
    if managed.is_open(sid):
        closed = managed.close_managed(sid, review=review)
        _board_changed()
        return {"ok": True, "closed": closed, "mode": "managed"}

    pids = pids_holding(sid)
    if not pids:
        return {"ok": True, "closed": False, "mode": "idle"}
    ttys = window_ttys(pids)
    # review=False must mean NO review: SIGTERM lets claude exit "cleanly"
    # and its SessionEnd hook fires anyway. SIGKILL skips all ceremony —
    # the transcript is append-only, resume tolerates a truncated tail.
    sig = signal.SIGTERM if review else signal.SIGKILL
    if end_raw_holders(sid, sig):
        managed.close_windows(ttys)
        _board_changed()
        return {"ok": True, "closed": True, "mode": "raw"}
    raise HTTPException(status_code=504,
                        detail="claude on the Mac didn't exit — close the window there")


def _review_spawn_bin() -> Path | None:
    """Resolve the jstack `session-review-spawn` engine from installed plugins."""
    candidate = plugin_paths.jstack_bin("session-review-spawn")
    return candidate if candidate.is_file() else None


@router.post("/sessions/{sid}/review")
def review_session(sid: str):
    """Close a session with intent: fire the same post-session review the
    SessionEnd hook runs (reconcile follow-ups, timeline)."""
    _check_sid(sid)
    import subprocess
    from .messages import _find_session_file
    from .codex_transcript import metadata
    transcript = _find_session_file(sid)
    if not transcript:
        raise HTTPException(status_code=404, detail="session file not found")
    spawn = _review_spawn_bin()
    if not spawn:
        raise HTTPException(status_code=503, detail="review engine unavailable")
    native_id = metadata(transcript).get("id") or sid
    subprocess.Popen([str(spawn), native_id, str(transcript)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return {"ok": True, "review": "spawned"}


# Same bearer and managed-access gate as every other product route.
from .update_routes import router as update_router
router.include_router(update_router)
