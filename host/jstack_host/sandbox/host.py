"""The host side: what this machine admits, runs and reaps.

Every verb takes and returns plain dicts, so a caller on this machine calls it
in-process and a caller on another reaches it as `sandbox host <verb>` over
ssh with the same JSON. Placement across machines is the client's; this side
only ever answers for itself.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import platform
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from . import ledger, settings
from .tart import Tart, TartError


class Refused(RuntimeError):
    pass


def host_name() -> str:
    override = os.environ.get("JSTACK_SANDBOX_HOST")
    if override:
        return override
    from .. import hostenv
    return hostenv.host_name()


def tenant_root(conf: dict, tenant: str) -> Path:
    if not tenant or "/" in tenant or tenant.startswith("."):
        raise Refused(f"bad tenant name {tenant!r}")
    return settings.root(conf) / "tenants" / tenant


def shared_cache(conf: dict) -> Path:
    """Registry pulls, once per host: public images hold nothing of any tenant."""
    return settings.root(conf) / "shared" / "cache"


def _share_cache(conf: dict, home: Path) -> None:
    """Point a tenant's tart cache at the host's shared one.

    A tenant cache that predates sharing moves over whole when the shared one is
    still empty, and is left alone otherwise; nothing is ever deleted here.
    """
    cache, shared = home / "cache", shared_cache(conf)
    if cache.is_symlink():
        return
    shared.parent.mkdir(parents=True, exist_ok=True)
    if cache.is_dir():
        if shared.exists():
            return
        cache.rename(shared)
    shared.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    cache.symlink_to(shared)


def is_registry_ref(name: str) -> bool:
    return "/" in name


def tart_for(conf: dict, tenant: str) -> Tart:
    home = tenant_root(conf, tenant) / "tart"
    _share_cache(conf, home)
    return Tart(settings.tart_bin(conf), home)


def image_vm(image: str) -> str:
    """The tart name an image is kept under: baked images by name, pulled
    bases by their own reference."""
    return image if "/" in image else f"img-{image}"


def image_tag(conf: dict, tenant: str, image: str) -> dict:
    try:
        return json.loads((tenant_root(conf, tenant) / "images" /
                           f"{image}.json").read_text())
    except (OSError, ValueError):
        return {}


def toolchain() -> str:
    return f"macOS {platform.mac_ver()[0]}"


def _admits(mode: str, tenant: str) -> str:
    if mode == "off":
        return "this host takes no jobs (mode off)"
    if mode == "local" and tenant != host_name():
        return "this host takes only its own jobs (mode local)"
    return ""


def _headroom(conf: dict) -> float:
    """Guests' worth of spare CPU and memory, read live from the machine."""
    def sysctl(key):
        out = subprocess.run(["sysctl", "-n", key], capture_output=True,
                             text=True).stdout.strip()
        return out
    try:
        cores = int(sysctl("hw.ncpu"))
        load = float(sysctl("vm.loadavg").strip("{} ").split()[0])
        vm = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
        page = int(sysctl("hw.pagesize"))
        pages = {}
        for line in vm.splitlines()[1:]:
            key, _, val = line.partition(":")
            pages[key.strip()] = int(val.strip().rstrip(".") or 0)
        avail = (pages.get("Pages free", 0) + pages.get("Pages inactive", 0) +
                 pages.get("Pages purgeable", 0)) * page / 2**30
    except (ValueError, IndexError):
        return 0.0
    return round(min(max(cores - load, 0) / conf["guest_cpu"],
                     avail / conf["guest_mem_gb"]), 2)


# ---------------------------------------------------------------- status

def status(req: dict) -> dict:
    conf = settings.load()
    tenant = req.get("tenant", "")
    with ledger.open_db(settings.root(conf)) as db:
        guests = ledger.guests(db)
        leases = ledger.leases(db)
        waiting = db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    busy = _running(guests)
    images = {}
    if tenant:
        asked = {req["image"]} if req.get("image") else set()
        for name in sorted(_tenant_images(conf, tenant) | asked):
            images[name] = _image_state(conf, tenant, name, req.get("recipes", {}).get(name))
    return {
        "host": host_name(), "mode": conf["mode"],
        "refusal": _admits(conf["mode"], tenant) if tenant else "",
        "max_guests": conf["max_guests"], "guests": len(guests),
        "busy": len(busy), "free_slots": max(conf["max_guests"] - len(busy), 0),
        "free_seats": _free_seats(conf, guests, leases, tenant, req.get("image")),
        "warm": [g["image"] for g in guests if g["role"] == "warm"
                 and g["tenant"] == tenant],
        "headroom": _headroom(conf), "waiting": waiting,
        "direct": _direct_refusal(conf, tenant) if tenant else "",
        "direct_free": max(conf["direct_slots"] -
                           sum(1 for l in leases if l["kind"] == "direct"), 0),
        "leases": len(leases), "images": images,
    }


