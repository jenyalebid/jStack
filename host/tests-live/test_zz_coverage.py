"""The gate: every route the host serves was exercised against a real host.

This is the file that makes "we test every action" a fact instead of a claim.
The inventory is read off the routers at runtime, so a route added tomorrow is
uncovered tomorrow — there is no second list to keep in sync, because a second
list is how coverage claims rot.

**`zz` is load-bearing.** pytest collects files in alphabetical order and this
one reads what every other file recorded, so it has to sort last. A prettier
name would make the gate pass by having nothing to check yet.

A route may be left uncovered only by appearing in `UNCOVERED` with a reason
that is true. That list is the suite's own honesty: it prints on every run, so
a skip cannot hide in a green suite. Adding to it is a decision someone makes
out loud, not a silence.
"""

from __future__ import annotations

import pytest

from conftest import BASE_URL


#: Routes with no live test, and why. Every entry prints on every run.
#:
#: Keep this empty where you can. Each line is a live action nobody has proven
#: on a real host, and "it is hard to test" is how a surface stays untested
#: for a year.
UNCOVERED: dict[tuple[str, str], str] = {
    ("POST", "/api/jremote/v1/tunnel/pair"):
        "mints a WireGuard peer bundle. The guest-to-guest handshake is a "
        "proven dead end on this Mac (~/Systems/vm/SYSTEM.md), so a live "
        "call here would enrol a peer that can never complete — the install "
        "path is covered by the mesh runbook instead.",

    # The update surface. These twelve became visible to this gate only when
    # `_walk` learned to follow an include marker; they were never exercised
    # here, and the honest reason is that an in-process read is the weakest
    # thing that could be said about them. `hub/update` drives the whole
    # surface end to end on two real builds — a source build, a queued
    # release, a heartbeat, an artifact fetch and a leaf asking its hub for
    # all of it — which is why nobody noticed the hole. Issue #171.
    ("GET", "/api/jremote/v1/updates/latest"):
        "read of the release the host would move to. Proven end to end by the "
        "hub/update journey against two real builds; an unauthenticated-shape "
        "live read would add nothing that journey does not already assert.",
    ("GET", "/api/jremote/v1/updates/inventory"):
        "lists the releases on the feed. Same journey, same reason.",
    ("GET", "/api/jremote/v1/updates/source"):
        "reports the ref and build the host is tracking. Same journey.",
    ("POST", "/api/jremote/v1/updates/source/ref"):
        "moves the host onto another ref — a live call here would repoint the "
        "rig mid-suite. hub/update owns it.",
    ("POST", "/api/jremote/v1/updates/source/build"):
        "builds a release. Minutes of work and a signed artifact on the rig; "
        "hub/update is the only sane place to call it.",
    ("POST", "/api/jremote/v1/updates/queue"):
        "queues an update the updater then applies — it would replace the "
        "host this suite is talking to, mid-run.",
    ("POST", "/api/jremote/v1/updates/heartbeat"):
        "the updater reporting its own progress; there is no updater in this "
        "process to report for. hub/update reads the rows it writes.",
    ("GET", "/api/jremote/v1/updates/artifact/{release}/{filename}"):
        "serves release bytes. Exercised by every guest that updates.",
    ("POST", "/api/jremote/v1/managed/updates/request"):
        "a leaf asking its hub to update it. Needs two machines; shell_adopt "
        "and hub/update have them.",
    ("POST", "/api/jremote/v1/managed/updates/check"):
        "a leaf asking what its hub has for it. Needs a real parent "
        "credential, which this suite's device token is not.",
    ("POST", "/api/jremote/v1/managed/updates/heartbeat"):
        "a leaf reporting an update it is applying. Same, and it writes fleet "
        "rows a suite has no business inventing.",
    ("GET", "/api/jremote/v1/managed/updates/artifact/{release}/{filename}"):
        "a leaf pulling release bytes through its hub. Same.",

    # Delegated minting. Visible to this gate only now: `grant_router` was
    # mounted by create_app and read by nothing here. Issue #171.
    ("POST", "/api/jremote/v1/delegate/mint"):
        "mints a device credential on behalf of a parent that holds a grant "
        "this machine issued. The caller must be that parent; a suite holding "
        "a device token is the one caller that must never succeed, and the "
        "refusal alone would not say the grant path works. leaf/join and the "
        "agent-tools journey exercise it with a real hub.",
    ("POST", "/api/jremote/v1/delegate/access"):
        "the same grant, read instead of spent. Same caller problem, same "
        "journeys.",
}


