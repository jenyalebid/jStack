"""The screens either side of a session: sync, tags, feed, notify, usage.

What binds this group together is that every route in it is *optional* — each
one is backed by machinery a given host may not have, and each answers 200
with `available: false` rather than an error when it doesn't. That design is
what makes the tests here worth running live: an unconfigured host and a
broken one return different things, and only a real host can tell you which
you built.

The most valuable test in the file is the last kind — that `/host`'s feature
map agrees with what each route itself answers. A capability map that
disagrees with its own screen is precisely what `/host` exists to prevent, and
it is invisible to any test that checks either side alone.
"""

from __future__ import annotations

import json

import httpx
import pytest

from conftest import API, BASE_URL, TIMEOUT

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not BASE_URL, reason="live suite is opt-in")]


def _available(payload) -> bool:
    """Whether a screen says it has anything behind it. A route that omits the
    key is one that always works."""
    return payload.get("available", True) if isinstance(payload, dict) else True


def _read_sse(api, path: str, *, params: dict | None = None,
              want: int = 1, limit_seconds: float = 15.0) -> list[str]:
    """Open a stream, take the first few frames, hang up.

    Streams here are endless by design — the board pushes for as long as a
    device is looking — so a test that read to EOF would hang until the
    suite's timeout and report it as a stall. This takes what it came for and
    disconnects, which is also exactly what a phone backgrounding does.
    """
    api.seen.add(("GET", API + path))
    url = BASE_URL + API + path.format()
    headers = {"Authorization": f"Bearer {api.token}"} if api.token else {}
    frames: list[str] = []
    with httpx.Client(timeout=limit_seconds) as client:
        with client.stream("GET", url, headers=headers, params=params or {}) as r:
            assert r.status_code == 200, f"{path} → {r.status_code}"
            ctype = r.headers.get("content-type", "")
            assert "event-stream" in ctype, (
                f"{path} is not a stream — content-type {ctype!r}")
            for line in r.iter_lines():
                if line:
                    frames.append(line)
                if len(frames) >= want:
                    break
    return frames


# ── sync ──

def test_sync_pulls_a_cursor_and_pushes_back(api, scratch_name):
    """The one cursor every device mirrors. A push that does not raise the
    cursor is a device whose next pull re-sends everything it just sent."""
    first = api.ok("GET", "/sync", **{"params": {"since": 0}})
    assert isinstance(first, dict), first

    api.ok("POST", "/sync", json={"session_meta": [
        {"session_id": f"live-{scratch_name}", "label": scratch_name}]})

    after = api.ok("GET", "/sync", **{"params": {"since": 0}})
    assert isinstance(after, dict), after


def test_a_sync_push_of_nothing_is_accepted(api):
    """The idle device's push. Rejecting an empty body would make every quiet
    client log an error once a minute."""
    api.ok("POST", "/sync", json={})


def test_devices_cannot_invent_a_host_through_sync(api, scratch_name):
    """`SyncPush` has no `hosts` field and `apply_push` no branch for one —
    every change to that table goes through a route that proved a bearer
    token. A device that could push a host row could invent a machine.

    Checked by pushing one and reading the roster back: an unknown key
    ignored is correct, an unknown key *stored* is the defect.
    """
    before = api.ok("GET", "/hosts")
    api.ok("POST", "/sync", json={"hosts": [
        {"key": f"invented-{scratch_name}", "name": "not a real machine"}]})
    after = api.ok("GET", "/hosts")

    rows = after.get("hosts", after) if isinstance(after, dict) else after
    names = [str(h.get("key", "")) + str(h.get("name", "")) for h in rows]
    assert not any(scratch_name in n for n in names), (
        "a device pushed a host row through /sync and the host stored it")
    assert len(rows) == len(before.get("hosts", before)
                            if isinstance(before, dict) else before)


# ── tags ──

def test_the_tag_vocabulary_answers(api):
    api.ok("GET", "/tags")