def _tenant_images(conf: dict, tenant: str) -> set[str]:
    names = set()
    folder = tenant_root(conf, tenant) / "images"
    if folder.exists():
        names |= {p.stem for p in folder.glob("*.json")}
    try:
        names |= {vm["Name"] for vm in tart_for(conf, tenant).list()
                  if "/" in vm["Name"]}
    except (TartError, OSError, ValueError):
        pass
    return names


def _image_state(conf: dict, tenant: str, image: str,
                 recipe_sha: str | None) -> str:
    """'' when placeable, else why not."""
    if "/" in image:
        try:
            present = image in tart_for(conf, tenant).names()
        except (TartError, OSError, ValueError):
            present = False
        return "" if present else f"{image} is not pulled on {host_name()}"
    tag = image_tag(conf, tenant, image)
    if not tag:
        return f"image {image} is not baked on {host_name()}"
    if recipe_sha and tag.get("recipe") != recipe_sha:
        return f"image {image} is stale: its recipe changed since the bake"
    if tag.get("toolchain") != toolchain():
        return (f"image {image} is stale: baked on {tag.get('toolchain')}, "
                f"host is now {toolchain()}")
    return ""


def _free_seats(conf, guests, leases, tenant, image) -> int:
    free = 0
    for g in guests:
        if g["role"] == "shared" and g["tenant"] == tenant and g["image"] == image:
            taken = sum(1 for l in leases if l["guest"] == g["name"])
            free += max(conf["seats_per_guest"] - taken, 0)
    return free


# ---------------------------------------------------------------- admit

def admit(req: dict) -> dict:
    """Admit the ticket now, or keep it queued and say why."""
    conf = settings.load()
    tenant, image, kind = req["tenant"], req["image"], req.get("kind", "seat")
    if kind not in KINDS:
        raise Refused(f"kind is one of {', '.join(KINDS)}, not {kind!r}")
    refusal = _admits(conf["mode"], tenant) or (
        kind == "direct" and _direct_refusal(conf, tenant))
    if refusal:
        return {"state": "refused", "reason": refusal}
    stale = kind != "direct" and _image_state(conf, tenant, image, req.get("recipe"))
    if stale:
        return {"state": "refused", "reason": stale}
    ticket = req.get("ticket") or uuid.uuid4().hex
    now = time.time()
    t = tart_for(conf, tenant)
    with ledger.open_db(settings.root(conf), write=True) as db:
        db.execute("DELETE FROM tickets WHERE polled < ?",
                   (now - conf["ticket_seconds"],))
        db.execute("INSERT INTO tickets (id,tenant,image,kind,created,polled) "
                   "VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET polled=?",
                   (ticket, tenant, image, kind, now, now, now))
        first = db.execute("SELECT id FROM tickets ORDER BY created LIMIT 1").fetchone()[0]
        if first != ticket:
            ahead = db.execute("SELECT COUNT(*) FROM tickets WHERE created < "
                               "(SELECT created FROM tickets WHERE id=?)",
                               (ticket,)).fetchone()[0]
            return {"state": "queued", "ticket": ticket,
                    "reason": f"{ahead} job(s) ahead on {host_name()}"}
        plan = _place(db, conf, tenant, image, kind)
        if "reason" in plan:
            return {"state": "queued", "ticket": ticket, "reason": plan["reason"]}
        db.execute("DELETE FROM tickets WHERE id=?", (ticket,))
        for victim in plan.get("evict", []):
            db.execute("DELETE FROM guests WHERE name=?", (victim,))
        if plan.get("boot"):
            ledger.add_guest(db, plan["guest"], tenant, image,
                             "own" if kind == "own" else "shared")
        elif plan.get("adopt_warm"):
            db.execute("UPDATE guests SET role=? WHERE name=?",
                       ("own" if kind == "own" else "shared", plan["guest"]))
        lease = {"id": "L" + uuid.uuid4().hex[:10], "tenant": tenant,
                 "image": image, "kind": kind, "guest": plan["guest"],
                 "seat": "", "owner": req["owner"], "client": req["client"]}
        if kind == "seat":
            lease["seat"] = _seat_name(db, plan["guest"])
        ledger.add_lease(db, lease)
        db.execute("UPDATE leases SET state='booting' WHERE id=?", (lease["id"],))
    try:
        for victim in plan.get("evict", []):
            _guest_tart(conf, victim).delete(victim)
        if kind == "direct":
            _direct_dir(conf, lease).mkdir(parents=True, exist_ok=True)
        elif plan.get("boot"):
            t.clone(image_vm(image), plan["guest"])
            t.configure(plan["guest"], conf["guest_cpu"], conf["guest_mem_gb"])
            t.boot(plan["guest"], conf["boot_seconds"])
        elif not t.running(plan["guest"]):
            t.boot(plan["guest"], conf["boot_seconds"])
        if kind == "seat":
            _make_seat(t, plan["guest"], lease["seat"])
    except BaseException as exc:
        _reap_lease(conf, lease["id"])
        return {"state": "failed", "reason": f"could not start a guest: {exc}"}
    with ledger.open_db(settings.root(conf), write=True) as db:
        db.execute("UPDATE leases SET state='active', renewed=? WHERE id=?",
                   (time.time(), lease["id"]))
    return {"state": "admitted", "lease": {**lease, "host": host_name(),
                                           "state": "active"}}


