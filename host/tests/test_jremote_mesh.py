"""Mesh membership follows the device roster.

A WireGuard peer is a device credential's place on the network; the device
row is the one place a person revokes anything. These pin that the two cannot
come apart: pairing records the peer on the row, revoking the row removes the
peer, forgetting a machine revokes its credential and so its peer, and the
reconcile names every peer the roster cannot account for.

Driven against a fake `wg_peer.py` with the real CLI shape — add, list,
remove — and distinct addresses per peer, because a host row is matched to a
peer by its mesh address and a table where every peer shares one would never
exercise that.
"""

import textwrap

import pytest
from fastapi.testclient import TestClient

from jstack_host import devices, doctor, enrolment, router, tunnel
from jstack_host.store import SessionStore


FAKE_PEER_SCRIPT = textwrap.dedent('''
    import sys
    from pathlib import Path

    SUBNET_PREFIX = "10.66.0"
    HERE = Path(__file__).parent
    CLIENTS = HERE / "clients"
    TABLE = HERE / "peers.txt"

    def table():
        if not TABLE.exists():
            return []
        return [line.split() for line in TABLE.read_text().splitlines() if line]

    def main():
        cmd = sys.argv[1]
        rows = table()
        if cmd == "list":
            for name, ip in rows:
                print(f"{name}  {ip}  added 2026-09-01  last handshake: never")
            return 0
        if cmd == "add":
            name = sys.argv[-1]
            if any(n == name for n, _ in rows):
                print(f"device {name!r} already paired", file=sys.stderr)
                return 1
            used = {int(ip.rsplit(".", 1)[1]) for _, ip in rows} | {1}
            ip = f"{SUBNET_PREFIX}.{min(set(range(2, 255)) - used)}"
            rows.append([name, ip])
            TABLE.write_text("".join(f"{n} {i}\\n" for n, i in rows))
            CLIENTS.mkdir(exist_ok=True)
            if sys.argv[2] == "--leaf":
                (CLIENTS / f"{name}-leaf").mkdir(exist_ok=True)
                (CLIENTS / f"{name}-leaf" / "leaf.env").write_text(f"WG_ADDR={ip}/32\\n")
            else:
                (CLIENTS / f"{name}.conf").write_text(
                    f"[Interface]\\nPrivateKey = KEY-{name}\\nAddress = {ip}/32\\n")
            print(f"paired {name} at {ip}")
            return 0
        if cmd == "remove":
            name = sys.argv[2]
            if sys.argv[-1] != "--yes":
                print("refusing without --yes", file=sys.stderr)
                return 1
            keep = [r for r in rows if r[0] != name]
            if len(keep) == len(rows):
                print(f"no device {name!r}", file=sys.stderr)
                return 1
            TABLE.write_text("".join(f"{n} {i}\\n" for n, i in keep))
            (CLIENTS / f"{name}.conf").unlink(missing_ok=True)
            print(f"removed {name}")
            return 0
        if cmd == "boom":
            return 3
        return 2

    sys.exit(main())
''')


@pytest.fixture
def mesh(tmp_path, monkeypatch):
    """A hub that owns a mesh, end to end, with its own device table."""
    script = tmp_path / "wg_peer.py"
    script.write_text(FAKE_PEER_SCRIPT)
    (tmp_path / "wg0.conf").write_text("[Interface]\nPrivateKey = X\n")
    monkeypatch.setattr(tunnel, "PEER_SCRIPT", script)
    monkeypatch.setattr(tunnel, "HUB_CONF", tmp_path / "wg0.conf")
    monkeypatch.setattr(tunnel, "CLIENTS_DIR", tmp_path / "clients")
    s = SessionStore(db_path=tmp_path / "mesh.sqlite")
    monkeypatch.setattr(devices, "_store", lambda: s)
    monkeypatch.setattr(enrolment, "_store", lambda: s)
    monkeypatch.setattr("jstack_host.store.get_store", lambda: s)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    alerts = []
    monkeypatch.setattr("jstack_host.hostenv.security_alert", alerts.append)
    s.alerts = alerts
    return s


@pytest.fixture
def on_console(monkeypatch):
    monkeypatch.setattr(router, "_hub_console", lambda request: True)
    monkeypatch.setattr("jstack_host.managed_access.console", lambda request: True)


def _client(token):
    from jstack_host.server import create_app
    c = TestClient(create_app())
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


# ── removal ──────────────────────────────────────────────────────────────────

