"""Every forget/revoke names who asked (jStack#107).

A leaf went `deleted=1`, its grant revoked in the same second and its device
credential a day later, with nothing on the host naming the caller. Pinned here:
each removal path writes an `access_audit` row carrying its actor, a no-op
writes none, a caller that set no actor still leaves a row, and
`jstack-host history <machine>` shows the rows for a forgotten machine.
"""

import json

import pytest
from fastapi.testclient import TestClient

from jstack_host import audit, cli, devices, grants, managed_access, store


@pytest.fixture(autouse=True)
def one_store(monkeypatch):
    """Production reads devices and hosts from one store; conftest splits them."""
    monkeypatch.setattr(devices, "_store", lambda: store.get_store())


@pytest.fixture
def client(app):
    return TestClient(app)


def _adopted(key="leaf-mac-01"):
    s = store.get_store()
    s.upsert_host(key, "work", "10.66.0.9", 9090)
    grants.remember(key, "jrg1.deadbeef.secret", "http://hub:9090")
    return s


def _history(*targets):
    return store.get_store().access_history(list(targets) or None)


def test_forget_route_records_tile_and_grant_with_the_calling_device(client, monkeypatch):
    monkeypatch.setattr(managed_access, "console", lambda _: True)
    _adopted()
    row, token = devices.mint("boss-phone")
    resp = client.post("/api/jremote/v1/hosts/leaf-mac-01/forget", json={},
                       headers={"Authorization": f"Bearer {token}",
                                "User-Agent": "jRemote/1.0",
                                "X-JRemote-Build": "1.0 (104)"})
    assert resp.status_code == 200
    rows = _history("leaf-mac-01")
    assert {r["action"] for r in rows} == {"host.forget", "host_grant.revoke"}
    for r in rows:
        assert r["target_name"] == "work"
        assert r["actor"] == row["id"] and r["actor_name"] == "boss-phone"
        assert r["via"] == "POST /api/jremote/v1/hosts/leaf-mac-01/forget"
        assert r["user_agent"] == "jRemote/1.0"
        assert r["detail"] == {"build": "1.0 (104)"}


def test_device_revoke_route_records_the_revoker(client, monkeypatch):
    monkeypatch.setattr(managed_access, "console", lambda _: True)
    victim, _ = devices.mint("work")
    caller, token = devices.mint("menubar")
    resp = client.post(f"/api/jremote/v1/devices/{victim['id']}/revoke",
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    [r] = _history(victim["id"])
    assert (r["action"], r["target_name"], r["actor"]) == (
        "device.revoke", "work", caller["id"])


def test_a_revoke_that_changed_nothing_records_nothing():
    victim, _ = devices.mint("work")
    assert devices.revoke(victim["id"])
    assert not devices.revoke(victim["id"])
    assert not grants.forget("never-adopted")
    assert len(_history(victim["id"], "never-adopted")) == 1


def test_an_unset_actor_still_leaves_a_row_naming_the_code_path():
    victim, _ = devices.mint("work")
    devices.revoke(victim["id"])
    [r] = _history(victim["id"])
    assert r["via"] == "unattributed"
    assert r["origin"].startswith("pid ")
    assert any("devices.py" in f and "revoke" in f for f in r["detail"]["stack"])


def test_detach_revocations_are_recorded_per_credential():
    grants.issue("http://hub:9090")
    dev, _ = devices.mint("hub-projection")
    with audit.acting(audit.from_cli("detach")):
        grants._store().revoke_parent_authority()
    rows = _history()
    kinds = {(r["action"], r["via"]) for r in rows}
    assert ("parent_grant.revoke", "cli:detach") in kinds
    assert any(r["target"] == dev["id"] and r["action"] == "device.revoke"
               for r in rows)


def test_cli_records_its_subcommand_and_session(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-123")
    victim, _ = devices.mint("work")
    monkeypatch.setattr(cli, "build_parser", lambda: _parser(
        lambda args: devices.revoke(victim["id"]) and 0))
    assert cli.main(["zap"]) == 0
    [r] = _history(victim["id"])
    assert r["via"] == "cli:zap"
    assert r["detail"]["session"] == "sess-123"
    assert r["origin"].startswith("pid ")


def _parser(fn):
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("zap").set_defaults(fn=fn)
    return ap


def test_history_shows_a_forgotten_machine_with_its_credential(capsys, monkeypatch):
    s = _adopted()
    dev, _ = devices.mint("work")
    s.bind_host_device("leaf-mac-01", dev["id"])
    with audit.acting({"via": "POST /x", "actor": "d1", "actor_name": "boss-phone",
                       "origin": "10.66.0.2"}):
        s.forget_host("leaf-mac-01")
        grants.forget("leaf-mac-01")
    devices.revoke(dev["id"])
    monkeypatch.setattr(cli, "_adopt", lambda args: None)

    assert cli.main(["history", "work"]) == 0
    out = capsys.readouterr().out
    assert "host.forget" in out and "host_grant.revoke" in out
    assert "device.revoke" in out and "by boss-phone via POST /x from 10.66.0.2" in out

    assert cli.main(["history", "leaf-mac-01", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {r["action"] for r in rows} == {"host.forget", "host_grant.revoke",
                                           "device.revoke"}
