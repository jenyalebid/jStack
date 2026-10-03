"""`jstack-host sandbox …` — the whole surface. System doc: ~/Systems/sandbox/SYSTEM.md."""
from __future__ import annotations

import argparse
import json
import sys

from . import client, host, images, owner, settings


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def _lease_line(lease: dict) -> str:
    seat = f" {lease['seat']}" if lease.get("seat") else ""
    return (f"{lease['id']}  {lease['image']}  {lease['kind']}{seat}  "
            f"on {lease.get('host', '?')}  guest {lease['guest']}  "
            f"{lease.get('state', '')}")


def cmd_get(a) -> int:
    say = (lambda line: print(line, file=sys.stderr))
    lease = client.get(a.image, kind="own" if a.own else "seat", wait=a.wait,
                       recipe=images.sha(a.image), say=say)
    if a.json:
        _print(lease)
    else:
        print(lease["id"])
        print(_lease_line(lease), file=sys.stderr)
    return 0


def _entry_target(lease_id):
    entry = client.held(lease_id)
    return entry, client.target_of(entry)


def cmd_exec(a) -> int:
    entry, target = _entry_target(a.lease)
    argv = a.argv[1:] if a.argv and a.argv[0] == "--" else a.argv
    return client.run(target, a.lease, argv, tenant=entry["tenant"],
                      interactive=not sys.stdin.isatty())


def cmd_shell(a) -> int:
    entry, target = _entry_target(a.lease)
    return client.run(target, a.lease, [], tty=sys.stdin.isatty(),
                      interactive=True, tenant=entry["tenant"])


def cmd_push(a) -> int:
    return images.push(a.lease, a.src, a.dest, a.exclude)


def cmd_pull(a) -> int:
    return images.pull(a.lease, a.path, a.dest)


def cmd_reset(a) -> int:
    entry, target = _entry_target(a.lease)
    _print(client.call(target, "reset", {"lease": a.lease}))
    return 0


def cmd_release(a) -> int:
    _print(client.release(a.lease))
    return 0


def cmd_assign(a) -> int:
    if a.source:
        for lease in client.assign_from(a.source):
            print(_lease_line(lease))
        return 0
    if not a.lease:
        raise client.SandboxError("name a lease, or --from <session> for all of one session's")
    lease = client.assign(a.lease, a.session, at=a.host)
    print(_lease_line({**lease, "host": lease.get("host")}))
    return 0


def cmd_ls(a) -> int:
    if a.all:
        rows = client.fleet()
        if a.json:
            _print(rows)
            return 0
        for h in rows:
            if "error" in h:
                print(f"{h['host']}: {h['error']}")
                continue
            print(f"{h['host']}  mode {h['mode']}  guests {len(h['guests'])}  "
                  f"waiting {len(h['tickets'])}")
            for lease in h["leases"]:
                who = lease["owner"].get("sid") or lease["owner"].get("pid")
                print(f"  {_lease_line({**lease, 'host': h['host']})}  "
                      f"tenant {lease['tenant']}  owner {who}")
        return 0
    rows = client.mine(everyone=a.instance)
    if a.json:
        _print(rows)
        return 0
    for row in rows:
        print(_lease_line(row))
    return 0


def cmd_mode(a) -> int:
    out = host.mode({"mode": a.mode} if a.mode else {})
    print(out["mode"])
    return 0


def cmd_settings(a) -> int:
    if a.key is None:
        _print(settings.load())
    elif a.value is None:
        _print(settings.load().get(a.key))
    else:
        _print(settings.set_value(a.key, settings.parse_value(a.value))[a.key])
    return 0


def cmd_image(a) -> int:
    say = (lambda line: print(line, file=sys.stderr))
    if a.action == "bake":
        _print(images.bake(a.name, say=say))
    elif a.action == "pull":
        _print(host.image_pull({"tenant": client.tenant(), "image": a.name}))
    else:
        _print({name: {"recipe": images.sha(name)}
                for name in ([a.name] if a.name else images.names())})
    return 0


def cmd_purge(a) -> int:
    _print(host.purge({"tenant": a.tenant, "everything": a.everything}))
    return 0


def cmd_tick(a) -> int:
    _print(host.tick())
    return 0


def cmd_whoami(a) -> int:
    _print({"tenant": client.tenant(), "owner": owner.current()})
    return 0


def cmd_guard(a) -> int:
    from . import guard
    return guard.main()


