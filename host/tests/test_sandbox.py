"""Sandbox units: ledger, modes, reach, ranking, leases, seats, images, guard.

Every host here is a fake: its own state dir and a tart that only keeps a dict,
so nothing boots. The live legs are in ~/Systems/sandbox/testing/journeys.json.
"""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
import os
import shlex
import subprocess
import time

import pytest

from jstack_host.sandbox import client, guard, host, images, owner, reach, settings


REAL_NET_MISSING = host._net_missing


class FakeTart:
    vms: dict = {}

    def __init__(self, *args):
        self.home = str(args[-1])

    def _mine(self):
        return FakeTart.vms.setdefault(self.home, {})

    def list(self):
        return [{"Name": n, "State": s} for n, s in self._mine().items()]

    def names(self):
        return set(self._mine())

    def running(self, name):
        return self._mine().get(name) == "running"

    def clone(self, src, dst):
        assert src in self._mine(), f"no {src} to clone"
        self._mine()[dst] = "stopped"

    def configure(self, *a):
        pass

    booted: dict = {}

    def boot(self, name, wait, args=(), env=None):
        self._mine()[name] = "running"
        FakeTart.booted[name] = list(args)
        FakeTart.booted_env[name] = dict(env or {})
        return "192.168.64.2"

    def ip(self, name, wait):
        assert self.running(name), f"{name} is not running"
        return "192.168.2.7" if FakeTart.booted.get(name) else "192.168.64.2"

    def exec(self, *a, **kw):
        class R:
            returncode = 0
            stdout = ""
        return R()

    def stop(self, name):
        if name in self._mine():
            self._mine()[name] = "stopped"

    def delete(self, name):
        self._mine().pop(name, None)

    def run(self, *args, **kw):
        if args[0] == "rename":
            self._mine()[args[2]] = self._mine().pop(args[1])

    def pull(self, ref):
        self._mine()[ref] = "stopped"


class SerialPool:
    def __init__(self, max_workers=1):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def map(self, fn, items):
        return [fn(i) for i in items]


HOSTS: dict = {}
ME = {"pid": 1, "start": "now", "sid": "s-1", "engine": "claude"}


@contextlib.contextmanager
def on(name):
    """Run as host `name`: its own state, root and identity."""
    saved = {k: os.environ.get(k) for k in ("JSTACK_SANDBOX_STATE", "JSTACK_SANDBOX_HOST")}
    os.environ["JSTACK_SANDBOX_STATE"] = str(HOSTS[name]["state"])
    os.environ["JSTACK_SANDBOX_HOST"] = name
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def add_host(tmp_path, name, mode="free", headroom=1.0, peers=(), **conf):
    state = tmp_path / name / "state"
    HOSTS[name] = {"state": state, "headroom": headroom, "peers": list(peers)}
    with on(name):
        settings.set_value("root", str(tmp_path / name / "root"))
        settings.set_value("mode", mode)
        for k, v in conf.items():
            settings.set_value(k, v)


def seed_image(host_name, tenant, image="ios"):
    with on(host_name):
        conf = settings.load()
        FakeTart(host.tenant_root(conf, tenant) / "tart")._mine()[host.image_vm(image)] = "stopped"
        folder = host.tenant_root(conf, tenant) / "images"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{image}.json").write_text(json.dumps(
            {"recipe": None, "toolchain": host.toolchain()}))


@pytest.fixture(autouse=True)
def fleet(monkeypatch, tmp_path):
    FakeTart.vms = {}
    FakeTart.booted = {}
    FakeTart.booted_env = {}
    HOSTS.clear()
    monkeypatch.setattr(host, "Tart", FakeTart)
    monkeypatch.setattr(host, "_net_missing", lambda conf: "")
    monkeypatch.setattr(host, "_headroom",
                        lambda conf: HOSTS[host.host_name()]["headroom"])
    monkeypatch.setattr(client, "start_keeper", lambda lease_id: None)
    # Fake hosts switch identity through os.environ, so the survey runs serially.
    monkeypatch.setattr(client, "ThreadPoolExecutor", SerialPool)
    monkeypatch.setattr(owner, "current", lambda start_pid=None: ME)

    def call(target, verb, req, conf=None):
        with on(target["name"]):
            try:
                return host.call(verb, req)
            except host.Refused as exc:
                raise client.SandboxError(str(exc)) from None
    monkeypatch.setattr(client, "call", call)
    monkeypatch.setattr(reach, "candidates", lambda me: [
        {"name": me, "ssh": None}] + [{"name": p, "ssh": p} for p in HOSTS[me]["peers"]])
    return tmp_path


