"""The three doors the Info window's Source row presses.

The whole point of the read route is what it does *not* do. The reported
symptom was "I open settings and it starts downloading": a window that checks
on open turns looking into acting. So the first test here fails if reading the
route reaches GitHub at all, by any path — `check()`, a client, a socket.
"""
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from jstack_host import attach_parent, build_source, devices, hostenv, mode
from jstack_host import fleet_updates as fleet, sourcestamp, update_routes
from jstack_host import release_manifest as releases
from jstack_host.update_supervisor import atomic_json

SOURCE = "/api/jremote/v1/updates/source"
REF = SOURCE + "/ref"
BUILD = SOURCE + "/build"


@pytest.fixture
def rig(tmp_path, monkeypatch, app):
    state = tmp_path / "state"
    (state / "updates").mkdir(parents=True)
    monkeypatch.setattr(hostenv, "state_dir", lambda: state)
    monkeypatch.setattr(hostenv, "host_id", lambda: "hub-main")
    monkeypatch.setattr(attach_parent, "parent_record", lambda: {})
    monkeypatch.setattr(mode, "is_managed", lambda: False)
    monkeypatch.setattr(mode, "is_hub", lambda: True)
    monkeypatch.setattr(sourcestamp, "capture", lambda: {
        "sha": "a" * 40, "dirty": False, "release": "2026-09-20-aaaaaaaa-1234",
        "version": "0.75.0"})
    from jstack_host import store as stores
    from jstack_host.store import SessionStore
    grid = SessionStore(db_path=tmp_path / "grid.sqlite")
    monkeypatch.setattr(stores, "get_store", lambda: grid)
    atomic_json(state / "updates" / "config.json",
                {"github_repo": "example/stack", "channel": "stable",
                 "feed_dir": str(tmp_path / "feed"), "public_key": "k"})
    atomic_json(state / "updates" / "channel.json",
                {"checked": 1000, "ref": "stable", "head": "b" * 40,
                 "release": "2026-09-20-aaaaaaaa-1234", "status": "behind"})
    console = TestClient(app, client=("127.0.0.1", 4000),
                         headers={"Authorization": "Bearer " + devices.internal_token()})
    _, token = devices.mint("Phone")
    phone = TestClient(app, client=("10.66.0.8", 4001),
                       headers={"Authorization": "Bearer " + token})
    return state / "updates", console, phone, grid