def cmd_host(a) -> int:
    """The machine-to-machine door: JSON in on stdin, JSON out on stdout."""
    if a.verb == "run":
        argv = a.rest[1:] if a.rest and a.rest[0] == "--" else a.rest
        try:
            return host.run_in(a.lease, argv, tty=a.tty, interactive=a.i,
                               tenant=a.tenant)
        except host.Refused as exc:
            print(str(exc), file=sys.stderr)
            return 2
    raw = sys.stdin.read()
    try:
        out = host.call(a.verb, json.loads(raw) if raw.strip() else {})
    except (host.Refused, KeyError, ValueError) as exc:
        out = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(out, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="jstack-host sandbox",
                                 description="ready machines by purpose, leased to a session")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("get", help="queue, wait, print the lease")
    p.add_argument("image")
    p.add_argument("--own", action="store_true", help="a whole guest, not a seat")
    p.add_argument("--wait", type=float, default=None, help="give up after N seconds")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_get)

    p = sub.add_parser("exec", help="run a command in the lease")
    p.add_argument("lease")
    p.add_argument("argv", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_exec)

    p = sub.add_parser("shell", help="an interactive shell in the lease")
    p.add_argument("lease")
    p.set_defaults(fn=cmd_shell)

    p = sub.add_parser("push", help="copy a local directory into the lease")
    p.add_argument("lease")
    p.add_argument("src")
    p.add_argument("dest", nargs="?", default="work")
    p.add_argument("--exclude", action="append", default=[], help="a tar pattern to leave out")
    p.set_defaults(fn=cmd_push)

    p = sub.add_parser("pull", help="bring a path out of the lease")
    p.add_argument("lease")
    p.add_argument("path")
    p.add_argument("dest", nargs="?", default=".")
    p.set_defaults(fn=cmd_pull)

    for name, fn, text in (("reset", cmd_reset, "a fresh guest, or a fresh seat"),
                           ("release", cmd_release, "reap now")):
        p = sub.add_parser(name, help=text)
        p.add_argument("lease")
        p.set_defaults(fn=fn)

    p = sub.add_parser("assign", help="move a lease to a session (default: this one)")
    p.add_argument("lease", nargs="?")
    p.add_argument("session", nargs="?", default=None)
    p.add_argument("--host", default=None, help="the lease's host, when not held from here")
    p.add_argument("--from", dest="source", default=None,
                   help="take every lease this session id holds from this instance")
    p.set_defaults(fn=cmd_assign)

    p = sub.add_parser("ls", help="this session's leases")
    p.add_argument("--all", action="store_true", help="every reachable host's leases and queue")
    p.add_argument("--instance", action="store_true", help="every lease held from this instance")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_ls)

    p = sub.add_parser("mode", help="read or set this host's mode")
    p.add_argument("mode", nargs="?", choices=settings.MODES)
    p.set_defaults(fn=cmd_mode)

    p = sub.add_parser("settings", help="read or set any setting")
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.set_defaults(fn=cmd_settings)

    p = sub.add_parser("image", help="the image library")
    p.add_argument("action", choices=("bake", "pull", "ls"))
    p.add_argument("name", nargs="?")
    p.set_defaults(fn=cmd_image)

    p = sub.add_parser("purge", help="remove a tenant's whole footprint from this host")
    p.add_argument("tenant")
    p.add_argument("--everything", action="store_true", help="the keep_images too")
    p.set_defaults(fn=cmd_purge)

    p = sub.add_parser("tick", help="expire, reap and keep the warm pool now")
    p.set_defaults(fn=cmd_tick)

    p = sub.add_parser("whoami", help="the tenant and owning session a lease would get")
    p.set_defaults(fn=cmd_whoami)

    p = sub.add_parser("guard", help="hook: refuse tests outside a lease")
    p.set_defaults(fn=cmd_guard)

    p = sub.add_parser("host", help=argparse.SUPPRESS)
    p.add_argument("verb")
    p.add_argument("lease", nargs="?")
    p.add_argument("--tenant", default="")
    p.add_argument("--tty", action="store_true")
    p.add_argument("-i", action="store_true")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_host)

    p = sub.add_parser("_keep", help=argparse.SUPPRESS)
    p.add_argument("lease")
    p.set_defaults(fn=lambda a: client.keep(a.lease))
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args) or 0
    except (client.SandboxError, host.Refused, KeyError, ValueError) as exc:
        print(f"sandbox: {exc}", file=sys.stderr)
        return 1