def get_as(me, image="ios", kind="seat", **kw):
    with on(me):
        return client.get(image, kind=kind, wait=kw.pop("wait", 0.01), say=lambda _: None,
                          **kw)


# ---------------------------------------------------------------- modes

@pytest.mark.parametrize("mode,own,other", [
    ("off", False, False), ("local", True, False),
    ("offload", True, True), ("free", True, True)])
def test_modes_admit_exactly_what_they_say(fleet, mode, own, other):
    add_host(fleet, "box", mode=mode)
    seed_image("box", "box")
    seed_image("box", "guest")
    with on("box"):
        mine = host.admit({"tenant": "box", "image": "ios", "kind": "own",
                           "owner": ME, "client": "box"})
        theirs = host.admit({"tenant": "guest", "image": "ios", "kind": "own",
                             "owner": ME, "client": "guest"})
    assert (mine["state"] == "admitted") is own
    assert (theirs["state"] == "admitted") is other


def test_mode_switch_keeps_running_leases_and_cools_warm(fleet):
    add_host(fleet, "box", mode="free", warm={"box:ios": 1})
    seed_image("box", "box")
    lease = get_as("box")
    with on("box"):
        host.tick()
        assert any(g["role"] == "warm" for g in host.ls({})["guests"])
        host.mode({"mode": "off"})
        state = host.ls({})
        conf = settings.load()
    assert [g["role"] for g in state["guests"]] == ["shared"]
    assert [l["id"] for l in state["leases"]] == [lease["id"]]
    assert conf == {**settings.DEFAULTS, "mode": "off", "root": conf["root"],
                    "warm": {"box:ios": 1}}


def test_settings_reject_unknown_keys_and_modes(fleet):
    add_host(fleet, "box")
    with on("box"), pytest.raises(KeyError):
        settings.set_value("schedule", "7-4")
    with on("box"), pytest.raises(ValueError):
        settings.set_value("mode", "sometimes")


# ---------------------------------------------------------------- reach

BLOCK = """Host other
  HostName 1.1.1.1
# >>> jremote managed hosts >>>
Host leaf-b
  HostName 10.0.0.2
  User b
Host hub
  HostName 10.0.0.1
  User h
# <<< jremote managed hosts <<<
"""


def test_reach_is_the_managed_block_minus_the_parent(tmp_path, monkeypatch):
    cfg = tmp_path / "ssh_config"
    cfg.write_text(BLOCK)
    monkeypatch.setenv("JSTACK_SANDBOX_SSH_CONFIG", str(cfg))
    monkeypatch.setenv("JSTACK_SANDBOX_PARENT", json.dumps(
        {"parent_name": "home-hub", "parent_address": "10.0.0.1"}))
    from importlib import reload
    fresh = reload(reach)
    names = [c["name"] for c in fresh.candidates("me")]
    assert names == ["me", "leaf-b"]
    monkeypatch.setenv("JSTACK_SANDBOX_PARENT", "")
    assert [c["name"] for c in fresh.candidates("me")] == ["me", "leaf-b", "hub"]


def test_a_leaf_without_a_grant_reaches_only_itself(tmp_path, monkeypatch):
    cfg = tmp_path / "ssh_config"
    cfg.write_text("Host other\n  HostName 1.1.1.1\n")
    monkeypatch.setenv("JSTACK_SANDBOX_SSH_CONFIG", str(cfg))
    monkeypatch.setenv("JSTACK_SANDBOX_PARENT", "{}")
    from importlib import reload
    assert [c["name"] for c in reload(reach).candidates("me")] == ["me"]


# ---------------------------------------------------------------- ranking

def test_free_beats_offload_and_headroom_breaks_ties(fleet):
    add_host(fleet, "home", mode="offload", headroom=5.0, peers=["a", "b", "c"])
    add_host(fleet, "a", mode="offload", headroom=9.0)
    add_host(fleet, "b", mode="free", headroom=1.0)
    add_host(fleet, "c", mode="free", headroom=3.0)
    for name in ("home", "a", "b", "c"):
        seed_image(name, "home")
    assert get_as("home")["host"] == "c"


def test_offload_self_wins_on_headroom_against_offload_peer(fleet):
    add_host(fleet, "home", mode="offload", headroom=6.0, peers=["work"])
    add_host(fleet, "work", mode="offload", headroom=2.0)
    seed_image("home", "home")
    seed_image("work", "home")
    assert get_as("home")["host"] == "home"


