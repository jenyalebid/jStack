"""Which of the two adopt routes a machine is steered into — #61.

`adopt` and `adopt --offline` are not alternatives. One hands over a line to
run; the other hands over a file to carry, because the machine it is for has no
route to this hub at all. Nothing stood between them, so the carried file was
offered for a Mac that was already answering this hub over the mesh: it got a
USB stick and a rewritten bundle where one command would have done.

The steering hangs on two facts that must BOTH hold, and the tests below are
mostly about the second one. A live peer entry is the hub's own bookkeeping — it
survives the far Mac being wiped, and a wiped Mac is precisely who the carried
file exists for. So a peer that is live but silent must still get the file.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from jstack_host import addresses, cli, devices, enrolment, mode, presence, tunnel
from jstack_host.store import SessionStore


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """A hub that can mint, holding one adopted machine called "Work Mac".

    Everything this fixture stubs is a boundary `_cmd_adopt` crosses before it
    reaches the steering: the installed token, the mode, the peer table's
    existence. The steering's own two inputs — the peer list and the health
    probe — are left to each test, because they are the thing under test.
    """
    s = SessionStore(db_path=tmp_path / "steering.sqlite")
    s.upsert_host("work-key", "Work Mac", "10.66.0.7", 9090)
    monkeypatch.setattr("jstack_host.store.get_store", lambda: s)
    monkeypatch.setattr(enrolment, "_store", lambda: s)
    monkeypatch.setattr(devices, "_store", lambda: s)

    monkeypatch.setattr(cli, "_adopt", lambda a: None)
    monkeypatch.setattr(devices, "provisioned", lambda: True)
    monkeypatch.setattr(mode, "current", lambda: {"mode": "hub", "note": ""})
    monkeypatch.setattr(tunnel, "can_pair", lambda: True)
    monkeypatch.setattr(addresses, "reachable", lambda port: [
        {"kind": "lan", "url": f"http://192.168.0.106:{port}"},
        {"kind": "mesh", "url": f"http://10.66.0.1:{port}"},
    ])

    minted = []
    real_mint = enrolment.mint_code

    def watched(*a, **kw):
        out = real_mint(*a, **kw)
        minted.append(out)
        return out

    monkeypatch.setattr(enrolment, "mint_code", watched)

    carried = []
    monkeypatch.setattr(cli, "_adopt_offline",
                        lambda *a, **kw: bool(carried.append(a)) or 0)
    return SimpleNamespace(store=s, minted=minted, carried=carried)


def _adopt(name="Work Mac", offline=False, **over):
    fields = {"name": name, "offline": offline, "ttl": None, "port": None,
              "json": False, "state_dir": None}
    return SimpleNamespace(**{**fields, **over})


def _mesh(monkeypatch, peers, answering=True):
    """The two inputs the steering reads: the peer table, and what answers."""
    monkeypatch.setattr(tunnel, "live_peers", lambda: set(peers))
    monkeypatch.setattr(presence, "answers",
                        lambda address, port, timeout=1.0: answering)


# ── the refusal ──────────────────────────────────────────────────────────────

def test_the_carried_file_is_refused_for_a_mac_already_on_the_mesh(
        hub, monkeypatch, capsys):
    """The reported defect. The machine held a live peer and answered over the
    tunnel, and the menu bar still handed over a file to walk across town."""
    _mesh(monkeypatch, {"work-mac"})

    assert cli._cmd_adopt(_adopt(offline=True)) == 1
    assert hub.carried == [], "the bundle was rewritten for a Mac on the mesh"


def test_the_refusal_names_the_route_to_take_instead(hub, monkeypatch, capsys):
    """A refusal that does not say what to do instead is a dead end. The
    operator is standing in a dialog with no reason to know `adopt` alone is a
    different flow."""
    _mesh(monkeypatch, {"work-mac"})

    cli._cmd_adopt(_adopt(offline=True))
    said = capsys.readouterr().err
    assert "jstack-host adopt Work Mac" in said
    assert "10.66.0.7:9090" in said, "the refusal did not say where it answered"


def test_a_refused_offline_adopt_mints_no_code(hub, monkeypatch):
    """Minting first and refusing second would leave a live host code burning
    down in the table for an adoption nobody performed."""
    _mesh(monkeypatch, {"work-mac"})

    cli._cmd_adopt(_adopt(offline=True))
    assert hub.minted == []


# ── and the cases that must NOT be refused ───────────────────────────────────

def test_a_live_peer_that_does_not_answer_still_gets_the_file(hub, monkeypatch):
    """The case the whole carried flow exists for, and the one a check on the
    peer entry alone would have broken: the Mac was reinstalled, so the hub
    still carries its peer while the machine itself is gone."""
    _mesh(monkeypatch, {"work-mac"}, answering=False)

    assert cli._cmd_adopt(_adopt(offline=True)) == 0
    assert hub.carried, "a wiped Mac was refused the only route it has left"


def test_a_machine_with_no_peer_at_all_gets_the_file(hub, monkeypatch):
    """A Mac that was never adopted — the ordinary first-contact case."""
    _mesh(monkeypatch, set())

    assert cli._cmd_adopt(_adopt(name="New Mac", offline=True)) == 0
    assert hub.carried


def test_a_hub_that_cannot_read_its_peer_table_does_not_refuse(hub, monkeypatch):
    """"Could not tell" is not "already on the mesh". Reading it as one would
    take the carried file away from the machine with no other way in."""
    def broken():
        raise tunnel.TunnelError("could not list peers")

    monkeypatch.setattr(tunnel, "live_peers", broken)
    monkeypatch.setattr(presence, "answers",
                        lambda address, port, timeout=1.0: True)

    assert cli._cmd_adopt(_adopt(offline=True)) == 0
    assert hub.carried


def test_the_online_route_is_untouched_for_a_mac_on_the_mesh(hub, monkeypatch,
                                                             capsys):
    """The steering is about which flow, not about whether to adopt. A machine
    already on the mesh re-adopting through `adopt` is ordinary."""
    _mesh(monkeypatch, {"work-mac"})

    assert cli._cmd_adopt(_adopt()) == 0
    assert "jstack-host attach" in capsys.readouterr().out
    assert hub.minted


# ── the name the operator typed is not the name the peer holds ───────────────

def test_a_display_name_is_matched_against_the_peer_it_slugged_into(
        hub, monkeypatch):
    """The hub adopted "Work Mac" and wrote a peer called `work-mac`. Someone
    typing either one means the same machine, and a check that compared the
    typed string would miss whichever half they did not type."""
    _mesh(monkeypatch, {"work-mac"})

    assert cli._cmd_adopt(_adopt(name="work-mac", offline=True)) == 1
    assert cli._cmd_adopt(_adopt(name="Work Mac", offline=True)) == 1
    assert hub.carried == []


def test_a_peer_name_that_is_only_a_prefix_is_not_read_as_live(monkeypatch):
    """`peer_is_live` searched for `^\\s*{name}\\b`, and `-` is a word boundary
    — so `work` answered yes off a table holding only `work-mac`. Every caller
    acts on this, and here it would refuse a Mac the file was right for."""
    monkeypatch.setattr(tunnel, "live_peers", lambda: {"work-mac", "studio"})

    assert tunnel.peer_is_live("work-mac")
    assert not tunnel.peer_is_live("work")


def test_an_empty_peer_table_is_not_a_peer_named_no(monkeypatch, tmp_path):
    """`wg_peer.py list` says "no devices paired" when there are none, and the
    parser reads rows out of the same stream."""
    import subprocess

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout="no devices paired\n", stderr=""))
    assert tunnel.live_peers() == set()

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0,
        stdout="work-mac  10.66.0.7  added 2026-09-15\n"
               "studio    10.66.0.3  added 2026-01-02\n",
        stderr=""))
    assert tunnel.live_peers() == {"work-mac", "studio"}


# ── a hub with nowhere to be redeemed against ────────────────────────────────

def test_a_hub_with_no_redeemable_address_mints_nothing(hub, monkeypatch,
                                                        capsys):
    """Loopback only and no mesh. The code would have nowhere to be sent, and
    the dialog above this printed a command with `<this-mac>` where the address
    belongs — a blank inside a line meant to be copied and run."""
    monkeypatch.setattr(addresses, "reachable", lambda port: [
        {"kind": "local", "url": f"http://127.0.0.1:{port}"},
    ])

    assert cli._cmd_adopt(_adopt(json=True)) == 1
    assert hub.minted == []
    out = capsys.readouterr()
    assert out.out == "", "a code was printed for a hub with no address"
    assert "no address another machine can redeem against" in out.err


def test_a_hub_with_no_address_can_still_write_a_carried_file(hub, monkeypatch):
    """The carried flow needs no redeemable address by construction — the file
    brings the tunnel with it, and the mesh address it dials afterwards is the
    one inside the bundle."""
    monkeypatch.setattr(addresses, "reachable", lambda port: [
        {"kind": "local", "url": f"http://127.0.0.1:{port}"},
    ])
    _mesh(monkeypatch, set())

    assert cli._cmd_adopt(_adopt(offline=True)) == 0
    assert hub.carried


# ── the roster the menu bar reads before it offers anything ──────────────────

def test_the_roster_probes_only_the_machines_whose_peer_is_live(hub,
                                                                monkeypatch):
    """A hub whose leaves are all long gone pays nothing for asking."""
    hub.store.upsert_host("gone-key", "Old Mac", "10.66.0.9", 9090)
    monkeypatch.setattr(tunnel, "live_peers", lambda: {"work-mac"})
    probed = []
    monkeypatch.setattr(presence, "answers",
                        lambda address, port, timeout=1.0:
                        bool(probed.append(address)) or True)

    rows = {r["name"]: r for r in presence.roster()}
    assert probed == ["10.66.0.7"]
    assert rows["Work Mac"]["online"] is True
    assert rows["Old Mac"]["online"] is False


def test_the_roster_says_nothing_rather_than_offline_when_it_cannot_tell(
        hub, monkeypatch):
    """Tri-state, and null is not false — the menu bar reads this to decide
    whether to grey out a button, and a guess would grey out the wrong one."""
    def broken():
        raise tunnel.TunnelError("could not list peers")

    monkeypatch.setattr(tunnel, "live_peers", broken)
    assert [r["online"] for r in presence.roster()] == [None]


def test_leaves_json_carries_the_peer_name_and_liveness(hub, monkeypatch,
                                                        capsys):
    """What the adopt dialog actually calls. The peer name travels with the row
    so the menu bar compares against the hub's own answer rather than inventing
    a second slug rule."""
    _mesh(monkeypatch, {"work-mac"})

    assert cli._cmd_leaves(SimpleNamespace(json=True, state_dir=None)) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(r["peer"], r["online"]) for r in rows] == [("work-mac", True)]
