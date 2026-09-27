"""Credentials, against a real host: minting, renaming, revoking, pairing.

This is the surface that decides who gets in, so it is the surface where an
in-process pass is worth least. Every test here goes through the real device
store on a real machine — the same file the running host reads on its next
request — because the defects this area actually produces are about *rows
left behind*: a re-pair that adds a second credential instead of re-keying the
first, a revoke that marks a row dead while the token still opens the door.

**The standing hazard: never revoke the suite's own device.** `POST
/devices/{id}/revoke` is the one route here that can end the run — the token
in `JREMOTE_LIVE_TOKEN` belongs to a row in the same table, and revoking it
would fail every test that sorts after this file with a 401 that looks like an
auth bug rather than self-harm. Everything below revokes only a row it minted
itself, and asserts the id differs from the caller's first.
"""

from __future__ import annotations

import pytest

from conftest import BASE_URL

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not BASE_URL, reason="live suite is opt-in")]


def _ids(devices) -> list[str]:
    rows = devices["devices"] if isinstance(devices, dict) else devices
    return [d.get("id") or d.get("device_id") for d in rows]


def _minted(body: dict) -> tuple[str, str]:
    """The `(id, token)` out of a mint or a redemption.

    Both routes answer `{"device": {...}, "token": ...}` — the row nested, the
    credential beside it — and the id is read from the nested row rather than
    the top level, which is where a first draft of these tests looked and
    found `None`. Shared by mint and redeem so one shape change moves one
    function instead of six assertions.
    """
    row = body.get("device") if isinstance(body.get("device"), dict) else body
    return (row.get("id") or row.get("device_id") or "",
            body.get("token") or body.get("device_token") or "")


def test_the_device_roster_lists_the_caller(api):
    """The suite authenticated with a real token, so its own row must be in
    the table it is reading. A roster that cannot see the device asking is how
    a host ends up unable to revoke the credential in front of it."""
    devices = api.ok("GET", "/devices")
    assert _ids(devices), "a host serving an authenticated request has ≥1 device"


def test_a_minted_device_appears_then_renames_then_revokes(api, scratch_name, console):
    """The whole lifecycle in one test, deliberately.

    Split into three, a failure in the middle leaves a live credential on the
    host for every later run to trip over — and this suite is pointed at a VM
    precisely because it writes. One test owns the row it creates from mint to
    revoke, so the table is left as it was found.
    """
    minted = api.ok("POST", "/devices", json={"name": scratch_name})
    new_id, token = _minted(minted)
    assert new_id, f"mint returned no device id: {minted}"
    assert token, "a minted device with no token cannot be used"

    caller_rows = api.ok("GET", "/devices")
    assert new_id in _ids(caller_rows), "minted device is absent from the roster"

    renamed = f"{scratch_name}-renamed"
    api.ok("POST", "/devices/{device_id}/rename", fmt={"device_id": new_id},
           json={"name": renamed})
    after = api.ok("GET", "/devices")
    rows = after["devices"] if isinstance(after, dict) else after
    row = next((d for d in rows
                if (d.get("id") or d.get("device_id")) == new_id), None)
    assert row is not None, "renamed device vanished from the roster"
    assert row.get("name") == renamed, f"rename did not stick: {row}"

    api.ok("POST", "/devices/{device_id}/revoke", fmt={"device_id": new_id})


def test_a_revoked_token_stops_opening_the_door(api, scratch_name, console):
    """Revocation is only real if the credential stops working — a row flagged
    dead while its token still authenticates is the exact shape of a security
    control that reports success and does nothing. In-process tests check the
    flag; only a live host can check the door.
    """
    minted = api.ok("POST", "/devices", json={"name": scratch_name})
    new_id, token = _minted(minted)
    assert new_id and token, f"mint returned nothing usable: {minted}"

    alive = api.call("GET", "/host", token=token)
    assert alive.status_code == 200, (
        f"a freshly minted token was refused: {alive.status_code} {alive.text[:200]}")

    api.ok("POST", "/devices/{device_id}/revoke", fmt={"device_id": new_id})

    dead = api.call("GET", "/host", token=token)
    assert dead.status_code == 401, (
        f"revoked token still authenticates ({dead.status_code}) — the revoke "
        f"flagged a row and left the door open")