def test_a_host_without_the_image_is_not_ranked_and_says_why(fleet):
    add_host(fleet, "home", mode="off", peers=["a"])
    add_host(fleet, "a", mode="free")
    with pytest.raises(client.SandboxError) as err:
        get_as("home")
    assert "not baked" in str(err.value) and "mode off" in str(err.value)


def test_full_fleet_queues_with_reason_and_fifo(fleet):
    add_host(fleet, "a", mode="free", max_guests=1)
    seed_image("a", "a")
    first = get_as("a", kind="own")
    lines = []
    with on("a"), pytest.raises(client.SandboxError) as err:
        client.get("ios", kind="own", wait=0.01, say=lines.append)
    assert "guest slot(s) on a are in use" in lines[0]
    assert "gave up waiting" in str(err.value)
    with on("a"):
        early = host.admit({"ticket": "t1", "tenant": "a", "image": "ios",
                            "kind": "own", "owner": ME, "client": "a"})
        late = host.admit({"ticket": "t2", "tenant": "a", "image": "ios",
                           "kind": "own", "owner": ME, "client": "a"})
        client.release(first["id"])
        assert host.admit({"ticket": "t2", "tenant": "a", "image": "ios", "kind": "own",
                           "owner": ME, "client": "a"})["state"] == "queued"
        assert host.admit({"ticket": "t1", "tenant": "a", "image": "ios", "kind": "own",
                           "owner": ME, "client": "a"})["state"] == "admitted"
    assert early["state"] == late["state"] == "queued"


def test_cancelled_tickets_leave_no_queue_behind(fleet):
    add_host(fleet, "a", mode="free", max_guests=1)
    seed_image("a", "a")
    get_as("a", kind="own")
    with on("a"), pytest.raises(client.SandboxError):
        client.get("ios", kind="own", wait=0.01, say=lambda _: None)
    with on("a"):
        assert host.ls({})["tickets"] == []


# ---------------------------------------------------------------- warm

def test_warm_guest_is_handed_over_and_refilled(fleet):
    add_host(fleet, "a", mode="free", warm={"a:ios": 1})
    seed_image("a", "a")
    with on("a"):
        host.tick()
        warm = [g["name"] for g in host.ls({})["guests"] if g["role"] == "warm"]
    lease = get_as("a", kind="own")
    assert lease["guest"] == warm[0]
    with on("a"):
        host.tick()
        roles = sorted(g["role"] for g in host.ls({})["guests"])
    assert roles == ["own", "warm"]


def test_warm_yields_its_slot_and_is_not_kept_while_jobs_wait(fleet):
    add_host(fleet, "a", mode="free", max_guests=1, warm={"a:other": 1})
    seed_image("a", "a")
    seed_image("a", "a", image="other")
    with on("a"):
        host.tick()
    lease = get_as("a", kind="own")
    with on("a"):
        guests = host.ls({})["guests"]
    assert [g["image"] for g in guests] == ["ios"] and lease["image"] == "ios"


# ---------------------------------------------------------------- leases

def test_dead_owner_orphans_then_grace_reaps_and_proves_it(fleet, monkeypatch):
    add_host(fleet, "a", mode="free", grace_minutes=0)
    seed_image("a", "a")
    lease = get_as("a", kind="own")
    monkeypatch.setattr(owner, "alive", lambda o: False)
    with on("a"):
        client.keep(lease["id"])
        assert host.ls({})["leases"][0]["state"] == "orphan"
        out = host.tick()
        assert out["reaped"][0]["gone"] is True
        assert host.ls({}) ["leases"] == [] and host.ls({})["guests"] == []


def test_unrenewed_lease_expires_to_orphan_not_to_reap(fleet):
    add_host(fleet, "a", mode="free", expire_seconds=0)
    seed_image("a", "a")
    lease = get_as("a", kind="own")
    time.sleep(0.01)
    with on("a"):
        out = host.tick()
        assert out["orphaned"] == [lease["id"]] and out["reaped"] == []


def test_assign_takes_over_an_orphan_with_its_guest(fleet, monkeypatch):
    add_host(fleet, "a", mode="free")
    seed_image("a", "a")
    lease = get_as("a", kind="own")
    with on("a"):
        host.orphan({"lease": lease["id"]})
        heir = {"pid": 2, "start": "later", "sid": "s-2", "engine": "codex"}
        monkeypatch.setattr(owner, "find_session", lambda sid: heir)
        moved = client.assign(lease["id"], "s-2")
        assert moved["guest"] == lease["guest"] and moved["state"] == "active"
        assert host.ls({})["leases"][0]["owner"] == heir