KINDS = ("own", "seat", "direct")


def _direct_refusal(conf: dict, tenant: str) -> str:
    """Why this host will not run a tenant's work on itself, or empty."""
    if tenant not in conf["direct_tenants"]:
        return f"{host_name()} runs no direct work for {tenant} (direct_tenants)"
    return ""


def _running(guests: list[dict]) -> list[dict]:
    """The guests that hold a slot: booted for a lease, not parked."""
    return [g for g in guests if g["role"] != "warm" and not g.get("parked")]


def _place(db, conf, tenant, image, kind) -> dict:
    guests = ledger.guests(db)
    leases = ledger.leases(db)
    if kind == "direct":
        held = sum(1 for l in leases if l["kind"] == "direct")
        if held >= conf["direct_slots"]:
            return {"reason": f"all {conf['direct_slots']} direct slot(s) on "
                              f"{host_name()} are in use"}
        return {"guest": f"d-{tenant}-{uuid.uuid4().hex[:6]}"}
    if kind == "seat":
        for g in guests:
            if g["role"] == "shared" and g["tenant"] == tenant and g["image"] == image:
                taken = sum(1 for l in leases if l["guest"] == g["name"])
                if taken < conf["seats_per_guest"]:
                    return {"guest": g["name"]}
    cap = conf["tenant_caps"].get(tenant)
    mine = [g for g in guests if g["tenant"] == tenant and g["role"] != "warm"]
    if cap is not None and len(mine) >= cap:
        return {"reason": f"tenant {tenant} is at its cap of {cap} guest(s)"}
    for g in guests:
        if g["role"] == "warm" and g["tenant"] == tenant and g["image"] == image:
            return {"guest": g["name"], "adopt_warm": True}
    busy = _running(guests)
    if len(busy) >= conf["max_guests"]:
        return {"reason": f"all {conf['max_guests']} guest slot(s) on "
                          f"{host_name()} are in use"}
    evict = []
    if len(busy) + sum(g["role"] == "warm" for g in guests) >= conf["max_guests"]:
        evict = [next(g["name"] for g in guests if g["role"] == "warm")]
    return {"guest": f"g-{tenant}-{uuid.uuid4().hex[:6]}", "boot": True,
            "evict": evict}


def _seat_name(db, guest: str) -> str:
    used = {l["seat"] for l in ledger.leases(db, guest=guest)}
    n = 1
    while f"seat{n}" in used:
        n += 1
    return f"seat{n}"


def _make_seat(t: Tart, guest: str, seat: str) -> None:
    """A seat is its own macOS user: own home, simulators and DerivedData.

    A home is made 700: every seat is in `staff`, which macOS lets read a home."""
    script = (f"id {seat} >/dev/null 2>&1 || sudo sysadminctl -addUser {seat} "
              f"-password {uuid.uuid4().hex} -home /Users/{seat} >/dev/null 2>&1; "
              f"sudo createhomedir -c -u {seat} >/dev/null 2>&1; "
              f"sudo chmod 700 /Users/{seat}; id {seat}")
    t.exec(guest, ["sh", "-c", script], timeout=120)


def _drop_seat(t: Tart, guest: str, seat: str) -> None:
    t.exec(guest, ["sh", "-c",
                   f"sudo pkill -9 -u {seat}; sudo sysadminctl -deleteUser {seat}"
                   f" >/dev/null 2>&1; sudo rm -rf /Users/{seat}"],
           check=False, timeout=120)


def _guest_tart(conf: dict, guest: str) -> Tart:
    with ledger.open_db(settings.root(conf)) as db:
        rows = ledger.guests(db, name=guest)
    tenant = rows[0]["tenant"] if rows else guest.split("-")[1]
    return tart_for(conf, tenant)


# ---------------------------------------------------------------- lifecycle

def _lease(db, lease_id: str) -> dict:
    rows = ledger.leases(db, id=lease_id)
    if not rows:
        raise Refused(f"no lease {lease_id} on {host_name()}")
    return rows[0]


