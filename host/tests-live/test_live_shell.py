"""Shell access (#131) against a real host — the three routes, from off-console.

What a single live host can prove about shell grants is the *gates*, and that
is not a consolation prize: every one of these routes is a refusal for the
caller this suite is. The suite calls over the LAN from a machine that is not
the host, with a device credential that belongs to no adopted machine, against
a guest that is nobody's leaf — so the console gate, the managed-credential
gate and the hub's 409 are exactly the behaviour of the machine under test, and
each is a defect if it answers anything else.

**The positive halves need more than one Mac**, and they have a home already:
`host/tools/managed_update_accept.py` drives them as the `shell_adopt` and
`shell_flip` journeys across a real hub and a real leaf — a key presented at
adoption, a grant flipped on the console, the poked machine rewriting its own
authorized set. Asserting those here would mean asserting them against a rig
that cannot have them, which is how a suite ends up green about nothing.

**Nothing here writes.** A flip refused at the gate stores nothing, and the
unknown-machine key is one no store has a row for — so unlike the device
tests, this file leaves no artifact to clean up.
"""

from __future__ import annotations

import pytest

from conftest import BASE_URL

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not BASE_URL, reason="live suite is opt-in")]


def test_a_grant_flip_is_refused_off_the_hub_console(api, host_identity,
                                                     scratch_name):
    """Who may hand one machine's agents shell on another: the menu bar, and
    nothing else.

    The expected refusal is read off `/host.features.device_management`, which
    is `managed_access.console(request)` — the same predicate the route's
    `require_console` calls. So this asserts the gate and the flag agree for
    the caller in front of them: a host that advertises console authority to a
    LAN client and then refuses it, or advertises none and accepts the flip,
    fails here, and a capability map that disagrees with its own gate is
    invisible to a test of either side alone.

    The `{key}` is a machine no store has a row for, so the console branch is
    the 404 out of `shell_grants.flip` rather than a real grant: this suite
    does not get to rewrite a live fleet's authorized sets.
    """
    body = {"src": host_identity["host_id"], "allowed": True}
    r = api.post("/hosts/{key}/shell", fmt={"key": f"never-{scratch_name}"},
                 json=body)

    if host_identity["features"].get("device_management"):
        assert r.status_code == 404, (
            f"the hub console flipped a grant onto a machine it has no row "
            f"for ({r.status_code}): {r.text[:300]}")
    else:
        assert r.status_code == 403, (
            f"a caller the host does not treat as its console got "
            f"{r.status_code} from a grant flip: {r.text[:300]}")
    assert r.status_code >= 400, "a shell grant flip must never answer 2xx here"


def test_a_grant_flip_of_a_machine_onto_itself_is_refused(api, host_identity):
    """`src == dst` is not a no-op to accept quietly — a machine does not need
    a grant to reach itself, and a stored self-pair is a row every later
    `shell_sources_for` read has to know to ignore.

    Off-console this is the 403 first; the assertion is that it is refused
    either way, since both refusals are correct and which one arrives depends
    on where the caller stands.
    """
    key = host_identity["host_id"]
    r = api.post("/hosts/{key}/shell", fmt={"key": key},
                 json={"src": key, "allowed": True})
    assert r.status_code in (403, 404), (
        f"host accepted a shell grant from a machine to itself "
        f"({r.status_code}): {r.text[:300]}")


def test_pulling_a_shell_set_needs_an_adopted_machines_credential(api):
    """`POST /managed/shell` answers a *machine*, never a device.

    The credential this suite holds is a device token on the host, which
    `managed_access.leaf_for_device` maps to no adopted machine — so the honest
    live answer is 403, and that is the assertion. It is also the one that
    matters most about this route: the shell set names every public key a
    machine will authorize, and a device that could pull it would be reading
    the fleet's access table through a route meant for the machine it is about.

    The positive pull is the `shell_adopt` journey in
    `host/tools/managed_update_accept.py`, which has a second Mac to be the
    leaf; the route is the same compute the adoption handshake answers, so a
    flip and a joiner run can never disagree.
    """
    r = api.post("/managed/shell", json={})
    assert r.status_code == 403, (
        f"POST /managed/shell answered {r.status_code} to a plain device "
        f"credential: {r.text[:300]}. A 422 here means the route grew a body "
        f"this test does not present — the gate is what is being asserted, so "
        f"send the new shape rather than relaxing the status.")


def test_a_hub_refuses_the_refresh_poke_and_a_leaf_acts_on_it(api, host_identity):
    """The poke carries no key material, so the only thing it can be wrong
    about is *who it is for*.

    A hub has no parent to pull a shell set from, and `409` is the answer that
    says so; on a managed machine the same call rewrites the user-writable half
    of its own authorized set and reports the steps. Which branch runs is read
    off `/host.features.managed_host` — `managed_access.is_leaf()`, the same
    predicate the route gates on — so this test is also the check that the flag
    and the route agree about what this machine is.
    """
    r = api.post("/shell/refresh", json={})

    if not host_identity["features"].get("managed_host"):
        assert r.status_code == 409, (
            f"a host that says it is not managed answered {r.status_code} to a "
            f"shell refresh: {r.text[:300]} — a hub pulling a parent's shell "
            f"set has no parent to pull from")
        return

    assert r.status_code == 200, f"{r.status_code}: {r.text[:300]}"
    steps = r.json().get("steps")
    assert isinstance(steps, list), (
        f"a refresh must report what it rewrote: {r.text[:300]}")
    for step in steps:
        assert isinstance(step, dict) and step.get("step"), (
            f"an unnamed step in a refresh report: {step}")