def test_a_handoff_takes_every_lease_of_the_source_session(fleet, monkeypatch):
    add_host(fleet, "a", mode="free")
    seed_image("a", "a")
    source = {"pid": 1, "start": "early", "sid": "s-1", "engine": "claude"}
    mine = [get_as("a", who=source) for _ in range(2)]
    get_as("a", who={**source, "sid": "s-other"})
    with on("a"):
        heir = {"pid": 2, "start": "later", "sid": "s-2", "engine": "claude"}
        monkeypatch.setattr(owner, "current", lambda *a: heir)
        moved = client.assign_from("s-1")
        assert sorted(l["id"] for l in moved) == sorted(l["id"] for l in mine)
        owners = {l["id"]: l["owner"]["sid"] for l in host.ls({})["leases"]}
        assert sorted(owners.values()) == ["s-2", "s-2", "s-other"]


def test_another_tenant_cannot_touch_a_lease(fleet):
    add_host(fleet, "a", mode="free")
    seed_image("a", "a")
    lease = get_as("a", kind="own")
    with on("a"), pytest.raises(host.Refused):
        host.release({"lease": lease["id"], "tenant": "intruder"})
    with on("a"), pytest.raises(host.Refused):
        host.run_in(lease["id"], ["true"], tenant="intruder")


def test_a_run_over_the_wire_arrives_with_its_tenant_and_argv(monkeypatch):
    from jstack_host.sandbox import cli
    sent, ran = [], []
    monkeypatch.setattr(client.subprocess, "run",
                        lambda argv, **kw: sent.append(argv[-1]) or type("P", (), {"returncode": 0})())
    monkeypatch.setattr(host, "run_in", lambda lease, argv, **kw: ran.append((lease, argv, kw)) or 0)
    client.run({"ssh": "far"}, "L1", ["sh", "-c", "echo $X", "--", "-i"], interactive=True, tenant="t")
    words = shlex.split(sent[0])
    cli.main(words[words.index("sandbox") + 1:])
    assert ran == [("L1", ["sh", "-c", "echo $X", "--", "-i"],
                    {"tty": False, "interactive": True, "tenant": "t"})]


def test_ls_shows_only_this_sessions_leases(fleet, monkeypatch):
    add_host(fleet, "a", mode="free")
    seed_image("a", "a")
    mine = get_as("a", kind="own")
    other = {"pid": 9, "start": "x", "sid": "s-9", "engine": "claude"}
    with on("a"):
        theirs = client.get("ios", kind="own", say=lambda _: None, who=other)
        assert [r["id"] for r in client.mine()] == [mine["id"]]
        assert {r["id"] for r in client.mine(everyone=True)} == {mine["id"], theirs["id"]}


# ---------------------------------------------------------------- seats

def test_seats_share_a_guest_then_spill_to_a_new_one(fleet):
    add_host(fleet, "a", mode="free", seats_per_guest=2)
    seed_image("a", "a")
    one, two, three = (get_as("a") for _ in range(3))
    assert one["guest"] == two["guest"] != three["guest"]
    assert {one["seat"], two["seat"]} == {"seat1", "seat2"}


def test_releasing_a_seat_keeps_the_guest_for_the_other(fleet):
    add_host(fleet, "a", mode="free")
    seed_image("a", "a")
    one, two = get_as("a"), get_as("a")
    with on("a"):
        out = client.release(one["id"])
        assert out["released"]["guest_kept"] == two["guest"]
        assert [g["name"] for g in host.ls({})["guests"]] == [two["guest"]]


def test_tenant_cap_holds(fleet):
    add_host(fleet, "a", mode="free", tenant_caps={"a": 1})
    seed_image("a", "a")
    get_as("a", kind="own")
    with on("a"), pytest.raises(client.SandboxError) as err:
        client.get("ios", kind="own", wait=0.01, say=lambda _: None)
    assert "at its cap" in str(err.value)


def test_a_shared_network_boots_with_its_setting_and_holds_one_tenant(fleet):
    (fleet / "softnet").write_text("")
    add_host(fleet, "a", mode="free", max_guests=3,
             shared_net_args=["--net-x", "--allow=all"], softnet=str(fleet / "softnet"))
    for tenant in ("a", "b"):
        seed_image("a", tenant)
    hub = get_as("a", kind="own", net="shared")
    assert FakeTart.booted[hub["guest"]] == ["--net-x", "--allow=all"]
    leaf = get_as("a", kind="own", net="shared")
    assert FakeTart.booted[leaf["guest"]] == ["--net-x", "--allow=all"]
    assert FakeTart.booted_env[hub["guest"]]["PATH"].startswith(f"{fleet}:")
    plain = get_as("a", kind="own")
    assert FakeTart.booted[plain["guest"]] == []
    assert FakeTart.booted_env[plain["guest"]] == {}
    with on("a"):
        assert client.lease_verb("ip", hub["id"])["ip"] == "192.168.2.7"
        out = host.admit({"tenant": "b", "image": "ios", "kind": "own",
                          "net": "shared", "owner": ME, "client": "b"})
        assert out["state"] == "queued" and "held by another tenant" in out["reason"]
        with pytest.raises(host.Refused, match="--own"):
            host.admit({"tenant": "a", "image": "ios", "kind": "seat",
                        "net": "shared", "owner": ME, "client": "a"})