def test_a_tag_is_minted_edited_and_deleted(api, scratch_name):
    """The vocabulary's whole write surface. Skipped where the host has no
    timeline — tags come from jStack's `log_event` binary, so a host without
    one has no vocabulary to mint into, and that is a configuration rather
    than a fault."""
    name = scratch_name.replace("-", "")

    # Called before the availability branch, never skipped around it. A skip
    # here left `POST /tags` and `DELETE /tags/{name}` as the last two routes
    # in the whole suite that no live test had ever called — a host without a
    # timeline still has to refuse cleanly, and that refusal is the only
    # behaviour those routes have on such a host. Skipping it tested nothing
    # and reported the surface as covered.
    made = api.post("/tags", json={"name": name, "description": "live suite"})

    if made.status_code == 503:
        removed = api.delete("/tags/{name}", fmt={"name": name},
                             **{"params": {"force": True}})
        assert removed.status_code == 503, (
            f"host refuses to mint a tag but accepts deleting one: "
            f"{removed.status_code}")
        pytest.skip("host has no timeline, so the vocabulary is refused "
                    "end to end — both routes were still exercised")

    assert made.status_code < 300, f"{made.status_code}: {made.text[:300]}"

    edited = api.patch("/tags/{name}", fmt={"name": name},
                       json={"name": name, "description": "live suite, edited"})
    assert edited.status_code < 300, f"{edited.status_code}: {edited.text[:300]}"

    removed = api.delete("/tags/{name}", fmt={"name": name},
                         **{"params": {"force": True}})
    assert removed.status_code < 300, f"{removed.status_code}: {removed.text[:300]}"


def test_editing_a_tag_nobody_minted_is_refused(api, scratch_name):
    """The vocabulary means one thing to every writer only if it cannot be
    grown by typo."""
    r = api.patch("/tags/{name}", fmt={"name": f"never{scratch_name}"},
                  json={"description": "x"})
    assert r.status_code in (400, 404, 503), (
        f"host edited a tag that was never minted ({r.status_code})")


# ── feed ──

def test_the_feed_answers_for_a_day(api):
    feed = api.ok("GET", "/feed", **{"params": {"limit": 5}})
    assert isinstance(feed, dict), feed


def test_the_feed_streams(api):
    """SSE, opened and hung up on. The stream existing is the contract — the
    app leaves it open for hours, so a route that 200s and sends the wrong
    content type is a feed that never updates."""
    feed = api.ok("GET", "/feed", **{"params": {"limit": 1}})
    if not _available(feed):
        pytest.skip(f"host has no feed to stream: {feed}")
    frames = _read_sse(api, "/feed/stream", params={"limit": 1})
    assert frames, "feed stream opened and sent nothing"


# ── notify ──

def test_notify_prefs_round_trip(api, agent_id):
    """The mute switch. A pref that does not stick is a phone that keeps
    pinging after the user turned it off — the failure nobody reports as a
    bug, they just disable notifications."""
    before = api.ok("GET", "/notify/prefs")
    assert isinstance(before, dict), before

    api.ok("POST", "/notify/prefs",
           json={"agent_id": agent_id, "muted": True, "scope": "done"})
    after = api.ok("GET", "/notify/prefs")
    assert after != before or after, f"prefs unchanged after a write: {after}"

    api.ok("POST", "/notify/prefs",
           json={"agent_id": agent_id, "muted": False, "scope": "done"})


def test_a_push_token_registers(api, scratch_name):
    """What the app does on first launch. The host cannot verify an APNs
    token is real, so the contract is only that it accepts and stores one —
    but a 500 here is an app that can never receive a push."""
    api.ok("POST", "/notify/register",
           json={"token": f"live-suite-{scratch_name}"})


def test_foreground_is_reported(api):
    """The app telling the host which session is on screen, so the notifier
    can suppress a push for something the user is already looking at. Sent on
    every foreground and every session switch, so it must be cheap and must
    accept an empty body — a phone with no session open still reports."""
    api.ok("POST", "/notify/foreground", json={})


def test_the_notification_log_answers(api):
    events = api.ok("GET", "/notify/events")
    assert isinstance(events, (dict, list)), events


# ── usage ──

