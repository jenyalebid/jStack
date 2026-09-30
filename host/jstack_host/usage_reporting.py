"""Whether a Mac's OWN local client draws Home's Usage section.

Three states, and the third one is the point. `client` leaves the choice to
each client's own setting — the switch in the app, one device one answer.
`hidden` and `available` take that choice away for the client running ON this
Mac. Nothing here governs a phone: `/host` answers this field to a **loopback**
caller only, because a policy a remote device is not subject to is a policy it
must not be handed as if it were its own.

Who owns the answer depends on what the machine is, and that split is the same
one the whole leaf contract is built on:

- A **hub** owns its own. It is the console at its own menu bar, there is no
  row above it, and the state lives in this machine's state dir.
- A **leaf** does not. Its hub holds the row (`hosts.usage_reporting`), beside
  `sees_home` and `sees_leaves`, flipped from the same menu, for the reason the
  app already says out loud on a managed Mac: "Access and visibility are
  controlled from the hub's menu bar." A leaf that could flip its own would be
  the machine whose view was being restricted overruling the hub that
  restricted it.

So a leaf CACHES what its parent said. `/host` is the call that must work
before any screen, so it answers out of the cache and never spends a round trip
to the hub; the cache is refreshed off the parent call the leaf's own client
already makes (`/managed/hosts`, which carries the leaf's own row back in its
`self` block), and a flip at the hub pokes the leaf to pull at once. A missed
poke degrades to "at the next roster pull", never to a leaf enforcing a policy
its hub has retired — the same degradation `shell_grants` accepts, and for the
same reason: the pull rewrites the whole answer rather than applying a delta.

A hub that predates the field says nothing, and silence is `client` — an old
hub has no policy, and inventing `hidden` out of an absent field would hide a
section nobody chose to hide.
"""

from __future__ import annotations

import json
import threading

from . import hostenv

#: The client's own setting decides. The default, everywhere, always.
CLIENT = "client"
#: Forced off: the local client draws no Usage section whatever its setting.
HIDDEN = "hidden"
#: Forced on: the local client draws it whatever its setting.
AVAILABLE = "available"

STATES = (CLIENT, HIDDEN, AVAILABLE)

#: The leaf route a poke hits. Carries no payload — the machine pulls.
REFRESH_PATH = "/api/jremote/v1/usage/refresh"

_STATE = hostenv.state_dir() / "jremote_usage_reporting.json"
_lock = threading.Lock()


class UnknownState(ValueError):
    """A state that is not one of `STATES`.

    Refused rather than stored, for `agent_prefs`' reason: a typo that stores
    cleanly is a switch that appears to work and changes nothing on the Mac.
    """


def normalise(state) -> str:
    if state not in STATES:
        raise UnknownState(f"unknown usage reporting state {state!r}")
    return state


def _load() -> dict:
    try:
        d = json.loads(_STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return d if isinstance(d, dict) else {}


def _store(**fields) -> None:
    with _lock:
        d = _load()
        d.update(fields)
        _STATE.parent.mkdir(parents=True, exist_ok=True)
        _STATE.write_text(json.dumps(d, indent=2, sort_keys=True))


def own() -> str:
    """This machine's own word about its own client. A hub's answer."""
    state = _load().get("own")
    return state if state in STATES else CLIENT


def set_own(state: str) -> str:
    """Store this machine's own choice. The console route's write."""
    _store(own=normalise(state))
    return state


def cached_parent() -> str:
    """What this leaf's hub last said about it."""
    state = _load().get("parent")
    return state if state in STATES else CLIENT


def note_parent(state) -> str:
    """Record the hub's verdict, straight off a parent response.

    Tolerant on purpose: an absent or unrecognised field is a hub with no
    policy for this machine, which is `client` — and writing that down is how a
    retired `hidden` stops being enforced on the leaf that cached it. The
    recognised-values check is what stops a future state this build never heard
    of from being stored as itself and then read back as gospel.
    """
    resolved = state if state in STATES else CLIENT
    # Only on a change. This is called off the roster pull, which a client
    # makes four times a minute, and a state dir rewritten on every poll is
    # wear for nothing — the answer it would write is the answer already there.
    if resolved != cached_parent():
        _store(parent=resolved)
    return resolved


def effective() -> str:
    """The state this machine's local client is subject to, right now."""
    from . import managed_access
    return cached_parent() if managed_access.is_leaf() else own()


def poke(host_key: str, *, poster=None) -> dict:
    """Tell one leaf its policy changed, so it pulls now instead of later.

    The hub half of the flag, and deliberately shaped like
    `shell_grants.refresh_on`: graded, never raising. The row at the hub is
    already the authority by the time this runs, so an unreachable leaf is a
    machine that catches up on its next roster pull, not a failed flip — and
    the pull rewrites the whole answer, so any later poke converges.

    No payload rides the poke. A leaf that was handed its state would be a leaf
    taking policy from whatever could reach its port; it pulls through its own
    parent credential instead.
    """
    from . import grants
    from .store import get_store
    step = {"step": f"usage-refresh:{host_key}", "ok": False, "note": ""}
    row = get_store().host_row(host_key)
    if row is None or row["deleted"]:
        step["note"] = "unknown machine"
        return step
    try:
        access = grants.mint_on(dict(row), "Usage policy refresh",
                               poster=poster, owner_id="host-internal")
        status, body = (poster or grants._httpx_post)(
            f"http://{access['address']}:{access['port']}{REFRESH_PATH}",
            {}, access["token"])
    except grants.GrantError as exc:
        step["note"] = f"{exc} — it picks the policy up at its next roster pull"
        return step
    if status != 200:
        step["note"] = (f"the machine answered {status}: "
                        f"{body.get('detail', 'no detail')} — it picks the "
                        "policy up at its next roster pull")
        return step
    step["ok"] = True
    step["note"] = f"applied {body.get('usage_reporting', '?')}"
    return step