def renew(req: dict) -> dict:
    conf = settings.load()
    with ledger.open_db(settings.root(conf), write=True) as db:
        lease = _lease(db, req["lease"])
        if lease["state"] == "orphan":
            return {"state": "orphan"}
        db.execute("UPDATE leases SET renewed=? WHERE id=?", (time.time(), lease["id"]))
    return {"state": lease["state"]}


def orphan(req: dict) -> dict:
    conf = settings.load()
    with ledger.open_db(settings.root(conf), write=True) as db:
        _lease(db, req["lease"])
        db.execute("UPDATE leases SET state='orphan', orphaned=? WHERE id=? "
                   "AND state != 'orphan'", (time.time(), req["lease"]))
    return {"state": "orphan", "grace_minutes": conf["grace_minutes"]}


def assign(req: dict) -> dict:
    """Move a lease to another session, from any state but reaped."""
    conf = settings.load()
    with ledger.open_db(settings.root(conf), write=True) as db:
        lease = _lease(db, req["lease"])
        if lease["tenant"] != req["tenant"]:
            raise Refused(f"{lease['id']} belongs to tenant {lease['tenant']}")
        parked = any(g["parked"] for g in ledger.guests(db, name=lease["guest"]))
        db.execute("UPDATE leases SET owner=?, client=?, state=?, "
                   "orphaned=0, renewed=? WHERE id=?",
                   (json.dumps(req["owner"]), req["client"],
                    "parked" if parked else "active", time.time(), lease["id"]))
        lease = _lease(db, req["lease"])
    return {"lease": {**lease, "host": host_name()}}


def release(req: dict) -> dict:
    conf = settings.load()
    with ledger.open_db(settings.root(conf)) as db:
        lease = _lease(db, req["lease"])
    if req.get("tenant") and lease["tenant"] != req["tenant"]:
        raise Refused(f"{lease['id']} belongs to tenant {lease['tenant']}")
    return {"released": _reap_lease(conf, lease["id"])}


def _reap_lease(conf: dict, lease_id: str) -> dict:
    """Remove a lease and whatever only it held, then prove it is gone."""
    with ledger.open_db(settings.root(conf), write=True) as db:
        rows = ledger.leases(db, id=lease_id)
        if not rows:
            return {"lease": lease_id, "gone": True}
        lease = rows[0]
        db.execute("DELETE FROM leases WHERE id=?", (lease_id,))
        others = ledger.leases(db, guest=lease["guest"])
        if not others:
            db.execute("DELETE FROM guests WHERE name=?", (lease["guest"],))
    _drop_links(conf, lease_id)
    if lease["kind"] == "direct":
        return {"lease": lease_id, **_close_direct(conf, lease)}
    t = tart_for(conf, lease["tenant"])
    if others:
        if lease["seat"]:
            _drop_seat(t, lease["guest"], lease["seat"])
        return {"lease": lease_id, "gone": True, "guest_kept": lease["guest"]}
    t.delete(lease["guest"])
    gone = lease["guest"] not in t.names()
    return {"lease": lease_id, "gone": gone, "guest": lease["guest"]}


def reset(req: dict) -> dict:
    conf = settings.load()
    with ledger.open_db(settings.root(conf)) as db:
        lease = _lease(db, req["lease"])
    if lease["kind"] == "direct":
        _close_direct(conf, lease)
        _direct_dir(conf, lease).mkdir(parents=True, exist_ok=True)
        return {"reset": lease["id"], "direct": str(_direct_dir(conf, lease))}
    t = tart_for(conf, lease["tenant"])
    if lease["kind"] == "seat":
        _drop_seat(t, lease["guest"], lease["seat"])
        _make_seat(t, lease["guest"], lease["seat"])
        return {"reset": lease["id"], "seat": lease["seat"]}
    if lease["state"] == "parked":
        raise Refused(f"{lease['id']} is parked; resume it first")
    t.delete(lease["guest"])
    t.clone(image_vm(lease["image"]), lease["guest"])
    t.configure(lease["guest"], conf["guest_cpu"], conf["guest_mem_gb"])
    t.boot(lease["guest"], conf["boot_seconds"])
    return {"reset": lease["id"], "guest": lease["guest"]}


def _own_lease(db, req: dict, key: str = "lease", guest: bool = True) -> dict:
    lease = _lease(db, req[key])
    if req.get("tenant") and lease["tenant"] != req["tenant"]:
        raise Refused(f"{lease['id']} belongs to tenant {lease['tenant']}")
    if guest and lease["kind"] == "direct":
        raise Refused(f"{lease['id']} runs directly on {host_name()}: it has no guest")
    return lease


