"""The host's account of itself, against a real host.

These are the routes the app asks before it trusts anything else: who are you,
where can I reach you, what can you do, what version do you ship. The in-process
tests pin their shapes; what only a live host can prove is that the answers are
true *of the machine* — a mode read off real interfaces, a download that really
serves bytes, a control action that really runs.
"""

from __future__ import annotations

import pytest

from conftest import BASE_URL

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not BASE_URL, reason="live suite is opt-in")]


def test_host_identifies_itself(host_identity):
    """`/host` is the route every other one is trusted on the strength of."""
    assert host_identity["host_id"], "a host with no id cannot be told from another"
    assert host_identity["name"]
    assert host_identity["profile"]
    assert isinstance(host_identity["features"], dict)


def test_the_mode_is_one_the_app_can_render(host_identity):
    """The menu bar and the app both print `mode.mode` verbatim. A value
    outside this set renders as itself and means nothing to a reader — which
    is how this Mac spent an evening displaying "managed" at nobody."""
    assert host_identity["mode"]["mode"] in {"local", "open", "managed"}


def test_every_advertised_address_is_one_a_second_machine_could_use(host_identity):
    """The pairing screen's whole job. Loopback here is the classic failure:
    true for the host saying it, false for every device being told it."""
    kinds = {"lan", "local", "mesh"}
    for a in host_identity["addresses"]:
        assert set(a) == {"kind", "host", "url", "note"}, a
        assert a["kind"] in kinds, a
        assert not a["host"].startswith("127."), f"loopback advertised: {a}"
        assert a["host"] != "localhost", f"loopback advertised: {a}"
        assert a["url"].startswith("http://"), a


def test_a_lan_address_is_reachable_from_off_the_host(api, host_identity):
    """Every `lan` entry is captioned "works while both machines are on this
    network". The suite runs on a different machine than the host, so it is
    exactly the reader that caption is addressed to — and can check it.

    This is the test that would have caught a VM bridge being published as the
    LAN (jStack ccf6224): 192.168.64.1 is private, routable on the host, and
    unreachable from anywhere else."""
    import httpx
    lans = [a for a in host_identity["addresses"] if a["kind"] == "lan"]
    assert lans, "a host with no LAN address cannot be paired on this network"
    for a in lans:
        try:
            r = httpx.get(f"{a['url']}/api/health", timeout=5.0)
        except Exception as e:  # noqa: BLE001
            pytest.fail(f"advertised LAN address {a['host']} is unreachable "
                        f"from another machine: {type(e).__name__}")
        assert r.status_code == 200, f"{a['host']} answered {r.status_code}"


def test_health_needs_no_token_and_names_the_host(api):
    """The one route that must answer before a token exists — it is how the
    app decides whether a machine hosts anything at all (`LanProbe`)."""
    r = api.raw("GET", "/api/health", timeout=10.0)
    assert r.status_code == 200, f"{r.status_code}: {r.text[:200]}"
    body = r.json()
    assert body.get("service") == "jremote-host" or "dashboard" in body, (
        f"LanProbe.isHostDashboard would read this host as an impostor: {body}")


def test_a_request_with_no_token_is_refused(api):
    """Fail-closed is the contract. Checked live because an embedded host
    mounts its own middleware, and a mount that lost the dependency would pass
    every in-process test of the router."""
    r = api.call("GET", "/host", token=None)
    assert r.status_code == 401


def test_a_request_with_a_wrong_token_is_refused(api):
    r = api.call("GET", "/host", token="nope-not-a-real-token")
    assert r.status_code == 401


def test_the_known_hosts_roster_answers(api):
    hosts = api.ok("GET", "/hosts")
    assert isinstance(hosts, (list, dict))


def test_a_known_host_can_be_renamed_and_forgotten(api, host_identity, scratch_name):
    """Rename then forget, so the suite leaves the roster as it found it."""
    key = host_identity["host_id"]
    r = api.call("POST", "/hosts/{key}/rename", fmt={"key": key},
                 json={"name": scratch_name})
    assert r.status_code in (200, 404), r.text
    r = api.call("POST", "/hosts/{key}/forget", fmt={"key": key})
    assert r.status_code in (200, 404), r.text