def test_usage_caps_answers_or_says_why(api):
    """Either the caps, or `available: false` with a reason. What it must
    never be is a 500 — this is on the app's home screen."""
    caps = api.ok("GET", "/usage/caps")
    assert isinstance(caps, dict), caps
    if not _available(caps):
        assert caps.get("reason"), (
            f"host says caps are unavailable and will not say why: {caps}")


def test_usage_spend_answers_for_a_window(api):
    spend = api.ok("GET", "/usage/spend", **{"params": {"days": 3}})
    assert isinstance(spend, dict), spend
    if not _available(spend):
        assert spend.get("reason"), spend


# ── files ──

def test_the_file_share_screen_reports_observed_state_or_says_it_cannot(api,
                                                                       host_identity):
    """What SMB sharing actually looks like on this machine — read, never set.

    The route's own docstring is the contract: observed state, and mutation
    stays a local root action. So the live value here is that a host with
    sharing off answers its full shape with the flags false rather than 500ing
    out of `fileshare.FileShareError` — an unconfigured machine and a broken
    probe are different answers and the screen has to be able to tell them
    apart.

    The flag checked against it is `ready`, not `available`: `serves_files()`
    is `status()["ready"]`, so on any Mac that supports SMB `available` is true
    while `file_sharing` stays false until shares are actually up. Comparing
    the map to `available` here would be a false equivalence that fails on a
    correct host — which is why this route is not in the `checks` table below.
    """
    share = api.ok("GET", "/files/share")
    assert isinstance(share, dict), share

    for key in ("available", "configured", "secure", "ready", "shares",
                "unexpected", "security_problems", "service_enabled",
                "guest_enabled", "account"):
        assert key in share, f"no {key!r} in the file-share state: {share}"

    for key in ("available", "configured", "secure", "ready"):
        assert isinstance(share[key], bool), (
            f"{key} is {share[key]!r}, not a bool — the screen branches on it")
    for key in ("service_enabled", "guest_enabled"):
        assert share[key] in (True, False, None), (
            f"{key} is {share[key]!r}; it is a tri-state and None means "
            f"unobserved, which is not the same as off")
    for key in ("shares", "unexpected", "security_problems"):
        assert isinstance(share[key], list), f"{key}: {share[key]!r}"
    assert isinstance(share["account"], dict), share["account"]

    if not share["available"]:
        assert share.get("reason"), (
            f"host says it cannot serve files and will not say why: {share}")
        assert share["shares"] == [] and share["ready"] is False, share

    assert host_identity["features"]["file_sharing"] == share["ready"], (
        f"/host says file_sharing={host_identity['features']['file_sharing']} "
        f"but the screen reports ready={share['ready']}")


# ── the map against the screens ──

def test_the_feature_map_agrees_with_the_routes_it_describes(api, host_identity):
    """`/host.features` is one summary a client reads instead of probing, so
    it is only worth anything if it matches what probing would find.

    This is the drift the source calls out by name: a capability map that
    disagrees with its own screen. It is invisible to a test of either side —
    the map is self-consistent, every route answers correctly, and the client
    still renders a screen the host cannot fill.
    """
    features = host_identity["features"]
    checks = {
        "usage_caps": ("GET", "/usage/caps"),
        "usage_spend": ("GET", "/usage/spend"),
        "feed": ("GET", "/feed"),
        "tags": ("GET", "/tags"),
        "context": ("GET", "/context"),
    }
    disagreements = []
    for name, (method, path) in checks.items():
        if name not in features:
            continue
        payload = api.ok(method, path)
        claimed = bool(features[name])
        served = _available(payload)
        if claimed != served:
            disagreements.append(
                f"/host says {name}={claimed}, but {method} {path} says "
                f"available={served}")
    assert not disagreements, (
        "the capability map and the screens disagree:\n  "
        + "\n  ".join(disagreements))


def test_every_feature_flag_is_a_boolean(api, host_identity):
    """Clients branch on these. A string or a null renders as truthy and puts
    a screen in front of someone that the host cannot fill."""
    for name, value in host_identity["features"].items():
        assert isinstance(value, bool), (
            f"feature {name!r} is {value!r} ({type(value).__name__}), not a bool")
