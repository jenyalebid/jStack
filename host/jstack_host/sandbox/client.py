"""The calling side: rank the reachable hosts, queue, hold and hand on leases.

A held lease is renewed by a keeper process that lives exactly as long as its
owning session: when the session ends the keeper orphans the lease and exits,
and the host reaps it after the grace window unless someone takes it over.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import host, owner, reach, settings

TIER = {"free": 0, "offload": 1, "local": 1}


class SandboxError(RuntimeError):
    pass


# ---------------------------------------------------------------- transport

def remote_words(conf: dict | None = None) -> list[str]:
    return shlex.split((conf or settings.load())["remote_command"]) + ["sandbox", "host"]


def ssh_argv(conf: dict | None = None) -> list[str]:
    conf = conf or settings.load()
    return ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(conf['connect_seconds'])}"]


def call(target: dict, verb: str, req: dict, conf: dict | None = None) -> dict:
    if target.get("ssh") is None:
        try:
            return host.call(verb, req)
        except host.Refused as exc:
            raise SandboxError(str(exc)) from None
    words = " ".join(remote_words(conf) + [verb])
    proc = subprocess.run(ssh_argv(conf) + [target["ssh"], words],
                          input=json.dumps(req), capture_output=True, text=True)
    lines = proc.stdout.strip().splitlines()
    try:
        out = json.loads(lines[-1]) if lines else {}
    except ValueError:
        out = {}
    if proc.returncode or "error" in out:
        raise SandboxError(f"{target['name']}: " + (out.get("error") or
                           proc.stderr.strip() or f"exit {proc.returncode}"))
    return out


def run(target: dict, lease_id: str, argv: list[str], tty: bool = False,
        interactive: bool = False, tenant: str = "", stdin=None, stdout=None) -> int:
    if target.get("ssh") is None:
        try:
            return host.run_in(lease_id, argv, tty=tty, interactive=interactive,
                               tenant=tenant, stdin=stdin, stdout=stdout)
        except host.Refused as exc:
            raise SandboxError(str(exc)) from None
    words = remote_words() + ["run", lease_id, "--tenant", tenant]
    words += (["--tty"] if tty else []) + (["-i"] if interactive else [])
    words += ["--", *argv]
    ssh = ssh_argv() + (["-t"] if tty else []) + [target["ssh"]]
    return subprocess.run(ssh + [" ".join(shlex.quote(w) if not w.startswith("~/")
                                          else w for w in words)],
                          stdin=stdin, stdout=stdout).returncode


# ---------------------------------------------------------------- registry

def registry_path():
    return settings.state_dir() / "leases.json"


@contextlib.contextmanager
def registry(write: bool = False):
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        yield data
        if write:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
            tmp.replace(path)


def held(lease_id: str) -> dict:
    with registry() as data:
        if lease_id in data:
            return data[lease_id]
    raise SandboxError(f"no lease {lease_id} held from this instance "
                       f"(`jstack-host sandbox ls --all` lists every host's)")


def target_of(entry: dict) -> dict:
    return {"name": entry["host"], "ssh": entry.get("ssh")}


# ---------------------------------------------------------------- placement

def tenant() -> str:
    return host.host_name()


def rank(statuses: list[dict]) -> list[dict]:
    """Placeable hosts, best first: free before offload, most headroom within."""
    def key(s):
        room = s["free_slots"] + s.get("free_seats", 0) + len(s.get("warm", []))
        return (TIER.get(s["mode"], 9), 0 if room else 1, -s["headroom"])
    return sorted(statuses, key=key)


def survey(image: str, recipe: str | None, conf: dict,
           only: str | None = None) -> tuple[list[dict], list[str]]:
    me = tenant()
    cands = [c for c in reach.candidates(me) if only in (None, c["name"])]
    if only and not cands:
        return [], [f"{only}: not reachable from this instance"]
    req = {"tenant": me, "image": image, "recipes": {image: recipe} if recipe else {}}

    def one(c):
        try:
            return {**call(c, "status", req, conf), "target": c}
        except SandboxError as exc:
            return {"target": c, "error": str(exc)}
    with ThreadPoolExecutor(max_workers=max(len(cands), 1)) as pool:
        found = list(pool.map(one, cands))
    usable, why = [], []
    for s in found:
        name = s["target"]["name"]
        if "error" in s:
            why.append(f"{name}: unreachable ({s['error']})")
        elif s["refusal"]:
            why.append(f"{name}: {s['refusal']}")
        elif s["images"].get(image, f"image {image} is not on {s['host']}"):
            why.append(f"{name}: {s['images'].get(image) or f'image {image} is not on it'}")
        else:
            usable.append(s)
    return rank(usable), why


def get(image: str, kind: str = "seat", wait: float | None = None,
        recipe: str | None = None, say=print, who: dict | None = None,
        net: str = "", near: str | None = None) -> dict:
    """`near` is a held lease: the new one lands on that lease's host or waits."""
    conf = settings.load()
    only = held(near)["host"] if near else None
    who = who or owner.current()
    ticket = uuid.uuid4().hex
    deadline = time.time() + wait if wait else None
    asked: list[dict] = []
    last = ""
    try:
        while True:
            ranked, why = survey(image, recipe, conf, only)
            if not ranked:
                raise SandboxError("no host can take this job:\n  " + "\n  ".join(why))
            reasons, refused = [], 0
            for s in ranked:
                if s["target"] not in asked:
                    asked.append(s["target"])
                out = call(s["target"], "admit", {
                    "ticket": ticket, "tenant": tenant(), "image": image,
                    "kind": kind, "net": net, "owner": who, "client": tenant(),
                    "recipe": recipe}, conf)
                if out["state"] == "admitted":
                    lease = out["lease"]
                    _hold(lease, s["target"])
                    return lease
                reasons.append(f"{s['target']['name']}: {out['reason']}")
                # A failed boot is final for this ask: another try boots a
                # fresh guest the same way, without end when nothing waits.
                refused += out["state"] in ("refused", "failed")
            if refused == len(ranked):
                raise SandboxError("no host can take this job:\n  " + "\n  ".join(reasons))
            line = "queued — " + "; ".join(reasons)
            if line != last:
                say(line)
                last = line
            if deadline and time.time() > deadline:
                raise SandboxError("gave up waiting: " + "; ".join(reasons))
            time.sleep(min(conf["queue_poll_seconds"],
                           max(deadline - time.time(), 0) if deadline else 1e9))
    finally:
        for target in asked:
            with contextlib.suppress(SandboxError):
                call(target, "cancel", {"ticket": ticket}, conf)


