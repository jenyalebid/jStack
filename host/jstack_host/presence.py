"""Whether a machine this hub adopted is on the mesh *right now*.

There are two ways to adopt a Mac and they are not alternatives — each is the
only one that works for its own machine. A Mac that can reach this hub redeems
a code where it stands, over the tunnel it already holds. A Mac that cannot
gets the tunnel carried to it in a file, because redeeming needs a route and
the route is what redeeming hands back. Nothing steered between them, so the
carried file was offered for machines that had no use for it: the operator
walked a USB stick to a Mac that was already answering this hub over the mesh,
and that Mac's bundle was rewritten with a fresh code on the way out (#61).

Steering needs one fact, and the fact is not "was this machine ever adopted" —
a registry row survives the machine being wiped. It is whether the machine is
reachable from here at this moment, which is two separate things that both
have to hold:

  · **Its peer is live.** `wg0.conf` still carries an entry under its name. A
    row in the registry with no peer behind it is a machine this hub let in
    once and cannot route to now.
  · **It answers.** Something replies at the address the peer was issued. A
    live peer entry is the hub's own bookkeeping; it says nothing about
    whether the far Mac is powered on, still holds its half of the keypair, or
    was reinstalled last week. That machine genuinely needs the carried file,
    and a check that refused it on the peer entry alone would strand it.

Where neither can be established — this Mac cannot read its own peer table —
the answer is `None` and not `False`. "I could not tell" and "it is offline"
lead to opposite actions, and a probe that reports state it cannot observe is
worse than no probe.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

#: A Mac on the mesh answers in milliseconds; a Mac that is off never answers
#: at all. One second is long enough for the first and short enough that a
#: dialog can ask about every adopted machine before it draws.
HEALTH_TIMEOUT = 1.0

#: How many machines are probed at once. The wait is all network, so the pool
#: only has to be wide enough that a hub's worth of leaves costs one timeout
#: rather than one per machine.
PROBE_WIDTH = 8


def answers(address: str, port: int, timeout: float = HEALTH_TIMEOUT) -> bool:
    """Does anything answer at that address.

    `/api/health` because it is the one route deliberately outside the bearer
    gate: the question here is "is anyone there", and a probe that needed a
    token would be answering a different one — whether this hub's credential
    is still good over there — and would read a revoked token as a machine
    that has left the mesh.

    An HTTP error counts as an answer, on purpose. A 404 means something
    accepted the connection and replied, which is the whole question; on a Mac
    running an embedded host the health route belongs to the larger app and
    may not be mounted at all.
    """
    if not address:
        return False
    url = f"http://{address}:{port or 9090}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as reply:
            reply.read(1)
        return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def peer_of(name: str) -> str:
    """The WireGuard peer name a machine called `name` holds, or "".

    `enrolment.peer_name` and not a second slug rule: the peer was created
    under whatever that function returned at adoption, so anything else here
    would look for an entry that was never written.
    """
    from . import enrolment
    return enrolment.peer_name(name or "")


def roster() -> list[dict]:
    """Every machine this hub adopted, each with its peer name and `online`.

    `online` is `True`, `False`, or `None` when this Mac could not read its own
    peer table — see the module docstring. The health probe runs only for rows
    whose peer is live, so a hub whose leaves are all long gone pays nothing
    for asking.
    """
    from . import tunnel
    from .store import get_store

    rows = [dict(r) for r in get_store().list_hosts()]
    try:
        peers: set[str] | None = tunnel.live_peers()
    except (tunnel.TunnelError, OSError):
        peers = None

    for row in rows:
        row["peer"] = peer_of(row.get("name") or "")
        row["online"] = (None if peers is None
                         else bool(row["peer"]) and row["peer"] in peers)

    reachable = [r for r in rows if r["online"]]
    if reachable:
        with ThreadPoolExecutor(max_workers=PROBE_WIDTH) as pool:
            for row, up in zip(reachable, pool.map(
                    lambda r: answers(r.get("address") or "", r.get("port") or 0),
                    reachable)):
                row["online"] = up
    return rows


def live_on_mesh(name: str) -> dict | None:
    """The adopted machine `name` names, when it is live on this mesh now.

    `None` for a machine that is not, was never adopted, or cannot be judged —
    every one of which means the caller has no grounds to refuse the carried
    file. The refusal is the narrow answer here on purpose: being wrong in this
    direction offers a file nobody needed, and being wrong in the other one
    blocks the only route a stranded Mac has.
    """
    from . import tunnel
    from .store import get_store

    peer = peer_of(name)
    if not peer:
        return None
    try:
        if peer not in tunnel.live_peers():
            return None
    except (tunnel.TunnelError, OSError):
        return None
    # The peer table first and the probe second, never the roster: this runs
    # while someone waits on a dialog, and every other machine's health is not
    # the question being asked.
    for row in get_store().list_hosts():
        if peer_of(row.get("name") or "") != peer:
            continue
        if answers(row.get("address") or "", row.get("port") or 0):
            return {**dict(row), "peer": peer, "online": True}
    return None