def test_an_enrolment_code_is_minted_listed_and_revoked(api, scratch_name, console):
    """The off-LAN pairing path's management half. The code itself appears in
    the mint response and nowhere else, so the listing is checked for the
    *record* — who minted it, what it is for — never for the secret."""
    minted = api.ok("POST", "/enrolment/codes",
                    json={"name": scratch_name, "kind": "device"})
    code = minted.get("code")
    assert code, f"minted an enrolment code with no code in it: {minted}"

    listed = api.ok("GET", "/enrolment/codes")
    rows = listed["codes"] if isinstance(listed, dict) else listed
    assert any(scratch_name in str(r.get("name", "")) for r in rows), (
        "a minted code is absent from the outstanding list")
    assert not any(code == r.get("code") for r in rows), (
        "the listing served the code back — it must appear at mint and never "
        "again")

    api.ok("POST", "/enrolment/codes/revoke", json={"code": code})


def test_a_code_mint_refuses_a_kind_it_does_not_serve(api, scratch_name, console):
    """`kind` is fixed at mint and never re-asserted at redemption, so an
    unrecognised one accepted here would be a code that means nothing later."""
    r = api.post("/enrolment/codes",
                 json={"name": scratch_name, "kind": "not-a-kind"})
    assert r.status_code == 400, (
        f"host minted a code of an unknown kind ({r.status_code}) — a kind is "
        f"never checked again after this point")


def test_redeeming_a_code_pairs_a_real_device(api, scratch_name, console):
    """The pairing round trip, end to end, over the wire.

    This is what the phone does at the pairing screen, and the one test here
    that proves the unauthenticated route: mint a code with a token, spend it
    without one, and use what comes back. A redemption that returns a token
    the host then refuses is the failure that strands every new device.
    """
    minted = api.ok("POST", "/enrolment/codes",
                    json={"name": scratch_name, "kind": "device"})
    code = minted["code"]

    redeemed = api.call("POST", "/enrolment/redeem", token=None,
                        json={"code": code, "port": 9090})
    assert redeemed.status_code == 200, (
        f"redeem → {redeemed.status_code}: {redeemed.text[:300]}")
    device_id, token = _minted(redeemed.json())
    assert token, f"redemption handed back no credential: {redeemed.text[:300]}"

    paired = api.call("GET", "/host", token=token)
    assert paired.status_code == 200, (
        f"the token redemption just issued is refused by the host "
        f"({paired.status_code}) — a paired device that cannot call anything")

    if device_id:
        api.ok("POST", "/devices/{device_id}/revoke", fmt={"device_id": device_id})


def test_a_spent_code_cannot_be_spent_twice(api, scratch_name, console):
    """One-time means one time. A code that survives its redemption is a
    long-lived shared secret nobody thinks they have.

    Exactly one failed attempt, on purpose: the suite calls from another
    machine and is not loopback-exempt, so a loop of bad codes here would trip
    the enrolment limiter and lock the run out of its own pairing route.
    """
    minted = api.ok("POST", "/enrolment/codes",
                    json={"name": scratch_name, "kind": "device"})
    code = minted["code"]

    first = api.call("POST", "/enrolment/redeem", token=None,
                     json={"code": code, "port": 9090})
    assert first.status_code == 200, first.text[:300]
    device_id, _ = _minted(first.json())

    second = api.call("POST", "/enrolment/redeem", token=None,
                      json={"code": code, "port": 9090})
    assert second.status_code in (401, 429), (
        f"a spent code was accepted a second time ({second.status_code})")

    if device_id:
        api.ok("POST", "/devices/{device_id}/revoke", fmt={"device_id": device_id})
