"""The managed-leaf contract: authority is enforced beneath every UI."""

import pytest
from fastapi.testclient import TestClient

from jstack_host import attach_parent, devices, grants, hostenv, managed_access, mode
from jstack_host import store as stores


@pytest.fixture
def rig(tmp_path, monkeypatch, app):
    store = stores.SessionStore(db_path=tmp_path / "managed.sqlite")
    monkeypatch.setattr(stores, "get_store", lambda: store)
    monkeypatch.setattr(devices, "_store", lambda: store)
    monkeypatch.setattr(grants, "_store", lambda: store)
    monkeypatch.setattr(attach_parent, "parent_record", lambda: {})
    monkeypatch.setattr(mode, "is_hub", lambda: True)
    monkeypatch.setattr(mode, "current", lambda: {"mode": "open"})
    monkeypatch.setattr(hostenv, "host_id", lambda: "hub-main")
    row, token = devices.mint("phone")
    auth = {"Authorization": "Bearer " + token}
    console = TestClient(app, client=("127.0.0.1", 4000), headers=auth)
    remote = TestClient(app, client=("10.66.0.8", 4001), headers=auth)
    return store, row, token, console, remote


def adopt(store, key, name):
    row, token = devices.mint(name)
    store.upsert_host(key, name, "10.66.0.9")
    store.bind_host_device(key, row["id"])
    grants.remember(key, "jrg1.test.secret")
    return row, {"Authorization": "Bearer " + token}


def test_remote_never_receives_other_devices_or_secrets(rig):
    _, own, _, console, remote = rig
    devices.mint("another phone")
    assert len(console.get("/api/jremote/v1/devices").json()["devices"]) == 2
    rows = remote.get("/api/jremote/v1/devices").json()["devices"]
    assert [row["id"] for row in rows] == [own["id"]]
    assert not {"token_hash", "authority_grant", "authority_device"} & rows[0].keys()


def test_remote_cannot_mint_revoke_rename_or_manage_machines(rig):
    store, _, _, _, remote = rig
    other, _ = devices.mint("other")
    store.upsert_host("leaf-one", "Office", "10.66.0.9")
    cases = [("/devices", {"name": "new"}),
             (f"/devices/{other['id']}/revoke", {}),
             (f"/devices/{other['id']}/rename", {"name": "stolen"}),
             ("/enrolment/codes", {"name": "new", "kind": "device"}),
             ("/enrolment/codes/revoke", {"code": "ABCD-EFGH"}),
             ("/hosts/leaf-one/forget", {}),
             ("/hosts/leaf-one/rename", {"name": "stolen"}),
             ("/hosts/leaf-one/visibility", {"sees_home": False, "sees_leaves": False})]
    for path, body in cases:
        assert remote.post("/api/jremote/v1" + path, json=body).status_code == 403, path
    assert remote.get("/api/jremote/v1/enrolment/codes").status_code == 403
    assert store.device(other["id"])["revoked_at"] is None
    assert store.host_row("leaf-one")["name"] == "Office"


def test_remote_can_disconnect_only_itself(rig):
    store, own, _, _, remote = rig
    other, _ = devices.mint("other")
    assert remote.post("/api/jremote/v1/device/disconnect").status_code == 200
    assert store.device(own["id"])["revoked_at"] is not None
    assert store.device(other["id"])["revoked_at"] is None
    assert remote.get("/api/jremote/v1/host").status_code == 401


def test_leaf_loopback_is_not_an_administration_console(rig, monkeypatch):
    _, _, _, console, _ = rig
    monkeypatch.setattr(attach_parent, "parent_record", lambda: {"token": "parent"})
    console.headers["Authorization"] = "Bearer " + devices.internal_token()
    assert console.get("/api/jremote/v1/host").json()["features"]["device_management"] is False
    assert console.get("/api/jremote/v1/devices").json() == {"devices": []}
    assert console.post("/api/jremote/v1/devices", json={"name": "x"}).status_code == 403
    assert console.post("/api/jremote/v1/enrolment/codes", json={"name": "x"}).status_code == 403