def _airgap(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a source route reached the network")
    monkeypatch.setattr(build_source, "check", refuse)
    monkeypatch.setattr(build_source, "head", refuse)
    monkeypatch.setattr(build_source.httpx, "Client", refuse)


def test_reading_the_source_reports_the_last_answer_and_asks_for_no_new_one(rig, monkeypatch):
    root, console, _, _ = rig
    _airgap(monkeypatch)
    body = console.get(SOURCE).json()
    assert body["ref"] == "stable" and body["repository"] == "example/stack"
    assert body["enabled"] is True and body["managed"] is False
    # Under both names: a window from either side of the rename reads it.
    assert body["running"] == {"build": "2026-09-20-aaaaaaaa-1234",
                               "release": "2026-09-20-aaaaaaaa-1234", "sha": "a" * 40,
                               "version": "0.75.0", "dirty": False}
    # Verbatim from channel.json, timestamp included: a window that cannot tell
    # a stale verdict from a live one renders the stale one as live.
    assert body["check"] == {"status": "behind", "head": "b" * 40, "checked": 1000,
                             "detail": ""}
    assert body["build"] == {"state": "idle"} and body["can_build"] is True
    assert body["blocked"] == ""
    assert not (root / "build.json").exists()


def test_a_hub_that_never_checked_says_so_rather_than_checking(rig, monkeypatch):
    root, console, _, _ = rig
    (root / "channel.json").unlink()
    _airgap(monkeypatch)
    assert console.get(SOURCE).json()["check"]["status"] == "unknown"


def test_the_ref_is_written_here_and_validated_where_the_terminal_validates_it(rig, monkeypatch):
    root, console, _, _ = rig
    _airgap(monkeypatch)
    body = console.post(REF, json={"ref": "dev"})
    assert body.status_code == 200 and body.json()["ref"] == "dev"
    assert json.loads((root / "config.json").read_text())["channel"] == "dev"
    # Switching pulls nothing and builds nothing — the next check reports it.
    assert not (root / "build.json").exists()
    assert build_source.channel_ref(fleet.config()) == "dev"


@pytest.mark.parametrize("bad", ["-dangerous", "../main", "a b", "x" * 65])
def test_a_ref_this_hub_could_not_follow_is_refused_before_it_is_stored(rig, monkeypatch, bad):
    root, console, _, _ = rig
    _airgap(monkeypatch)
    assert console.post(REF, json={"ref": bad}).status_code == 400
    assert json.loads((root / "config.json").read_text())["channel"] == "stable"


def test_an_empty_ref_is_a_refusal_and_never_a_silent_move_to_main(rig, monkeypatch):
    """`channel_ref` reads a missing name as `stable`, which is what a config
    predating the choice means. A press carrying nothing means nothing."""
    root, console, _, _ = rig
    _airgap(monkeypatch)
    console.post(REF, json={"ref": "dev"})
    assert console.post(REF, json={"ref": ""}).status_code == 422
    assert console.post(REF, json={}).status_code == 422
    assert json.loads((root / "config.json").read_text())["channel"] == "dev"


def test_a_rebuild_answers_before_it_finishes_and_is_read_back_as_in_flight(rig, monkeypatch):
    root, console, _, _ = rig
    started, release = threading.Event(), {}

    def slow_build(where, config, **kwargs):
        started.wait(5)
        release["done"] = True
        return {"release": "built-1"}

    monkeypatch.setattr(build_source, "build", slow_build)
    answer = console.post(BUILD)
    assert answer.status_code == 200
    # The marker is written by the request, not by the worker: a reply that
    # said "idle" over a build already accepted is the window lying.
    assert answer.json()["build"]["state"] == "building"
    assert console.get(SOURCE).json()["build"]["state"] == "building"
    assert not release and json.loads((root / "build.json").read_text())["ref"] == "stable"
    started.set()


def test_a_second_rebuild_is_refused_while_the_first_is_still_running(rig, monkeypatch):
    root, console, _, _ = rig
    monkeypatch.setattr(build_source, "build", lambda *a, **k: pytest.fail("built anyway"))
    atomic_json(root / "build.json", {"state": "building", "ref": "stable",
                                      "started": time.time()})
    refused = console.post(BUILD)
    assert refused.status_code == 409
    assert refused.json()["detail"] == "a build is already running on this hub"


def test_a_build_that_dies_before_it_records_anything_still_clears_the_marker(rig, monkeypatch):
    root, console, _, _ = rig
    done = threading.Event()

    def explode(*args, **kwargs):
        try:
            raise releases.ReleaseError("the checkout is gone")
        finally:
            done.set()

    monkeypatch.setattr(build_source, "build", explode)
    assert console.post(BUILD).status_code == 200
    assert done.wait(5)
    for _ in range(50):
        if json.loads((root / "build.json").read_text())["state"] == "failed":
            break
        time.sleep(0.05)
    phase = json.loads((root / "build.json").read_text())
    assert phase["state"] == "failed" and phase["detail"] == "the checkout is gone"


def test_a_managed_mac_is_refused_cleanly_rather_than_raising_inside_the_build(rig, monkeypatch):
    root, console, _, _ = rig
    monkeypatch.setattr(mode, "is_managed", lambda: True)
    atomic_json(root / "config.json", {**fleet.config(), "managed": True})
    refused = console.post(BUILD)
    assert refused.status_code == 409
    assert refused.json()["detail"] == "a managed machine takes its builds from its parent"
    assert console.post(REF, json={"ref": "dev"}).status_code == 409
    body = console.get(SOURCE).json()
    assert body["managed"] is True and body["can_build"] is False
    assert not (root / "build.json").exists()


def test_a_hub_with_leaves_shows_why_there_is_no_button_instead_of_offering_one(rig, monkeypatch):
    """The refusal comes from `build_source`, so the window and the terminal
    read one rule. See #144 for why it exists."""
    root, console, _, grid = rig
    grid.upsert_host("leaf-one", "Office Mac", "10.66.0.9")
    grid.bind_host_device("leaf-one", "device-1")
    body = console.get(SOURCE).json()
    assert body["can_build"] is False and "Office Mac" in body["blocked"]
    assert "#144" in body["blocked"]
    refused = console.post(BUILD)
    assert refused.status_code == 409 and "#144" in refused.json()["detail"]
    # The ref is still the hub's to choose; only the build is held.
    assert console.post(REF, json={"ref": "dev"}).status_code == 200
    assert not (root / "build.json").exists()


def test_a_host_with_no_updater_configured_offers_nothing_and_refuses_both(rig, monkeypatch):
    root, console, _, _ = rig
    (root / "config.json").unlink()
    _airgap(monkeypatch)
    body = console.get(SOURCE).json()
    assert body["enabled"] is False and body["can_build"] is False
    assert body["blocked"] == "updates are not enabled on this host"
    assert console.post(BUILD).status_code == 409
    assert console.post(REF, json={"ref": "dev"}).status_code == 409


def test_no_device_but_the_local_menu_bar_may_read_or_move_this_hubs_ref(rig, monkeypatch):
    """Hub-administrative, on the same check the rest of this file carries: a
    phone on the mesh cannot move the ref, and its credential does not become
    the menu bar's by arriving over loopback."""
    root, console, phone, _ = rig
    monkeypatch.setattr(build_source, "build", lambda *a, **k: pytest.fail("built anyway"))
    for call in (lambda c, h: c.get(SOURCE, headers=h),
                 lambda c, h: c.post(REF, headers=h, json={"ref": "dev"}),
                 lambda c, h: c.post(BUILD, headers=h)):
        assert call(phone, {}).status_code == 403
        assert call(console, dict(phone.headers)).status_code == 403
    assert json.loads((root / "config.json").read_text())["channel"] == "stable"
    assert not (root / "build.json").exists()