def _hold(lease: dict, target: dict) -> None:
    with registry(write=True) as data:
        data[lease["id"]] = {"host": target["name"], "ssh": target.get("ssh"),
                             "tenant": lease["tenant"], "image": lease["image"],
                             "kind": lease["kind"], "guest": lease["guest"],
                             "net": lease.get("net", ""),
                             "seat": lease.get("seat", ""), "owner": lease["owner"],
                             "state": "active"}
    start_keeper(lease["id"])


# ---------------------------------------------------------------- keeper

def keeper_pidfile(lease_id: str):
    return settings.state_dir() / "keepers" / f"{lease_id}.pid"


def self_command() -> list[str]:
    """How to run this package again: the sealed app's CLI takes `sandbox`, not `-m`."""
    launched = os.path.basename(sys.argv[0] or "")
    if launched == "JStackCLI":
        return [os.path.abspath(sys.argv[0]), "sandbox"]
    return [sys.executable, "-m", "jstack_host.sandbox"]


def start_keeper(lease_id: str) -> None:
    pidfile = keeper_pidfile(lease_id)
    with contextlib.suppress(OSError, ValueError):
        pid = int(pidfile.read_text())
        os.kill(pid, 0)
        return
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [*self_command(), "_keep", lease_id],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=open(settings.state_dir() / "keepers" / f"{lease_id}.log", "a"),
        start_new_session=True, env=os.environ.copy())
    pidfile.write_text(str(proc.pid))


