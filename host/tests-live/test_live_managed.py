"""The fleet's gates, against a real host: who may ask a machine about machines.

Six routes, one subject: every one of them decides whether the *caller* is the
kind of caller it is for, and answers nothing until it has. This suite is the
wrong caller for all six on purpose — a device credential that
`managed_access.leaf_for_device` maps to no adopted machine, arriving over the
LAN from a machine that is not the host — so the refusal is the behaviour of
the machine under test, not a stand-in for one.

**Each expected refusal is read off the flag the route gates on**, never
hardcoded: `features.device_management` is `managed_access.console(request)`,
`features.managed_host` is `is_leaf()`, `features.self_disconnect` is
`can_disconnect(...)`. So each test is also the check that the capability map
and the gate agree about the caller in front of them — a host that advertises
console authority to a LAN client and then refuses it is broken in a way no
test of either side alone can see.

**Nothing here writes.** A refusal at the gate stores nothing, every `{key}` is
one no store has a row for, and the disconnect test asserts the credential it
called with is still alive afterwards. The positive halves of these routes need
a second Mac and have one: the journeys in
`host/tools/managed_update_accept.py` drive adoption, grants and visibility
across a real hub and a real leaf.
"""

from __future__ import annotations

import pytest

from conftest import BASE_URL

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not BASE_URL, reason="live suite is opt-in")]

#: The refusal every `_managed_leaf` route owes a credential that belongs to no
#: adopted machine. Asserted as an exact status on purpose: 422 instead would
#: mean the route's body model changed under this test, and the fix is to
#: present the new shape, never to widen the status.
NOT_A_MACHINE = 403

_BODY_WARNING = ("A 422 here means the route grew or changed a body this test "
                 "does not present — the credential gate is what is being "
                 "asserted, so send the new shape rather than relaxing the "
                 "status.")


def _refused(r, *, want: int, why: str) -> None:
    assert r.status_code == want, f"{r.status_code}: {r.text[:300]} — {why}"


# ── the three routes a machine calls about other machines ──

def test_a_device_cannot_ask_for_the_fleet_a_machine_would_see(api):
    """`POST /managed/hosts` answers a *machine*'s view — home and the leaves
    its own row says it may see. A device credential has no such row, and one
    that could read this would enumerate the estate through a route about the
    machine it is not."""
    _refused(api.post("/managed/hosts", json={}), want=NOT_A_MACHINE,
             why="a plain device credential is not an adopted machine. "
                 + _BODY_WARNING)


def test_a_device_cannot_mint_a_machines_grant(api, host_identity):
    """`POST /managed/grant` hands back a credential for reaching another
    machine. The gate is checked before the key is looked at, which is why the
    body names this host's own id and still gets nowhere: a device that could
    mint here would hold access the console never granted it."""
    _refused(api.post("/managed/grant", json={"key": host_identity["host_id"]}),
             want=NOT_A_MACHINE,
             why="minting on a machine's behalf requires being that machine. "
                 + _BODY_WARNING)


def test_a_device_cannot_ask_whether_another_device_may_reach_it(api):
    """`POST /managed/authorize` is a leaf asking its parent to vouch for a
    caller. Answered for a device credential it would be an oracle: ask about
    any id and learn who may reach the machine you are not."""
    _refused(api.post("/managed/authorize", json={"device_id": "live-suite-asking"}),
             want=NOT_A_MACHINE,
             why="vouching is between a parent and its adopted machine. "
                 + _BODY_WARNING)


# ── the two console-authority routes ──

def test_setting_a_machines_visibility_is_the_consoles_alone(api, host_identity,
                                                             scratch_name):
    """Who a machine may see is the hub menu bar's decision.

    The `{key}` is a machine no store has a row for, so even the console branch
    is a 404 and no real leaf's visibility is rewritten — this suite does not
    get to re-draw a live fleet's sight lines to prove a gate.
    """
    r = api.post("/hosts/{key}/visibility", fmt={"key": f"never-{scratch_name}"},
                 json={"sees_home": True, "sees_leaves": False})

    if host_identity["features"].get("device_management"):
        _refused(r, want=404, why="the console set visibility on a machine the "
                                  "store has no row for")
    else:
        _refused(r, want=403, why="a caller the host does not treat as its "
                                  "console reached a console-only route")


def test_asking_for_a_machines_credential_is_refused_for_a_machine_nobody_adopted(
        api, host_identity, scratch_name):
    """`POST /hosts/{key}/grant` is the one route in this file whose gate is
    NOT the console.

    On a hub it is `may_reach` plus a host row, so an unknown key is a 404; on a
    managed machine the route is loopback-only, because a leaf's answer comes
    from its parent and a remote asking is told to go there instead. Which one
    this host is, is `features.managed_host` — `managed_access.is_leaf()`, the
    same predicate the route branches on.
    """
    r = api.post("/hosts/{key}/grant", fmt={"key": f"never-{scratch_name}"},
                 json={"name": scratch_name})

    if host_identity["features"].get("managed_host"):
        _refused(r, want=403, why="a managed machine must send a remote asker "
                                  "to the parent hub, not answer it")
    else:
        _refused(r, want=404, why="a hub minted access on a machine it has no "
                                  "row for")


# ── the credential that ends itself ──

def test_the_hosts_own_credential_cannot_disconnect_itself(api, host_identity):
    """`POST /device/disconnect` revokes the CALLER, so the only safe live
    assertion is the guard — and the guard is the interesting half.

    `features.self_disconnect` is `managed_access.can_disconnect(...)` for this
    exact credential, so the flag says in advance what the route will do. The
    suite runs as `host-internal`, one of the two shared rows: re-keying or
    revoking it locks out every other holder of that token — the command line,
    the host's own plumbing — from a request that carried no authority over
    them. So the refusal is asserted, and then that the credential still opens
    the door, because a guard that refuses and revokes anyway would end the run
    and read as an auth bug.

    Minting a throwaway device to disconnect instead is not reachable from
    here: both `POST /devices` and `POST /enrolment/codes` are hub-console
    (loopback-on-the-hub) actions, and this suite calls over the LAN.
    """
    if host_identity["features"].get("self_disconnect"):
        pytest.skip(
            "this run's credential is a disconnectable device row, not the "
            "host's own — calling /device/disconnect would revoke the token "
            "every later test authenticates with. Point the suite at a rig "
            "whose token is scripts/live-vm-test.sh's internal one, or cover "
            "this route from the leaf side (managed_update_accept.py)")

    r = api.post("/device/disconnect", json={})
    _refused(r, want=403,
             why="the host says this credential cannot disconnect itself, and "
                 "the route let it try anyway")

    alive = api.call("GET", "/host")
    assert alive.status_code == 200, (
        f"the credential is gone after a refused disconnect "
        f"({alive.status_code}) — the guard refused and revoked anyway")