def test_removing_a_peer_takes_it_off_the_table_and_a_second_removal_is_quiet(mesh):
    tunnel.issue("phone")
    assert "phone" in tunnel.peer_table()
    assert tunnel.remove_peer("phone") is True
    assert "phone" not in tunnel.peer_table()
    assert tunnel.remove_peer("phone") is False


def test_a_tool_that_refuses_to_remove_is_an_error_not_a_silent_keep(mesh, tmp_path):
    tunnel.issue("phone")
    (tmp_path / "wg_peer.py").write_text(
        "import sys\nSUBNET_PREFIX='10.66.0'\n"
        "print('phone 10.66.0.2 added x') if sys.argv[1]=='list' else sys.exit(3)\n")
    with pytest.raises(tunnel.TunnelError, match="removing peer phone failed"):
        tunnel.remove_peer("phone")


@pytest.mark.parametrize("name", ["", "Two Words", "../x", "UPPER"])
def test_a_bad_peer_name_never_reaches_the_tool(mesh, name):
    with pytest.raises(tunnel.TunnelError, match="bad peer name"):
        tunnel.remove_peer(name)


# ── the row carries the peer; revoking the row removes it ────────────────────

def test_pairing_over_the_route_binds_the_peer_to_the_calling_device(mesh, monkeypatch):
    monkeypatch.setattr(tunnel, "is_lan_caller", lambda ip: True)   # TestClient is not on a LAN
    row, token = devices.mint("Owner Phone")
    answer = _client(token).post("/api/jremote/v1/tunnel/pair",
                                 json={"device": "owner-phone"})
    assert answer.status_code == 200, answer.text
    assert devices.peer_of(row["id"]) == "owner-phone"


def test_revoking_a_device_removes_its_peer(mesh):
    row, _ = devices.mint("Owner Phone")
    tunnel.issue("owner-phone")
    devices.bind_peer(row["id"], "owner-phone")
    assert devices.revoke(row["id"]) is True
    assert "owner-phone" not in tunnel.peer_table()
    assert mesh.alerts == []


def test_a_device_with_no_peer_revokes_without_touching_the_mesh(mesh):
    row, _ = devices.mint("LAN only")
    tunnel.issue("someone-else")
    assert devices.revoke(row["id"]) is True
    assert tunnel.peer_table() == {"someone-else": "10.66.0.2"}


def test_a_failed_peer_removal_still_revokes_and_alarms(mesh, tmp_path):
    row, _ = devices.mint("Owner Phone")
    tunnel.issue("owner-phone")
    devices.bind_peer(row["id"], "owner-phone")
    (tmp_path / "wg_peer.py").write_text(
        "import sys\nSUBNET_PREFIX='10.66.0'\n"
        "print('owner-phone 10.66.0.2 added x') if sys.argv[1]=='list' else sys.exit(3)\n")
    assert devices.revoke(row["id"]) is True
    assert devices.is_revoked(row["id"])
    assert len(mesh.alerts) == 1 and "owner-phone" in mesh.alerts[0]


def test_a_peer_reissued_to_another_row_moves_with_it(mesh):
    old, _ = devices.mint("Old phone")
    new, _ = devices.mint("New phone")
    devices.bind_peer(old["id"], "phone")
    devices.bind_peer(new["id"], "phone")
    assert devices.peer_of(old["id"]) == ""
    assert devices.peer_of(new["id"]) == "phone"


def test_redeeming_a_code_binds_the_issued_peer_to_the_new_row(mesh):
    minted = enrolment.mint_code("Owner iPad", "", 600, enrolment.KIND_DEVICE)
    answer = enrolment.redeem(minted["code"], "203.0.113.9")
    assert answer["tunnel"]["device"] == "owner-ipad"
    assert devices.peer_of(answer["device"]["id"]) == "owner-ipad"


# ── forget is the whole withdrawal ───────────────────────────────────────────

def test_the_console_forgetting_a_machine_revokes_its_credential_and_peer(mesh, on_console):
    hub, hub_token = devices.mint("Main Mac")
    leaf, _ = devices.mint("Work Bench")
    tunnel.issue("work-bench")
    devices.bind_peer(leaf["id"], "work-bench")
    mesh.upsert_host("bench-key", "Work Bench", "10.66.0.2", 9090)
    mesh.bind_host_device("bench-key", leaf["id"])
    answer = _client(hub_token).post("/api/jremote/v1/hosts/bench-key/forget")
    assert answer.status_code == 200, answer.text
    assert answer.json()["credential_revoked"] == leaf["id"]
    assert answer.json()["peer_removed"] == "work-bench"
    assert devices.is_revoked(leaf["id"])
    assert "work-bench" not in tunnel.peer_table()
    assert mesh.host_row("bench-key")["deleted"]