def test_a_host_without_a_working_softnet_refuses_a_shared_network(fleet, monkeypatch, tmp_path):
    add_host(fleet, "a", mode="free", softnet=str(tmp_path / "absent"))
    seed_image("a", "a")
    monkeypatch.setattr(host, "_net_missing", REAL_NET_MISSING)
    ask = {"tenant": "a", "image": "ios", "kind": "own", "net": "shared",
           "owner": ME, "client": "a"}
    with on("a"):
        out = host.admit(ask)
        assert out["state"] == "refused" and "no softnet" in out["reason"]
    with pytest.raises(client.SandboxError, match="(?s)no host can take.*no softnet"):
        get_as("a", kind="own", net="shared", wait=60)
    softnet = tmp_path / "softnet"
    softnet.write_text("")
    add_host(fleet, "a", mode="free", softnet=str(softnet))
    no_sudo = type("R", (), {"returncode": 1})()
    monkeypatch.setattr(host.subprocess, "run", lambda *a, **k: no_sudo)
    with on("a"):
        out = host.admit(ask)
        assert out["state"] == "refused" and "passwordless sudo" in out["reason"]
        assert host._net_env(host.settings.load(), "shared")["PATH"].startswith(f"{tmp_path}:")


def test_a_guest_that_never_comes_up_ends_the_get(fleet, monkeypatch):
    add_host(fleet, "a", mode="free")
    seed_image("a", "a")
    boots = []

    def dead(self, name, wait, args=(), env=None):
        boots.append(name)
        raise host.TartError(f"{name} did not come up within 300s")
    monkeypatch.setattr(FakeTart, "boot", dead)
    with pytest.raises(client.SandboxError, match="(?s)no host can take.*did not come up"):
        get_as("a", kind="own", wait=None)
    assert len(boots) == 1


def test_near_lands_beside_the_named_lease_or_waits(fleet):
    add_host(fleet, "a", mode="offload", headroom=0.1, peers=["b"])
    add_host(fleet, "b", mode="free", headroom=9.0, max_guests=1)
    for h in ("a", "b"):
        seed_image(h, "a")
    first = get_as("a", kind="own")
    assert first["host"] == "b"
    with on("a"):
        assert client.held(first["id"])["host"] == "b"
    with pytest.raises(client.SandboxError, match="gave up waiting"):
        get_as("a", kind="own", near=first["id"])
    with on("a"):
        client.release(first["id"])
    hub = get_as("a", kind="own")
    leaf_free = get_as("a", kind="own")
    assert hub["host"] == "b" and leaf_free["host"] == "a"


def test_a_parked_guest_frees_its_slot_and_keeps_its_lease(fleet):
    add_host(fleet, "a", mode="free", max_guests=1)
    seed_image("a", "a")
    held = get_as("a", kind="own", net="shared")
    with on("a"):
        assert client.lease_verb("park", held["id"])["parked"] == held["id"]
        t = host.tart_for(settings.load(), "a")
        assert t._mine()[held["guest"]] == "stopped"
        with pytest.raises(client.SandboxError, match="parked"):
            client.run({"name": "a", "ssh": None}, held["id"], ["true"], tenant="a")
    other = get_as("a", kind="own")
    with on("a"):
        with pytest.raises(client.SandboxError, match="slot"):
            client.lease_verb("resume", held["id"])
        client.release(other["id"])
        FakeTart.booted.pop(held["guest"])
        assert client.lease_verb("resume", held["id"])["state"] == "active"
        assert FakeTart.booted[held["guest"]] == settings.load()["shared_net_args"]
        assert client.held(held["id"])["state"] == "active"
        assert client.lease_verb("ip", held["id"])["ip"]