def ip(req: dict) -> dict:
    """The lease's guest address, asked of the guest each time."""
    conf = settings.load()
    with ledger.open_db(settings.root(conf)) as db:
        lease = _own_lease(db, req)
    if lease["state"] not in ("active", "orphan"):
        raise Refused(f"{lease['id']} is {lease['state']}")
    addr = tart_for(conf, lease["tenant"]).ip(lease["guest"], conf["boot_seconds"])
    with ledger.open_db(settings.root(conf), write=True) as db:
        db.execute("UPDATE guests SET ip=? WHERE name=?", (addr, lease["guest"]))
    return {"lease": lease["id"], "ip": addr}


def park(req: dict) -> dict:
    """Stop a whole guest and keep it, lease and all; it frees its slot."""
    conf = settings.load()
    with ledger.open_db(settings.root(conf), write=True) as db:
        lease = _own_lease(db, req)
        if lease["kind"] != "own":
            raise Refused("only a whole guest parks; a seat shares its guest")
        if lease["state"] not in ("active", "parked"):
            raise Refused(f"{lease['id']} is {lease['state']}")
        db.execute("UPDATE guests SET parked=1 WHERE name=?", (lease["guest"],))
        db.execute("UPDATE leases SET state='parked', renewed=? WHERE id=?",
                   (time.time(), lease["id"]))
    tart_for(conf, lease["tenant"]).stop(lease["guest"])
    return {"parked": lease["id"], "guest": lease["guest"]}


def resume(req: dict) -> dict:
    """Boot a parked guest again, as it was left, when a slot is free."""
    conf = settings.load()
    with ledger.open_db(settings.root(conf), write=True) as db:
        lease = _own_lease(db, req)
        if lease["state"] != "parked":
            return {"resumed": lease["id"], "state": lease["state"]}
        guests = ledger.guests(db)
        if len(_running(guests)) >= conf["max_guests"]:
            raise Refused(f"all {conf['max_guests']} guest slot(s) on "
                          f"{host_name()} are in use")
        warm = [g["name"] for g in guests if g["role"] == "warm"]
        evict = warm[:max(len(_running(guests)) + len(warm) + 1 - conf["max_guests"], 0)]
        for victim in evict:
            db.execute("DELETE FROM guests WHERE name=?", (victim,))
        db.execute("UPDATE guests SET parked=0 WHERE name=?", (lease["guest"],))
        db.execute("UPDATE leases SET state='booting', renewed=? WHERE id=?",
                   (time.time(), lease["id"]))
    for victim in evict:
        _guest_tart(conf, victim).delete(victim)
    try:
        tart_for(conf, lease["tenant"]).boot(lease["guest"], conf["boot_seconds"],
                                             )
    except BaseException:
        with ledger.open_db(settings.root(conf), write=True) as db:
            db.execute("UPDATE guests SET parked=1 WHERE name=?", (lease["guest"],))
            db.execute("UPDATE leases SET state='parked' WHERE id=?", (lease["id"],))
        raise
    with ledger.open_db(settings.root(conf), write=True) as db:
        db.execute("UPDATE leases SET state='active', renewed=? WHERE id=?",
                   (time.time(), lease["id"]))
    return {"resumed": lease["id"], "state": "active"}


def ls(req: dict) -> dict:
    conf = settings.load()
    with ledger.open_db(settings.root(conf)) as db:
        return {"host": host_name(), "mode": conf["mode"],
                "leases": ledger.leases(db), "guests": ledger.guests(db),
                "tickets": [dict(r) for r in db.execute(
                    "SELECT * FROM tickets ORDER BY created")]}


# ---------------------------------------------------------------- tick