# ── the reconcile names what the roster cannot account for ───────────────────

def test_reconcile_classes_every_peer(mesh):
    live, _ = devices.mint("Owner Phone")
    gone, _ = devices.mint("Old iPad")
    bench, _ = devices.mint("Work Bench")
    for name in ("owner-phone", "old-ipad", "nobody", "work-bench", "owner-ipad-11"):
        tunnel.issue(name)
    devices.bind_peer(live["id"], "owner-phone")
    devices.bind_peer(gone["id"], "old-ipad")
    mesh.revoke_device(gone["id"])                      # stamp only: a pre-column revoke
    mesh.upsert_host("bench-key", "Work Bench", tunnel.peer_table()["work-bench"], 9090)
    mesh.bind_host_device("bench-key", bench["id"])
    devices.mint("Owner iPad 11")                        # matched by its name only

    report = tunnel.reconcile()
    by = {p["peer"]: p for p in report["peers"]}
    assert by["owner-phone"]["state"] == tunnel.BOUND and by["owner-phone"]["matched_by"] == "recorded"
    assert by["old-ipad"]["state"] == tunnel.REVOKED
    assert by["nobody"]["state"] == tunnel.UNBOUND and by["nobody"]["device_id"] == ""
    assert by["work-bench"]["state"] == tunnel.BOUND and by["work-bench"]["matched_by"] == "host address"
    assert by["owner-ipad-11"]["state"] == tunnel.BOUND and by["owner-ipad-11"]["matched_by"] == "name"
    assert sorted(report["stale"]) == ["nobody", "old-ipad"]
    assert sorted(report["inferred"]) == ["owner-ipad-11", "work-bench"]


def test_binding_the_inferred_matches_makes_them_recorded(mesh):
    bench, _ = devices.mint("Work Bench")
    tunnel.issue("work-bench")
    mesh.upsert_host("bench-key", "Work Bench", tunnel.peer_table()["work-bench"], 9090)
    mesh.bind_host_device("bench-key", bench["id"])
    assert tunnel.bind_inferred() == ["work-bench"]
    assert tunnel.reconcile()["inferred"] == []
    assert devices.peer_of(bench["id"]) == "work-bench"


def test_purge_removes_only_the_stale_peers(mesh):
    live, _ = devices.mint("Owner Phone")
    tunnel.issue("owner-phone")
    tunnel.issue("ghost")
    devices.bind_peer(live["id"], "owner-phone")
    assert tunnel.purge() == {"removed": ["ghost"], "failed": {}}
    assert tunnel.peer_table() == {"owner-phone": "10.66.0.2"}


def test_a_revoked_name_never_matches_a_peer_by_inference(mesh):
    """A revoked row named like a live peer must not make that peer look
    owned: inference is only ever to a live credential."""
    gone, _ = devices.mint("Owner Phone")
    mesh.revoke_device(gone["id"])
    tunnel.issue("owner-phone")
    assert tunnel.reconcile()["stale"] == ["owner-phone"]


# ── the roster view and the doctor ───────────────────────────────────────────

def test_the_peer_roster_is_console_only(mesh):
    _, token = devices.mint("Remote")
    assert _client(token).get("/api/jremote/v1/tunnel/peers").status_code == 403


def test_the_console_reads_the_peer_roster(mesh, on_console):
    row, token = devices.mint("Main Mac")
    tunnel.issue("stray")
    answer = _client(token).get("/api/jremote/v1/tunnel/peers")
    assert answer.status_code == 200
    assert answer.json()["stale"] == ["stray"]


def test_doctor_is_quiet_where_no_mesh_is_owned(monkeypatch):
    monkeypatch.setattr(tunnel, "can_pair", lambda: False)
    assert doctor.check_mesh()["grade"] == doctor.OK


def test_doctor_fails_on_a_peer_with_no_live_device(mesh):
    tunnel.issue("stray")
    result = doctor.check_mesh()
    assert result["grade"] == doctor.FAIL
    assert "stray" in result["detail"]


def test_doctor_passes_when_every_peer_is_a_live_device(mesh):
    row, _ = devices.mint("Owner Phone")
    tunnel.issue("owner-phone")
    devices.bind_peer(row["id"], "owner-phone")
    assert doctor.check_mesh()["grade"] == doctor.OK