def test_a_parked_lease_nobody_renews_still_expires(fleet):
    add_host(fleet, "a", mode="free", expire_seconds=0, grace_minutes=0)
    seed_image("a", "a")
    held = get_as("a", kind="own")
    with on("a"):
        client.lease_verb("park", held["id"])
        time.sleep(0.01)
        host.tick()
        assert host.ls({})["leases"][0]["state"] == "orphan"
        host.tick()
        assert host.ls({})["leases"] == [] and host.ls({})["guests"] == []


def test_a_seat_never_parks(fleet):
    add_host(fleet, "a", mode="free")
    seed_image("a", "a")
    seat = get_as("a")
    with on("a"), pytest.raises(client.SandboxError, match="only a whole guest"):
        client.lease_verb("park", seat["id"])


def test_a_ledger_from_before_the_new_columns_opens(fleet, tmp_path):
    import sqlite3
    from jstack_host.sandbox import ledger
    root = tmp_path / "old"
    root.mkdir()
    db = sqlite3.connect(ledger.path(root))
    db.execute("CREATE TABLE guests (name TEXT PRIMARY KEY, tenant TEXT NOT NULL, "
               "image TEXT NOT NULL, role TEXT NOT NULL, created REAL NOT NULL, "
               "ip TEXT DEFAULT '')")
    db.execute("INSERT INTO guests VALUES ('g-a-1','a','ios','own',1,'')")
    db.commit()
    db.close()
    with ledger.open_db(root) as db:
        assert ledger.guests(db)[0]["net"] == "" and ledger.guests(db)[0]["parked"] == 0


# ---------------------------------------------------------------- images

def test_stale_recipe_and_moved_toolchain_are_refused(fleet, monkeypatch):
    add_host(fleet, "a", mode="free")
    seed_image("a", "a")
    with on("a"):
        stale = host.admit({"tenant": "a", "image": "ios", "kind": "own", "owner": ME,
                            "client": "a", "recipe": "abc"})
        assert "recipe changed" in stale["reason"]
        monkeypatch.setattr(host, "toolchain", lambda: "macOS 99")
        moved = host.admit({"tenant": "a", "image": "ios", "kind": "own", "owner": ME,
                            "client": "a"})
        assert "macOS 99" in moved["reason"]


def _bake_fixture(fleet, monkeypatch, verify_code):
    add_host(fleet, "a", mode="free")
    with on("a"):
        conf = settings.load()
        FakeTart(host.tenant_root(conf, "a") / "tart")._mine()["ghcr.io/x/base:1"] = "stopped"
        images.recipes_dir().mkdir(parents=True)
        (images.recipes_dir() / "ios.json").write_text(json.dumps(
            {"base": "ghcr.io/x/base:1", "steps": [{"run": "echo hi"}], "verify": "check"}))
    ran = []

    def fake_run(target, lease_id, argv, **kw):
        text = kw["stdin"].read()
        ran.append(text)
        return verify_code if "check" in text else 0
    monkeypatch.setattr(client, "run", fake_run)
    return ran


def test_bake_tags_only_after_verify(fleet, monkeypatch):
    ran = _bake_fixture(fleet, monkeypatch, verify_code=0)
    with on("a"):
        out = images.bake("ios", say=lambda _: None)
        assert out["tag"]["recipe"] == images.sha("ios")
        assert host.ls({})["leases"] == []
    assert any("echo hi" in r for r in ran)
    assert get_as("a", kind="own", recipe=None)["image"] == "ios"


def test_failed_verify_moves_nothing(fleet, monkeypatch):
    _bake_fixture(fleet, monkeypatch, verify_code=1)
    with on("a"):
        with pytest.raises(client.SandboxError):
            images.bake("ios", say=lambda _: None)
        conf = settings.load()
        assert host.image_tag(conf, "a", "ios") == {}
        assert host.ls({})["leases"] == [] and host.ls({})["guests"] == []


def test_secrets_ride_stdin_never_argv(fleet, monkeypatch, tmp_path):
    ran = _bake_fixture(fleet, monkeypatch, verify_code=0)
    with on("a"):
        recipe = json.loads((images.recipes_dir() / "ios.json").read_text())
        recipe["secrets"] = {"TOKEN": "tok"}
        (images.recipes_dir() / "ios.json").write_text(json.dumps(recipe))
        monkeypatch.setattr(images, "_secrets", lambda r: {"TOKEN": "s3cret"})
        images.bake("ios", say=lambda _: None)
    assert any("export TOKEN=s3cret" in r for r in ran)
    with on("a"):
        assert "s3cret" not in (images.recipes_dir() / "ios.json").read_text()