def tick(req: dict | None = None) -> dict:
    """Expire, orphan, reap and keep the warm pool. One tick at a time."""
    conf = settings.load()
    root = settings.root(conf)
    root.mkdir(parents=True, exist_ok=True)
    with open(root / "tick.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"skipped": "another tick is running"}
        return _tick(conf)


def _tick(conf: dict) -> dict:
    now, done = time.time(), {"orphaned": [], "reaped": [], "strays": [],
                               "warmed": [], "cooled": []}
    with ledger.open_db(settings.root(conf), write=True) as db:
        for lease in (ledger.leases(db, state="active") +
                      ledger.leases(db, state="parked")):
            if lease["renewed"] < now - conf["expire_seconds"]:
                db.execute("UPDATE leases SET state='orphan', orphaned=? WHERE id=?",
                           (now, lease["id"]))
                done["orphaned"].append(lease["id"])
        db.execute("DELETE FROM tickets WHERE polled < ?", (now - conf["ticket_seconds"],))
        expired = [l["id"] for l in ledger.leases(db, state="orphan")
                   if l["orphaned"] < now - conf["grace_minutes"] * 60]
        stuck = [l["id"] for l in ledger.leases(db, state="booting")
                 if l["created"] < now - 2 * conf["boot_seconds"]]
    for lease_id in expired + stuck:
        done["reaped"].append(_reap_lease(conf, lease_id))
    done["strays"] = _reap_strays(conf) + _drop_links(conf)
    with ledger.open_db(settings.root(conf)) as db:
        waiting = db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        warm = ledger.guests(db, role="warm")
    if conf["mode"] != "free" or waiting:
        for g in warm:
            with ledger.open_db(settings.root(conf), write=True) as db:
                db.execute("DELETE FROM guests WHERE name=?", (g["name"],))
            tart_for(conf, g["tenant"]).delete(g["name"])
            done["cooled"].append(g["name"])
        return done
    done["warmed"] = _warm(conf)
    return done


def _reap_strays(conf: dict) -> list[str]:
    """Guests this system named under its own root that no ledger row holds."""
    tenants = settings.root(conf) / "tenants"
    if not tenants.exists():
        return []
    with ledger.open_db(settings.root(conf)) as db:
        known = {g["name"] for g in ledger.guests(db)}
    reaped = []
    for folder in tenants.iterdir():
        if not (folder / "tart").exists():
            continue
        t = Tart(settings.tart_bin(conf), folder / "tart")
        try:
            names = t.names()
        except (TartError, ValueError):
            continue
        for name in names:
            if name.startswith(f"g-{folder.name}-") and name not in known:
                t.delete(name)
                reaped.append(name)
    return reaped


def _warm(conf: dict) -> list[str]:
    warmed = []
    for key, want in conf["warm"].items():
        tenant, _, image = key.partition(":")
        if not image or _image_state(conf, tenant, image, None):
            continue
        with ledger.open_db(settings.root(conf), write=True) as db:
            guests = ledger.guests(db)
            have = sum(1 for g in guests if g["role"] == "warm"
                       and g["tenant"] == tenant and g["image"] == image)
            booted = len(_running(guests)) + sum(g["role"] == "warm" for g in guests)
            if have >= want or booted >= conf["max_guests"]:
                continue
            name = f"g-{tenant}-{uuid.uuid4().hex[:6]}"
            ledger.add_guest(db, name, tenant, image, "warm")
        t = tart_for(conf, tenant)
        try:
            t.clone(image_vm(image), name)
            t.configure(name, conf["guest_cpu"], conf["guest_mem_gb"])
            t.boot(name, conf["boot_seconds"])
            warmed.append(name)
        except (TartError, OSError, subprocess.SubprocessError):
            with ledger.open_db(settings.root(conf), write=True) as db:
                db.execute("DELETE FROM guests WHERE name=?", (name,))
            t.delete(name)
    return warmed


# ---------------------------------------------------------------- owner verbs

def mode(req: dict) -> dict:
    if req.get("mode"):
        settings.set_value("mode", req["mode"])
        tick()
    return {"host": host_name(), "mode": settings.load()["mode"]}


def purge(req: dict) -> dict:
    """Remove a tenant's whole footprint from this host and prove it.

    Images named in `keep_images` stay, with their tags, unless the caller asks
    for everything; every other guest, file and process of the tenant goes. The
    host's shared registry cache is never the tenant's to remove: only the
    tenant's link to it goes.
    """
    conf = settings.load()
    tenant = req["tenant"]
    root = tenant_root(conf, tenant)
    keep = set() if req.get("everything") else set(conf["keep_images"])
    with ledger.open_db(settings.root(conf), write=True) as db:
        lease_ids = [l["id"] for l in ledger.leases(db, tenant=tenant)]
        guest_names = [g["name"] for g in ledger.guests(db, tenant=tenant)]
        db.execute("DELETE FROM leases WHERE tenant=?", (tenant,))
        db.execute("DELETE FROM guests WHERE tenant=?", (tenant,))
        db.execute("DELETE FROM tickets WHERE tenant=?", (tenant,))
    t = tart_for(conf, tenant)
    kept_vms = {image_vm(i) for i in keep}
    try:
        for vm in t.list():
            if vm["Name"] in kept_vms or is_registry_ref(vm["Name"]):
                continue
            if keep:
                t.delete(vm["Name"])
            elif vm.get("State") == "running":
                t.stop(vm["Name"])
    except (TartError, ValueError):
        pass
    kept = sorted(i for i in keep if image_vm(i) in t.names()) if keep else []
    if root.exists() and not kept:
        shutil.rmtree(root)
    elif root.exists():
        for child in root.iterdir():
            if child.name not in ("tart", "images"):
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        for tag in (root / "images").glob("*.json"):
            if tag.stem not in kept:
                tag.unlink()
    left = subprocess.run(["pgrep", "-f", str(root)], capture_output=True,
                          text=True).stdout.split()
    return {"tenant": tenant, "leases": lease_ids, "guests": guest_names,
            "kept": kept, "root_gone": not root.exists(), "processes_left": left}


def image_pull(req: dict) -> dict:
    conf = settings.load()
    tart_for(conf, req["tenant"]).pull(req["image"])
    return {"pulled": req["image"]}


def image_tag_lease(req: dict) -> dict:
    """Freeze a lease's guest as a tenant image, recording what made it."""
    conf = settings.load()
    with ledger.open_db(settings.root(conf)) as db:
        lease = _lease(db, req["lease"])
    t = tart_for(conf, lease["tenant"])
    target = image_vm(req["image"])
    t.stop(lease["guest"])
    staging = f"{target}-new"
    t.delete(staging)
    t.clone(lease["guest"], staging)
    if target in t.names():
        t.delete(target)
    t.run("rename", staging, target)
    tag = {"recipe": req["recipe"], "toolchain": toolchain(),
           "base": lease["image"], "baked": time.time()}
    folder = tenant_root(conf, lease["tenant"]) / "images"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{req['image']}.json").write_text(json.dumps(tag, indent=2))
    _reap_lease(conf, lease["id"])
    return {"image": req["image"], "tag": tag}


# ---------------------------------------------------------------- direct

def _direct_dir(conf: dict, lease: dict) -> Path:
    """A direct lease's own folder on the host: its commands start here and
    what it pushes lands here."""
    return tenant_root(conf, lease["tenant"]) / "direct" / lease["id"]


def _groups_file(conf: dict, lease: dict) -> Path:
    return _direct_dir(conf, lease).with_suffix(".groups")


def _lease_procs(conf: dict, lease: dict) -> list[int]:
    """Processes still in the process groups the lease's commands started."""
    try:
        groups = {int(g) for g in _groups_file(conf, lease).read_text().split()}
    except (OSError, ValueError):
        return []
    out = subprocess.run(["ps", "-A", "-o", "pid=,pgid="], capture_output=True,
                         text=True).stdout
    return [int(pid) for pid, pgid in (line.split() for line in out.splitlines())
            if int(pgid) in groups and int(pid) != os.getpid()]


def _sims_named(lease_id: str) -> list[str]:
    out = subprocess.run(["xcrun", "simctl", "list", "devices", "-j"],
                         capture_output=True, text=True).stdout
    try:
        devices = json.loads(out).get("devices", {})
    except ValueError:
        return []
    return [d["udid"] for group in devices.values() for d in group
            if lease_id in d.get("name", "")]


def _close_direct(conf: dict, lease: dict) -> dict:
    """End what a direct lease started on the host, and prove it: its processes,
    the simulators named with its id, its folder. Nothing else is touched."""
    killed = _lease_procs(conf, lease)
    for sig in (15, 9):
        for pid in _lease_procs(conf, lease):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, sig)
        deadline = time.time() + 5
        while _lease_procs(conf, lease) and time.time() < deadline:
            time.sleep(0.2)
    sims = _sims_named(lease["id"])
    for udid in sims:
        subprocess.run(["xcrun", "simctl", "shutdown", udid], capture_output=True)
        subprocess.run(["xcrun", "simctl", "delete", udid], capture_output=True)
    work = _direct_dir(conf, lease)
    shutil.rmtree(work, ignore_errors=True)
    left = _lease_procs(conf, lease) + _sims_named(lease["id"])
    _groups_file(conf, lease).unlink(missing_ok=True)
    return {"gone": not work.exists() and not left, "killed": killed, "sims": sims,
            "left": left}