def keep(lease_id: str) -> int:
    """Renew while the owner lives; orphan the lease the moment it does not."""
    conf = settings.load()
    try:
        while True:
            try:
                entry = held(lease_id)
            except SandboxError:
                return 0
            target = target_of(entry)
            if owner.alive(entry["owner"]):
                try:
                    out = call(target, "renew", {"lease": lease_id}, conf)
                except SandboxError as exc:
                    if "no lease" in str(exc):
                        _forget(lease_id)
                        return 0
                    out = {}
                if out.get("state") == "orphan":
                    call(target, "assign", {"lease": lease_id, "owner": entry["owner"],
                                            "client": tenant(), "tenant": entry["tenant"]}, conf)
            else:
                with contextlib.suppress(SandboxError):
                    call(target, "orphan", {"lease": lease_id}, conf)
                with registry(write=True) as data:
                    if lease_id in data:
                        data[lease_id]["state"] = "orphan"
                return 0
            time.sleep(conf["renew_seconds"])
    finally:
        with contextlib.suppress(OSError):
            keeper_pidfile(lease_id).unlink()


def _forget(lease_id: str) -> None:
    with registry(write=True) as data:
        data.pop(lease_id, None)


# ---------------------------------------------------------------- verbs

def release(lease_id: str) -> dict:
    entry = held(lease_id)
    out = call(target_of(entry), "release", {"lease": lease_id, "tenant": entry["tenant"]})
    _forget(lease_id)
    return out


def lease_verb(verb: str, lease_id: str) -> dict:
    """A host verb on one held lease: ip, park, resume."""
    entry = held(lease_id)
    out = call(target_of(entry), verb, {"lease": lease_id, "tenant": entry["tenant"]})
    if verb in ("park", "resume"):
        with registry(write=True) as data:
            if lease_id in data:
                data[lease_id]["state"] = out.get("state", "parked")
    return out


def assign(lease_id: str, session: str | None = None, at: str | None = None) -> dict:
    """Move a lease to `session` (default: the caller's own), even an orphan."""
    if session:
        who = owner.find_session(session)
        if not who:
            raise SandboxError(f"no live session {session} on this instance")
    else:
        who = owner.current()
    try:
        entry = held(lease_id)
        target = target_of(entry)
    except SandboxError:
        if not at:
            raise SandboxError(f"{lease_id} is not held from here; name its host "
                               f"with --host (see `sandbox ls --all`)") from None
        target = next((c for c in reach.candidates(tenant()) if c["name"] == at), None)
        if not target:
            raise SandboxError(f"{at} is not reachable from this instance") from None
    out = call(target, "assign", {"lease": lease_id, "owner": who,
                                  "client": tenant(), "tenant": tenant()})
    lease = out["lease"]
    with registry(write=True) as data:
        data[lease_id] = {"host": target["name"], "ssh": target.get("ssh"),
                          "tenant": lease["tenant"], "image": lease["image"],
                          "kind": lease["kind"], "guest": lease["guest"],
                          "seat": lease.get("seat", ""), "owner": who,
                          "state": "active"}
    start_keeper(lease_id)
    return lease


def assign_from(source_sid: str, session: str | None = None) -> list[dict]:
    """Move every lease a session holds from this instance, as a handoff does."""
    with registry() as data:
        ids = [k for k, v in data.items() if v["owner"].get("sid") == source_sid]
    return [assign(lease_id, session) for lease_id in ids]


def mine(everyone: bool = False) -> list[dict]:
    with registry() as data:
        rows = [{"id": k, **v} for k, v in data.items()]
    if everyone:
        return rows
    me = owner.current()
    return [r for r in rows if r["owner"].get("pid") == me["pid"]
            and r["owner"].get("start") == me["start"]]


def fleet() -> list[dict]:
    out = []
    for c in reach.candidates(tenant()):
        try:
            out.append(call(c, "ls", {}))
        except SandboxError as exc:
            out.append({"host": c["name"], "error": str(exc)})
    return out