def test_tenants_are_apart_and_purge_proves_it(fleet):
    add_host(fleet, "a", mode="free", peers=[])
    add_host(fleet, "b", mode="free", peers=["a"])
    seed_image("a", "a")
    seed_image("a", "b")
    get_as("a", kind="own")
    theirs = get_as("b", kind="own")
    with on("a"):
        conf = settings.load()
        assert host.tenant_root(conf, "a") != host.tenant_root(conf, "b")
        out = host.purge({"tenant": "b"})
        assert out["root_gone"] and out["leases"] == [theirs["id"]]
        assert {g["tenant"] for g in host.ls({})["guests"]} == {"a"}
    with pytest.raises(host.Refused):
        host.tenant_root(conf, "../a")


def test_purge_leaves_the_kept_images_unless_asked_for_everything(fleet):
    add_host(fleet, "a", mode="free", peers=[])
    seed_image("a", "a", "base")
    seed_image("a", "a", "scratch")
    get_as("a", image="base", kind="own")
    with on("a"):
        settings.set_value("keep_images", ["base"])
        conf = settings.load()
        t = FakeTart(host.tenant_root(conf, "a") / "tart")
        out = host.purge({"tenant": "a"})
        assert out["kept"] == ["base"] and not out["root_gone"]
        assert t.names() == {host.image_vm("base")}
        assert [p.name for p in (host.tenant_root(conf, "a") / "images").iterdir()] == ["base.json"]
        assert host.ls({})["guests"] == [] and host.ls({})["leases"] == []
        out = host.purge({"tenant": "a", "everything": True})
        assert out["kept"] == [] and out["root_gone"]


def test_registry_bases_are_pulled_once_per_host_and_no_purge_takes_them(fleet):
    add_host(fleet, "a", mode="free", peers=[])
    with on("a"):
        conf = settings.load()
        shared = host.shared_cache(conf)
        for tenant in ("a", "b"):
            cache = host.tart_for(conf, tenant).home
            assert (Path(cache) / "cache").resolve() == shared.resolve()
        (shared / "OCIs" / "ghcr.io").mkdir(parents=True)
        FakeTart(host.tenant_root(conf, "a") / "tart")._mine()["ghcr.io/x/base:1"] = "stopped"
        settings.set_value("keep_images", ["nothing"])
        host.purge({"tenant": "a"})
        assert FakeTart(host.tenant_root(conf, "a") / "tart").names() == {"ghcr.io/x/base:1"}
        host.purge({"tenant": "a", "everything": True})
        assert not host.tenant_root(conf, "a").exists()
        assert (shared / "OCIs" / "ghcr.io").is_dir()


def test_a_tenant_cache_from_before_sharing_moves_over_whole(fleet):
    add_host(fleet, "a", mode="free", peers=[])
    with on("a"):
        conf = settings.load()
        old = host.tenant_root(conf, "a") / "tart" / "cache" / "OCIs"
        old.mkdir(parents=True)
        (old / "pulled").write_text("x")
        host.tart_for(conf, "a")
        assert (host.shared_cache(conf) / "OCIs" / "pulled").read_text() == "x"
        assert (host.tenant_root(conf, "a") / "tart" / "cache").is_symlink()


# ---------------------------------------------------------------- owner

def test_owner_is_the_engine_above_and_a_reused_pid_is_dead(monkeypatch):
    rows = {10: {"pid": 10, "ppid": 5, "start": "t10", "command": "/bin/zsh -c x"},
            5: {"pid": 5, "ppid": 1, "start": "t5",
                "command": "claude --session-id abcdef12-3456 --model m"}}
    who = reload_owner_current(monkeypatch, rows)
    assert who["pid"] == 5 and who["sid"] == "abcdef12-3456"
    assert owner.alive({"pid": 5, "start": "t5"})
    assert not owner.alive({"pid": 5, "start": "other"})


def reload_owner_current(monkeypatch, rows):
    import importlib
    fresh = importlib.reload(owner)
    monkeypatch.setattr(fresh, "_ps", lambda *pids: {p: rows[p] for p in pids if p in rows}
                        if pids else rows)
    return fresh.current(10)


# ---------------------------------------------------------------- guard