def test_a_fresh_local_hub_is_its_own_console(rig, monkeypatch):
    """Where a fresh install lands: no `wg0.conf`, no gateway address, no
    parent — mode `local`. Its own menu bar still manages devices (the pairing
    item was missing on every fresh hub while the gate read mesh ownership);
    a remote caller still is not the console."""
    _, _, _, console, remote = rig
    monkeypatch.setattr(mode, "is_hub", lambda: False)
    monkeypatch.setattr(mode, "is_managed", lambda: False)
    monkeypatch.setattr(mode, "current", lambda: {"mode": "local"})
    assert console.get("/api/jremote/v1/host").json()["features"]["device_management"] is True
    assert console.post("/api/jremote/v1/enrolment/codes", json={"name": "phone"}).status_code == 200
    assert remote.get("/api/jremote/v1/host").json()["features"]["device_management"] is False
    assert remote.post("/api/jremote/v1/enrolment/codes", json={"name": "x"}).status_code == 403


def test_local_app_introduction_creates_no_independent_leaf_device(rig, monkeypatch):
    from jstack_host import enrolment
    store, _, _, console, remote = rig
    monkeypatch.setattr(enrolment, "_store", lambda: store)
    monkeypatch.setattr(attach_parent, "parent_record", lambda: {"token": "parent"})
    code = enrolment.mint_code("local app", "", kind=enrolment.KIND_LOCAL)["code"]
    path = "/api/jremote/v1/enrolment/redeem"
    assert remote.post(path, json={"code": code}).status_code == 403
    before = len(store.list_devices())
    response = console.post(path, json={"code": code})
    assert response.status_code == 200
    assert response.json()["device"]["id"] == devices.INTERNAL_ID
    assert len(store.list_devices()) == before + 1
    assert response.json()["tunnel"] is None
    assert console.post(path, json={"code": code}).status_code == 401
    old_code = enrolment.mint_code("old independent", "")["code"]
    assert console.post(path, json={"code": old_code}).status_code == 401


def test_both_visibility_switches_are_independent_and_enforced(rig):
    store, _, _, console, remote = rig
    leaf, auth = adopt(store, "leaf-one", "Office")
    adopt(store, "leaf-two", "Studio")
    for home, siblings, expected in [(True, True, {"hub-main", "leaf-two"}),
                                     (False, True, {"leaf-two"}),
                                     (True, False, {"hub-main"}),
                                     (False, False, set())]:
        response = console.post("/api/jremote/v1/hosts/leaf-one/visibility",
                                json={"sees_home": home, "sees_leaves": siblings})
        assert response.status_code == 200
        response = remote.post("/api/jremote/v1/managed/hosts", headers=auth)
        assert response.status_code == 200
        assert {r["key"] for r in response.json()["hosts"]} == expected
        # A credential saved before hiding home must not bypass the setting.
        assert remote.get("/api/jremote/v1/agents", headers=auth).status_code == (200 if home else 403)
        assert managed_access.may_reach(leaf["id"], "leaf-two") is siblings
        if not siblings:
            assert remote.post("/api/jremote/v1/managed/grant", headers=auth,
                               json={"key": "leaf-two"}).status_code == 404
    assert not managed_access.stream_allowed(leaf["id"])


def test_hidden_siblings_do_not_leak_through_sync(rig):
    store, _, _, _, remote = rig
    _, auth = adopt(store, "leaf-one", "Office")
    adopt(store, "leaf-two", "Studio")
    store.set_host_visibility("leaf-one", sees_home=True, sees_leaves=False)
    data = remote.get("/api/jremote/v1/sync", headers=auth).json()
    assert "leaf-two" not in {row["key"] for row in data["hosts"]}


def test_projection_is_idempotent_and_dies_with_its_authority(rig, monkeypatch):
    store, _, _, _, _ = rig
    grant = grants.issue("hub-main")
    monkeypatch.setattr(attach_parent, "parent_record", lambda: {"token": "parent"})
    allowed = {"value": True}
    monkeypatch.setattr(managed_access, "_post_parent", lambda *_: {"allowed": allowed["value"]})
    first, token = managed_access.mint_projection(grant, "phone", "hub-device")
    second, again = managed_access.mint_projection(grant, "phone", "hub-device")
    assert (first["id"], token) == (second["id"], again)
    assert sum(row["authority_device"] == "hub-device" for row in store.list_devices()) == 1
    assert managed_access.device_allowed(first["id"])
    allowed["value"] = False
    assert not managed_access.device_allowed(first["id"])
    allowed["value"] = True
    grants.revoke_issued()
    assert not managed_access.device_allowed(first["id"])


