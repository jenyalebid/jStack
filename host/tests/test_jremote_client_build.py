"""The device row knows what build its app is — the X-JRemote-Build header.

Born from three days of TestFlight roulette: a tunnel fix shipped as build 61
could only be judged by symptom, because nothing on the host recorded what
build the phone was actually running — a fix that never installed and a fix
that installed and failed look identical from here. The contract pinned:
any authenticated request carrying the header lands the build on the row,
recording is write-on-change not write-per-request, and a garbage or missing
header can neither dirty the row nor cost the request.
"""

import pytest
from fastapi.testclient import TestClient

from jstack_host import devices
from jstack_host.store import SessionStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = SessionStore(db_path=tmp_path / "devices.sqlite")
    monkeypatch.setattr(devices, "_store", lambda: s)
    return s


# ── the recording itself ─────────────────────────────────────────────────────

def test_a_noted_build_lands_on_the_device_row(store):
    row, _ = devices.mint("my-iphone")
    devices.note_build(row["id"], "1.0 (62)")
    assert store.device(row["id"])["client_build"] == "1.0 (62)"


def test_an_update_overwrites_the_old_build(store):
    row, _ = devices.mint("my-iphone")
    devices.note_build(row["id"], "1.0 (61)")
    devices.note_build(row["id"], "1.0 (62)")
    assert store.device(row["id"])["client_build"] == "1.0 (62)"


def test_recording_is_write_on_change_not_write_per_request(store, monkeypatch):
    row, _ = devices.mint("my-iphone")
    writes = []
    real = store.note_device_build
    monkeypatch.setattr(store, "note_device_build",
                        lambda *a: (writes.append(a), real(*a)))
    for _ in range(5):
        devices.note_build(row["id"], "1.0 (62)")
    assert len(writes) == 1


def test_a_restart_does_not_rewrite_an_unchanged_build(store, monkeypatch):
    row, _ = devices.mint("my-iphone")
    devices.note_build(row["id"], "1.0 (62)")
    devices.reset_for_tests()  # the cache a restart empties
    writes = []
    monkeypatch.setattr(store, "note_device_build",
                        lambda *a: writes.append(a))
    devices.note_build(row["id"], "1.0 (62)")
    assert not writes, "an unchanged build was rewritten after a cache loss"


def test_garbage_builds_never_dirty_the_row(store):
    row, _ = devices.mint("my-iphone")
    devices.note_build(row["id"], "   ")
    devices.note_build(row["id"], "")
    assert store.device(row["id"])["client_build"] is None
    devices.note_build(row["id"], "x" * 500)
    assert len(store.device(row["id"])["client_build"]) == 64


def test_an_unknown_device_is_a_no_op_not_an_error(store):
    devices.note_build("nobody", "1.0 (62)")  # must not raise


def test_a_failed_write_does_not_poison_the_cache(store, monkeypatch):
    """A throwing UPDATE must leave the cache clean so the next request retries;
    caching before the write strands a failed row as 'done' until the build
    string next changes."""
    row, _ = devices.mint("my-iphone")
    boom = {"raise": True}
    real = store.note_device_build

    def flaky(device_id, build):
        if boom["raise"]:
            raise RuntimeError("write failed")
        real(device_id, build)

    monkeypatch.setattr(store, "note_device_build", flaky)
    with pytest.raises(RuntimeError):
        devices.note_build(row["id"], "1.0 (62)")
    # The retry succeeds and lands the value — proof the cache didn't swallow it.
    boom["raise"] = False
    devices.note_build(row["id"], "1.0 (62)")
    assert store.device(row["id"])["client_build"] == "1.0 (62)"


# ── the header rides ordinary authenticated requests ─────────────────────────

def test_the_header_on_an_authenticated_request_is_recorded(store, app):
    row, token = devices.mint("my-iphone")
    client = TestClient(app)
    resp = client.get("/api/jremote/v1/devices",
                      headers={"Authorization": f"Bearer {token}",
                               "X-JRemote-Build": "1.0 (62)"})
    assert resp.status_code == 200
    assert store.device(row["id"])["client_build"] == "1.0 (62)"


def test_the_build_is_visible_in_the_device_list(store, app):
    _, token = devices.mint("my-iphone")
    client = TestClient(app)
    client.get("/api/jremote/v1/devices",
               headers={"Authorization": f"Bearer {token}",
                        "X-JRemote-Build": "1.0 (62)"})
    listed = client.get("/api/jremote/v1/devices",
                        headers={"Authorization": f"Bearer {token}"}).json()
    assert [d["client_build"] for d in listed["devices"]] == ["1.0 (62)"]


def test_a_request_without_the_header_changes_nothing(store, app):
    row, token = devices.mint("my-iphone")
    devices.note_build(row["id"], "1.0 (61)")
    client = TestClient(app)
    resp = client.get("/api/jremote/v1/devices",
                      headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert store.device(row["id"])["client_build"] == "1.0 (61)"


def test_a_broken_recorder_never_costs_the_request(store, app, monkeypatch):
    _, token = devices.mint("my-iphone")
    monkeypatch.setattr(devices, "note_build",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    client = TestClient(app)
    resp = client.get("/api/jremote/v1/devices",
                      headers={"Authorization": f"Bearer {token}",
                               "X-JRemote-Build": "1.0 (62)"})
    assert resp.status_code == 200, "version bookkeeping took down an authed request"