def test_the_engine_roster_is_served_not_shipped(api):
    """The app renders whatever this returns; a hardcoded client list is how
    an engine added on the host stays invisible in the app."""
    engines = api.ok("GET", "/engines")
    assert engines, "a host with no engines can open no sessions"


def test_the_mac_app_release_is_resolvable(api):
    """`/app/mac/latest` is what a Mac checks for updates. A host whose feed
    is stale ships a ten-build-old app to anyone installing today."""
    latest = api.ok("GET", "/app/mac/latest")
    assert latest


def test_the_mac_app_download_serves_bytes(api):
    """The route behind the update. A 200 with an HTML error page is the
    failure this catches — so the body is checked, not just the status."""
    r = api.get("/app/mac/download")
    assert r.status_code in (200, 302, 307, 404), r.text[:200]
    if r.status_code == 200:
        assert len(r.content) > 1024, "download served a stub, not an app"


def test_the_context_payload_answers(api):
    api.ok("GET", "/context")


def test_a_context_file_outside_the_root_is_refused(api):
    """Path traversal on a route that takes a path as a query parameter.
    Live, because the check depends on the real root, not a tmp_path."""
    r = api.get("/context/file",
                **{"params": {"path": "../../../../etc/passwd"}})
    assert r.status_code in (400, 403, 404), (
        f"traversal returned {r.status_code} — the host served a file outside "
        f"its context root")


def _control_names(actions) -> list[str]:
    names = ([a.get("id") or a.get("action") or a.get("name") for a in actions]
             if isinstance(actions, list)
             else list(actions.get("actions", {})) or list(actions))
    return [n for n in names if n]


def test_the_control_menu_answers_even_where_there_is_no_control_tier(api):
    """An empty menu is a real answer, not a failure.

    Control actions are the *embedding* host's own daemons, so a host running
    the default profile has none and must say so with an empty list rather
    than a 404 or a 500. An earlier draft of this test asserted the list was
    non-empty and failed against a correct host — the assertion encoded this
    Mac's configuration as if it were the contract.
    """
    actions = api.ok("GET", "/control/actions")
    assert isinstance(actions, (list, dict)), actions


def test_an_unknown_control_action_is_refused_not_dispatched(api):
    """The dispatch route's always-reachable half, and the one that matters
    most: `/control/{action}` takes an arbitrary string and hands it to a tier
    that runs daemons. A 200 here would mean an unrecognised name got past the
    lookup.

    Which refusal is correct depends on the host, so the menu is read first
    rather than guessed: a host with no convenience tier owes 503 (the tier is
    missing, and the action was never the problem), a host that has one owes
    4xx for a name it does not carry. Asserting one range for both is how this
    test first failed against a host answering correctly.
    """
    has_tier = bool(_control_names(api.ok("GET", "/control/actions")))
    r = api.post("/control/{action}", fmt={"action": "not-a-real-action"},
                 json={})
    if has_tier:
        assert 400 <= r.status_code < 500, (
            f"a host with control actions answered {r.status_code} for an "
            f"unknown one: {r.text[:300]}")
    else:
        assert r.status_code == 503, (
            f"a host with no control tier answered {r.status_code}, not 503: "
            f"{r.text[:300]}")


def test_a_listed_control_action_is_dispatchable(api):
    """The pairing: an action on the menu that no route will run is a button
    that fails only when someone presses it. Skips where the host offers no
    read-only action — the refusal test above keeps the route covered."""
    names = _control_names(api.ok("GET", "/control/actions"))
    safe = next((n for n in names if any(
        w in str(n).lower() for w in ("status", "list", "health", "info"))), None)
    if safe is None:
        pytest.skip(f"host offers no read-only control action: {names}")
    r = api.post("/control/{action}", fmt={"action": safe}, json={})
    assert r.status_code < 500, f"control/{safe} → {r.status_code}: {r.text[:300]}"