def test_hub_revoke_removes_leaf_authorization(rig):
    store, phone, _, console, remote = rig
    _, leaf_auth = adopt(store, "leaf-one", "Office")
    path = "/api/jremote/v1/managed/authorize"
    assert remote.post(path, headers=leaf_auth, json={"device_id": phone["id"]}).json()["allowed"]
    assert console.post(f"/api/jremote/v1/devices/{phone['id']}/revoke").status_code == 200
    assert remote.post(path, headers=leaf_auth, json={"device_id": phone["id"]}).json() == {"allowed": False}


def test_old_independent_leaf_credential_cannot_bypass_the_hub(rig, monkeypatch):
    _, _, _, _, remote = rig
    monkeypatch.setattr(attach_parent, "parent_record", lambda: {"token": "parent"})
    assert remote.get("/api/jremote/v1/agents").status_code == 403


def test_existing_adoption_is_resolved_from_its_redemption_not_its_name(rig):
    store, _, _, _, _ = rig
    row, _ = devices.mint("renamed after adoption")
    store.upsert_host("leaf-one", "Office", "10.66.0.9")
    store.add_enrolment_code("proof", "Office", 9_999_999_999, "", "host")
    store.consume_enrolment_code("proof", "from 10.66.0.9", 1)
    store.note_enrolment_device("proof", row["id"] + " from 10.66.0.9")
    assert store.host_for_device(row["id"])["key"] == "leaf-one"
    impostor, _ = devices.mint("Office")
    assert store.host_for_device(impostor["id"]) is None


def test_ambiguous_or_replaced_legacy_control_tokens_fail_closed(rig):
    store, _, _, _, _ = rig
    old, _ = devices.mint("old control")
    for key in ("first", "second"):
        store.upsert_host(key, "Office", "10.66.0.9")
    store.add_enrolment_code("old-proof", "Office", 9_999_999_999, "", "host")
    store.consume_enrolment_code("old-proof", "from 10.66.0.9", 1)
    store.note_enrolment_device("old-proof", old["id"] + " from 10.66.0.9")
    assert store.host_for_device(old["id"]) is None
    assert not managed_access.may_reach(old["id"], "hub-main")
    for key in ("first", "second"):
        replacement, _ = devices.mint("new " + key)
        store.bind_host_device(key, replacement["id"])
    assert store.host_for_device(old["id"]) is None
    assert not managed_access.may_reach(old["id"], "hub-main")


def test_a_leaf_control_token_cannot_disconnect_its_machine(rig):
    store, _, _, _, remote = rig
    leaf, auth = adopt(store, "leaf-one", "Office")
    assert remote.post("/api/jremote/v1/device/disconnect", headers=auth).status_code == 403
    assert remote.post(f"/api/jremote/v1/devices/{leaf['id']}/revoke", headers=auth).status_code == 403
    assert store.device(leaf["id"])["revoked_at"] is None


def test_an_open_stream_rechecks_parent_authority(rig, monkeypatch):
    import asyncio
    store, _, _, _, _ = rig
    grant = grants.issue("hub-main")
    monkeypatch.setattr(attach_parent, "parent_record", lambda: {"token": "parent"})
    allowed = {"value": True}
    monkeypatch.setattr(managed_access, "_post_parent", lambda *_: {"allowed": allowed["value"]})
    row, _ = managed_access.mint_projection(grant, "phone", "hub-device")

    async def verify():
        watcher = asyncio.create_task(devices.wait_revoked(row["id"]))
        await asyncio.sleep(.02)
        assert not watcher.done()
        allowed["value"] = False
        await asyncio.wait_for(watcher, timeout=3)
        assert store.device(row["id"])["revoked_at"] is None
    asyncio.run(verify())