# ---------------------------------------------------------------- link

def _links_dir(conf: dict) -> Path:
    return settings.root(conf) / "links"


def _gateway(t: Tart, guest: str) -> str:
    """The host's address on the guest's own network: the guest's default route."""
    out = t.exec(guest, ["sh", "-c", "route -n get default | awk '/gateway:/{print $2}'"],
                 timeout=30)
    return out.stdout.strip()


def link(req: dict) -> dict:
    """Make `to`'s port reachable from `lease`'s guest, TCP and UDP, at the
    host's address on that guest's network. A relay on this host carries it,
    bound to that address alone and answering only that guest; nothing is
    opened on the host's own network, and it ends with either lease."""
    conf = settings.load()
    port = int(req["port"])
    with ledger.open_db(settings.root(conf)) as db:
        lease = _own_lease(db, req)
        to = _own_lease(db, req, key="to")
    for l in (lease, to):
        if l["state"] not in ("active", "orphan"):
            raise Refused(f"{l['id']} is {l['state']}")
    t = tart_for(conf, lease["tenant"])
    src = t.ip(lease["guest"], conf["boot_seconds"])
    dst = tart_for(conf, to["tenant"]).ip(to["guest"], conf["boot_seconds"])
    gw = _gateway(t, lease["guest"])
    if not gw:
        raise Refused(f"{lease['id']}: the guest names no default route")
    from .client import self_command
    folder = _links_dir(conf)
    folder.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([*self_command(), "_relay", gw, str(port), src, dst, str(port)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=open(folder / "relay.log", "a"), text=True,
                            start_new_session=True)
    first = proc.stdout.readline().strip()
    proc.stdout.close()
    if first != "ready":
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise Refused(f"the link could not open {gw}:{port}: {first or 'relay exited'}")
    row = {"pid": proc.pid, "lease": lease["id"], "to": to["id"],
           "address": f"{gw}:{port}", "target": f"{dst}:{port}"}
    (folder / f"{lease['id']}-{port}.json").write_text(json.dumps(row))
    return row


def _drop_links(conf: dict, lease_id: str | None = None) -> list[str]:
    """End the links a lease is either end of; with no lease, every link whose
    leases are gone."""
    folder = _links_dir(conf)
    if not folder.exists():
        return []
    if lease_id is None:
        with ledger.open_db(settings.root(conf)) as db:
            live = {l["id"] for l in ledger.leases(db)}
    dropped = []
    for f in folder.glob("*.json"):
        try:
            row = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        ends = {row.get("lease"), row.get("to")}
        if (lease_id in ends) if lease_id else not ends <= live:
            with contextlib.suppress(ProcessLookupError, PermissionError, TypeError):
                os.kill(row.get("pid"), 15)
            f.unlink(missing_ok=True)
            dropped.append(row.get("address", f.stem))
    return dropped


# ---------------------------------------------------------------- in the guest

def _guest_argv(lease: dict, argv: list[str]) -> list[str]:
    # Anything run in a lease can tell it is in one, and which.
    home = ["env", f"JSTACK_SANDBOX_LEASE={lease['id']}",
            "zsh", "-lc", 'cd ~ && exec "$@"', "_", *argv]
    if lease["kind"] != "seat":
        return home
    return ["sudo", "-H", "-u", lease["seat"], *home]


def run_in(lease_id: str, argv: list[str], tty: bool = False,
           interactive: bool = False, tenant: str = "", stdin=None,
           stdout=None) -> int:
    """Run in the lease with this process's stdio, returning the exit code."""
    conf = settings.load()
    with ledger.open_db(settings.root(conf)) as db:
        lease = _lease(db, lease_id)
    if tenant and lease["tenant"] != tenant:
        raise Refused(f"{lease_id} belongs to tenant {lease['tenant']}")
    if lease["state"] not in ("active", "orphan"):
        raise Refused(f"{lease_id} is {lease['state']}")
    if lease["kind"] == "direct":
        work = _direct_dir(conf, lease)
        env = {**os.environ, "JSTACK_SANDBOX_LEASE": lease["id"],
               "JSTACK_SANDBOX_WORK": str(work)}
        # Its own process group, recorded, so a release ends all it started.
        proc = subprocess.Popen(["zsh", "-lc", 'cd "$JSTACK_SANDBOX_WORK" && exec "$@"',
                                 "_", *(argv or ["zsh", "-l"])], env=env, stdin=stdin,
                                stdout=stdout, process_group=0)
        with _groups_file(conf, lease).open("a") as f:
            f.write(f"{proc.pid}\n")
        return proc.wait()
    t = tart_for(conf, lease["tenant"])
    proc = t.exec(lease["guest"], _guest_argv(lease, argv or ["zsh", "-l"]),
                  interactive=interactive or tty, tty=tty, capture=False,
                  stdin=stdin, stdout=stdout)
    return proc.returncode


def push_argv(dest: str) -> list[str]:
    return ["sh", "-c", f"mkdir -p {shlex.quote(dest)} && tar -x -C {shlex.quote(dest)}"]


def pull_argv(path: str) -> list[str]:
    parent, base = os.path.split(path.rstrip("/")) or (".", path)
    return ["sh", "-c", f"tar -c -C {shlex.quote(parent or '.')} {shlex.quote(base)}"]


def cancel(req: dict) -> dict:
    conf = settings.load()
    with ledger.open_db(settings.root(conf), write=True) as db:
        db.execute("DELETE FROM tickets WHERE id=?", (req["ticket"],))
    return {"cancelled": req["ticket"]}


VERBS = {
    "status": status, "admit": admit, "renew": renew, "orphan": orphan,
    "assign": assign, "release": release, "reset": reset, "ls": ls,
    "tick": tick, "mode": mode, "purge": purge, "image-pull": image_pull,
    "image-tag": image_tag_lease, "cancel": cancel, "ip": ip,
    "park": park, "resume": resume, "link": link,
}


def call(verb: str, req: dict) -> dict:
    if verb not in VERBS:
        raise Refused(f"no host verb {verb!r}")
    return VERBS[verb](req)