def _walk(container, base: str, found: set[tuple[str, str]]) -> None:
    """Collect `(METHOD, path)` from `container`, descending into its includes.

    FastAPI 0.141 stopped copying an included router's routes into the parent
    and started appending one lazy marker instead, so a flat read of `.routes`
    hits that marker, finds no `.path` on it, and loses every route behind it.
    Descending reads both shapes: the old one has no marker to follow, the new
    one has nothing else. This is not a detail — the twelve `/updates` routes
    live behind exactly one such marker.
    """
    for route in getattr(container, "routes", ()):
        context = getattr(route, "include_context", None)
        inner = getattr(context, "included_router", None)
        if inner is not None:
            # The marker carries the prefix the sub-router was mounted under;
            # a route declared on a prefixed router already has it baked in.
            _walk(inner, base + (getattr(context, "prefix", "") or ""), found)
            continue
        path = base + route.path
        methods = getattr(route, "methods", None)
        if not methods:  # a websocket route has no HTTP method
            found.add(("WS", path))
            continue
        for m in methods:
            if m in ("HEAD", "OPTIONS"):  # starlette adds these itself
                continue
            found.add((m, path))


def _inventory() -> set[tuple[str, str]]:
    """Every (METHOD, path) the host serves, read off the app it actually builds.

    `create_app()` rather than a hand-picked tuple of routers: the app is the
    surface, so a router mounted tomorrow is in the inventory tomorrow with
    nobody remembering to come back here. Naming three of the four routers is
    how `grant_router` served `/delegate/mint` and `/delegate/access` for a
    release while this gate reported full coverage.

    Building the app runs no lifespan and starts nothing; it registers routes.
    """
    from jstack_host.server import create_app

    found: set[tuple[str, str]] = set()
    _walk(create_app(), "", found)
    return found


@pytest.mark.skipif(not BASE_URL, reason="live suite is opt-in")
def test_every_route_was_exercised_against_a_real_host(exercised):
    inventory = _inventory()
    missing = sorted(inventory - exercised - set(UNCOVERED))

    for route, reason in sorted(UNCOVERED.items()):
        print(f"  UNCOVERED {route[0]:6} {route[1]} — {reason}")
    print(f"  live coverage: {len(inventory) - len(missing) - len(UNCOVERED)}"
          f"/{len(inventory)} routes exercised, {len(UNCOVERED)} declared "
          f"uncovered, {len(missing)} untested")

    assert not missing, (
        "routes the host serves that no live test called:\n  "
        + "\n  ".join(f"{m:6} {p}" for m, p in missing)
        + "\n\nAdd a live test, or add the route to UNCOVERED with a reason.")


@pytest.mark.skipif(not BASE_URL, reason="live suite is opt-in")
def test_the_uncovered_list_has_no_stale_entries(exercised):
    """An excuse for a route that no longer exists, or that is now tested, is
    an excuse nobody rechecked. Both directions are a failure: the first means
    the list is fiction, the second means it is understating coverage."""
    inventory = _inventory()
    gone = sorted(set(UNCOVERED) - inventory)
    assert not gone, f"UNCOVERED names routes the host does not serve: {gone}"

    now_tested = sorted(set(UNCOVERED) & exercised)
    assert not now_tested, (
        f"UNCOVERED claims these are untestable, but a test called them: "
        f"{now_tested}. Delete the excuse.")


def test_the_inventory_is_not_empty():
    """A router that imported to nothing would make every coverage claim above
    vacuously true. Runs without a live host on purpose — this one is about
    the gate, not the machine.

    The floor is above the count a *flat* read of the routers returns, so a
    regression that loses the include markers again fails here rather than
    quietly shrinking the surface this suite claims to cover."""
    assert len(_inventory()) > 100