BOOTS = [
    "tart run --no-graphics probe", "tart run probe", "/opt/homebrew/bin/tart run probe",
    "tart run --headless probe", "qemu-system-aarch64 -nographic -m 4096",
    "VBoxHeadless --startvm probe", "cd /tmp; tart run --no-graphics probe",
    "x && tart run probe", "x || tart run --no-graphics probe", "echo x | tart run probe",
    "nohup tart run --no-graphics probe &", "nohup tart run probe >/dev/null 2>&1 & disown",
    "sudo -u someone tart run --no-graphics probe", "env FOO=1 tart run --no-graphics probe",
    "timeout 900 tart run --no-graphics probe", "setsid tart run probe",
    "caffeinate -s tart run --no-graphics probe", "nice -n 5 tart run probe",
    "machine run bench 'tart run --no-graphics probe'", 'machine run bench "tart run probe"',
    "ssh bench 'tart run --no-graphics probe'", "ssh -J bench admin@1.2.3.4 'tart run probe'",
    "ssh bench 'VBoxHeadless --startvm probe'", "tmux new-session -d 'tart run probe'",
    "tmux new-session -d -s boot 'tart run probe'", "bash -c 'tart run --no-graphics probe'",
    "sh -c 'tart run probe'", "zsh -c 'tart run --no-graphics probe'",
    "eval 'tart run --no-graphics probe'", "machine run bench 'cd /tmp; tart run x'",
    "ssh bench 'bash -c \"tart run probe\"'", "machine run bench 'nohup tart run probe &'",
    "machine run bench <<'SH'\ntart run --no-graphics probe\nSH",
]
ORDINARY = [
    "tart list", "tart ip probe", "tart clone base probe", "tart delete probe",
    "ssh admin@192.168.64.5 'echo hi'", "scp -r admin@192.168.64.5:/tmp/out .",
    "machine run bench 'sw_vers'", "machine get bench:/tmp/log .",
    'grep -rn "tart run" .', 'grep -rn -- "--no-graphics" scripts/',
    "ssh bench 'ps ax | grep -E \"tart run|softnet\"'",
    "machine run bench 'grep -n \"tart run\" ~/x.log'",
    "grep -n 'simctl boot' notes.md", "cat ~/.tart/probe.run.log",
    "tmux new-session -d -s t 'pytest tests/ | tee /tmp/x'", "bash -c 'echo hi'",
    "cat > /tmp/note.md <<'EOF'\ntart run --no-graphics is what we removed\nEOF",
    "python3 - <<'PY'\nBANNED = 'tart run --no-graphics'\nPY",
    "chromium --headless --dump-dom https://x", "xcodebuild -scheme App build",
    "jstack-host sandbox get ios && jstack-host sandbox exec L1 -- xcodebuild -scheme A test",
]


@pytest.mark.parametrize("command", BOOTS)
def test_guard_refuses_a_hand_boot_in_every_mode(command):
    for mode in settings.MODES:
        assert guard.verdict(command, mode), (command, mode)


@pytest.mark.parametrize("command", ORDINARY)
def test_guard_lets_ordinary_work_through(command):
    for mode in settings.MODES:
        assert guard.verdict(command, mode) == "", (command, mode)


@pytest.mark.parametrize("command,mode,blocked", [
    ("xcrun simctl boot 1234", "off", True),
    ("xcodebuild -scheme App test", "off", True),
    ("cd app && xcodebuild -scheme App test-without-building", "off", True),
    ("open -a Simulator", "off", True),
    ("ssh bench 'xcrun simctl boot x'", "off", True),
    ("xcrun simctl boot 1234", "free", False),
    ("xcodebuild -scheme App test", "local", False),
    ("cat > t.py <<'EOF'\nxcrun simctl boot x\nEOF", "off", False),
])
def test_guard(command, mode, blocked):
    assert bool(guard.verdict(command, mode)) is blocked


def test_a_command_in_a_lease_knows_its_lease():
    own = host._guest_argv({"id": "L1", "kind": "own"}, ["true"])
    seat = host._guest_argv({"id": "L2", "kind": "seat", "seat": "s1"}, ["true"])
    assert own[:2] == ["env", "JSTACK_SANDBOX_LEASE=L1"]
    assert seat[:6] == ["sudo", "-H", "-u", "s1", "env", "JSTACK_SANDBOX_LEASE=L2"]


def test_a_softnet_guest_is_found_by_arp(tmp_path):
    from jstack_host.sandbox import tart as tart_mod
    asked = []

    class Probe(tart_mod.Tart):
        def run(self, *args, check=True, timeout=None, **kw):
            asked.append(args)
            arp = "--resolver" in args
            return subprocess.CompletedProcess(args, 0 if arp else 1,
                                               "10.8.180.54\n" if arp else "", "" if arp else "no IP address found")

        def exec(self, name, argv, **kw):
            return subprocess.CompletedProcess(argv, 0, "", "")

    assert Probe("tart", tmp_path).ip("g", 5) == "10.8.180.54"
    assert asked[0][:2] == ("ip", "g") and "--resolver" not in asked[0]
    assert asked[1][asked[1].index("--resolver") + 1] == "arp"
