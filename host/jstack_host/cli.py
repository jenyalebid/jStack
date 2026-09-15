"""`jstack-host` — the one command a person types on their own Mac.

    jstack-host install          turn this Mac into a host, and keep it one
    jstack-host pair "iPhone"    a code to type into the app
    jstack-host pair --open      pair the app on this Mac, no code typed
    jstack-host attach CODE --parent URL   join a parent hub as a managed hub
    jstack-host open             guide this Mac into open mode, and prove it
    jstack-host welcome          open the app on a session that checks this Mac
    jstack-host status           is it up, and what does it know
    jstack-host doctor           what is missing, and how to fix each thing
    jstack-host uninstall        take it back off

Nothing here implements anything. `install_host` owns the LaunchAgent,
`server` owns serving, `enrolment` owns codes; this is the front door that
makes them one command with one `--help`, because "run `python3 -m` against a
module inside a package you have to know the name of" is not an install
instruction anyone should be given.

`pyproject.toml` has pointed `jstack-host` here since the package was split
out, so every `pip install` up to now produced a console script that raised
ImportError on its first line — the first thing a new host would ever have run.

**Every read command adopts the installed host's environment first.** A host
installed with `--state-dir` keeps its board, its devices and its token
somewhere this shell knows nothing about; asking about it from a plain
terminal would otherwise report on a *different*, empty host — "not
provisioned" for a machine with a token, and a pairing code minted into a
store nothing is serving. `adopt_installed_environment` reads that back off the
LaunchAgent, beneath anything the shell set explicitly.
"""

from __future__ import annotations

import argparse
import sys

from . import hostenv, install_host, server


def _adopt(args) -> None:
    """Point this process at the host that is actually installed."""
    install_host.adopt_installed_environment(install_host.plist_path(args.label))
    if getattr(args, "state_dir", None):
        import os
        os.environ["JREMOTE_STATE_DIR"] = args.state_dir
        hostenv.reset_profile()


def _cmd_pair(args) -> int:
    """Mint an enrolment code, the way the app expects to be introduced.

    A code rather than the raw token: it expires, it names the device before
    the device ever connects, and it can be revoked without re-keying every
    other device on the host. `jstack-host token` still prints the token for
    the case where someone is adding a host by hand.

    `--open` is the same code, delivered rather than displayed. On the machine
    that has just installed both halves there is nobody to read a code to —
    the app is right here — so it goes over the `jremote://pair` URL and the
    app spends it without anybody typing anything. See `pair_link`.
    """
    _adopt(args)
    from . import devices, enrolment
    if not devices.provisioned():
        print("this host has no token yet — run `jstack-host install` first.",
              file=sys.stderr)
        return 1
    row = enrolment.mint_code(args.name, created_by="", ttl=args.ttl)
    if getattr(args, "open", False):
        return _hand_to_app(row)

    from . import addresses
    port = getattr(args, "port", None) or addresses.DEFAULT_PORT
    found = addresses.reachable(port)

    if getattr(args, "json", False):
        # One parseable answer for the surfaces that draw this themselves.
        # The menu bar dialog renders it as a QR — the one thing stdout prose
        # cannot carry — so it asks for the parts, not the paragraph.
        import json
        from urllib.parse import urlencode
        payload = {"name": row["name"], "code": row["code"],
                   "expires_in": row["expires_in"], "port": port,
                   "addresses": found}
        if found:
            # The QR carries the mesh address when this host runs one. The
            # tunnel is always-on now, so a paired device reaches 10.66.0.x
            # from any network — while the LAN address this link used to
            # carry is dead the moment the scanning phone is off this wifi,
            # and the redeem endpoint applies no locational rule anyway.
            # A device with no tunnel yet can't be saved by either choice
            # of QR address on cellular; for its one first contact the
            # dialog prints the LAN address as the by-hand path.
            mesh = next((a for a in found if a["kind"] == "mesh"), None)
            query = {"code": row["code"], "url": (mesh or found[0])["url"]}
            if row["name"]:
                query["name"] = row["name"]
            payload["link"] = "jremote://pair?" + urlencode(query)
        print(json.dumps(payload))
        return 0

    mins = row["expires_in"] // 60
    print(f"\n    {row['code']}\n")
    print(f"for {row['name']} — good for {mins} minute{'' if mins == 1 else 's'}.")

    # The address, printed — not named.
    #
    # This line used to read "this Mac's address, and this code", which tells
    # somebody standing at another device to type a thing it never tells them.
    # The host is the only party that knows what to put there (the app on the
    # new device cannot ask a machine it has not reached yet), `addresses` has
    # answered it since the `/host` work, and nothing was printing it. A code
    # beside a blank is half a pairing, and the half that was missing is the
    # half people got stuck on.
    print("\nIn the app on that device: Instances › Add a Mac.")
    # The address turns on ONE fact, and it is one the person holding the
    # device knows and this host cannot: does that device already have the
    # tunnel?
    #
    # "use the first one that fits" printed three addresses and made the reader
    # guess which, with notes that describe each address rather than tell them
    # which to pick. A device paired once holds a conf routing 10.66.0.0/24,
    # and from then on the mesh address is the one that works — from anywhere
    # in the world. That is the steady state of every device here, and it was
    # printed LAST, under a note that reads like a footnote. So a device that
    # could have connected from an office was steered to a LAN address that
    # only resolves inside this building, and got a timeout.
    #
    # Two lines, each under the condition that picks it, mesh first. `.local`
    # is dropped: it needs the same LAN as the numeric address while resolving
    # less reliably on it, so it is never the right answer and never the only
    # one.
    lan = next((a for a in found if a["kind"] == "lan"), None)
    mesh = next((a for a in found if a["kind"] == "mesh"), None)
    if mesh and lan:
        print(f"\n    {mesh['url']}")
        print("    if that device already has the tunnel (anywhere in the "
              "world)")
        print(f"\n    {lan['url']}")
        print("    first time on it, while it is on this network")
    elif mesh or lan:
        print(f"\nAddress:  {(mesh or lan)['url']}")
    else:
        # Never silence. A host that cannot name an address is a host somebody
        # has to go find one for, and saying so beats printing nothing.
        print("\n  This Mac could not work out its own address — check "
              "`jstack-host where` and your network.")
    print(f"\nThen the code above: {row['code']}")
    return 0


def pair_link(code: str, port: int, name: str = "") -> str:
    """The `jremote://pair` URL that hands `code` to the app on this Mac.

    Loopback, always. This link is only ever fired at the app running on the
    host's own machine, and 127.0.0.1 is the one address that is true before
    the machine has a name anything else can resolve — a fresh install has no
    DNS entry, no Bonjour name it has published, and possibly no LAN. The app
    re-settles its own route on every launch anyway (it prefers loopback when
    the host it reaches IS the machine it is on), so this is the starting
    address and not a decision the app is stuck with.
    """
    from urllib.parse import urlencode
    query = {"code": code, "url": f"http://127.0.0.1:{port}"}
    if name:
        query["name"] = name
    return "jremote://pair?" + urlencode(query)


# How long to wait for the app to actually spend the code, and how often to
# look. Seconds, not milliseconds: the app this fires at has, by definition,
# never been opened on this Mac — it has to clear Gatekeeper on a bundle
# downloaded minutes ago, launch, restore its scenes, stand up the board, and
# run one round trip against loopback. Bounded, because the honest answer when
# it does not land is the code itself, and eight characters typed by hand beats
# an installer that sits there.
PAIR_WAIT = 30.0
PAIR_POLL = 0.5


def _pairing_landed(code: str) -> bool:
    """Did an app actually redeem `code`? Blocks up to `PAIR_WAIT`.

    `desk.open_url` returning True means Launch Services accepted a URL. It
    does not mean an app received it, and it certainly does not mean an app
    spent it — this exact gap shipped a pairing step that printed success on
    every fresh Mac while enrolling nothing, because the app dropped the link
    at cold launch (no board window yet, and the pairing sheet lives on the
    board). The store's used bit is the only place the truth is written down,
    so that is what this reads.
    """
    import time
    from . import enrolment

    deadline = time.monotonic() + PAIR_WAIT
    while True:
        if enrolment.state(code) == "used":
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(PAIR_POLL)


def _hand_to_app(row: dict) -> int:
    """Fire the pair link at the local app, and wait to see it spent.

    Exit 0 means this Mac is in the app's grid — nothing else. A machine with
    no app, an app that ignores the link, an app that never comes up: all of
    them are a normal outcome of `--open` (the app step is declinable), the
    code is already minted and still good, so the fallback is to print it
    exactly as the plain form would and exit non-zero. The caller in
    `install.sh` branches on that status to decide whether to claim the two
    halves have met.

    `desk.open_url` rather than a bare `open`, because it pins the link to
    the copy in /Applications. On a machine that has ever built the app, a
    stale bundle in derivedData is registered for the same URL scheme and
    Launch Services is free to prefer it — a pairing that lands in a build
    nobody is looking at, reported here as success.
    """
    from . import desk, install_host

    port = install_host.installed_port() or install_host.DEFAULT_PORT
    link = pair_link(row["code"], port, hostenv.host_name())
    if desk.open_url(link) and _pairing_landed(row["code"]):
        print(f"paired the app on this Mac as {row['name']} — this machine is "
              "in its grid now")
        return 0
    mins = row["expires_in"] // 60
    print("the app on this Mac did not take that link — open it, then type "
          "this code into it:")
    print(f"\n    {row['code']}\n")
    print(f"good for {mins} minute{'' if mins == 1 else 's'}.")
    return 1


# The first thing anyone sees after an install finishes. Addressed to the
# agent, not to the person: the session opens already working, and what it is
# working on is the machine it was just installed on.
#
# It says "check, then say" and not "say" on purpose. An agent that opens by
# congratulating someone on a working install it never looked at is worse than
# an empty window — the empty window at least does not lie, and the first
# impression this makes is the one that decides whether anything it says later
# gets believed.
WELCOME_PROMPT = (
    "You have just been installed on this Mac and this window is the first "
    "thing your owner sees. Do not greet them with a status you have not "
    "checked.\n\n"
    "Run `jstack-doctor` first. Read what it actually says, then tell them in "
    "plain words what works and what does not — no jargon they did not ask "
    "for, and no clean bill of health you did not verify. Repair what you can "
    "repair from here, and say plainly which parts need them.\n\n"
    "Then, briefly: what they now have. This app is where sessions like this "
    "one open; the host running on this Mac is what serves it; you are an "
    "agent with a workspace of your own, and this is it. Keep it to a few "
    "sentences.\n\n"
    "Finish by asking what they want to know, and answer it."
)


def _cmd_welcome(args) -> int:
    """Open the app on a session that explains the install that just ran.

    The install ends with a working machine and no idea what to do with it.
    This is the difference between the two: a session that comes up already
    running, in a real workspace, with the first prompt spent on checking the
    machine rather than on being typed.

    Everything here is a part that already existed — `desk.create` makes the
    managed session, `desk.open_thread` puts it in front of someone. The only
    new thing is which agent gets it and what it is asked to do first.
    """
    _adopt(args)
    import os.path
    from . import desk
    agents = hostenv.active_agents()
    if not agents:
        print("no agent workspaces on this host yet — nothing to open a "
              "session for.", file=sys.stderr)
        return 1
    agent_id = args.agent or sorted(agents)[0]
    if agent_id not in agents:
        known = ", ".join(sorted(agents))
        print(f"no agent {agent_id!r} on this host — there is: {known}",
              file=sys.stderr)
        return 1
    cwd = str(hostenv.workspace(agent_id))
    if not os.path.isdir(cwd):
        print(f"{agent_id}'s workspace is missing at {cwd}", file=sys.stderr)
        return 1
    try:
        sid = desk.create(cwd, nudge=WELCOME_PROMPT)
    except (OSError, RuntimeError) as e:
        print(f"could not start a session: {e}", file=sys.stderr)
        return 1
    if desk.open_thread(sid, cwd):
        print(f"opened a session with {agents[agent_id].get('name') or agent_id}"
              " — it is checking this install over now")
        return 0
    # The session is real and on the board whether or not a window came up, so
    # this is a note about the window, not a failure of the command.
    print(f"started a session ({sid[:8]}) — no app on this Mac took the link, "
          "so open it from the board when you have one.")
    return 0


def _cmd_attach(args) -> int:
    """Join a parent hub's mesh with a code minted on that parent.

    The one deliberate step that turns this Mac into a managed hub: redeem the
    host code, install the leaf tunnel it hands back, and report the mode the
    machine ended up in. `_adopt` first, like every command that reads or writes
    this host's state — the token this earns and the parent record it writes
    belong to the installed host, not to whatever a bare shell would resolve.

    Not gated behind a confirmation: attaching is already the explicit act — a
    person typed `attach`, an address and a one-time code. What it must not do is
    claim success it did not check, so it reads `mode` off the machine afterwards
    and prints that, rather than asserting "managed" because the installer
    returned zero.
    """
    _adopt(args)
    from . import attach_parent, hostenv, mode
    try:
        result = attach_parent.attach(
            args.code, args.parent, host_key=hostenv.host_id(), port=args.port)
    except attach_parent.AttachError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    m = mode.current()
    if getattr(args, "json", False):
        import json
        print(json.dumps({**result, "mode": m}))
        return 0

    host = result.get("host") or {}
    name = host.get("name") or hostenv.host_name()
    verb = "re-attached" if result["superseded"] else "attached"
    print(f"{verb} {name} to {result['parent_url']} — this Mac is a managed "
          "hub now.")
    print(f"\nmode  {m['mode']}{'' if m['live'] else '  (not live)'}")
    print(f"      {m['note']}")
    if not result.get("reachback", True):
        # Not an error and not hidden. The attach did everything it was asked
        # to; the parent simply does not let machines it adopts reach back
        # into it. Unsaid, this becomes an authentication failure discovered
        # days later against a parent that looks perfectly healthy.
        print("\n  That hub does not let the machines it adopts reach back "
              "into it, so the\n  credential this Mac just received is "
              "already revoked. Nothing is wrong:\n  that hub administers "
              "this Mac, and devices paired there reach it. This Mac\n  just "
              "cannot drive that hub. Its owner can change it with "
              "`jstack-host\n  reachback on` and a fresh attach.")
    if m["mode"] != "managed":
        # The installer returned success but the machine does not read as
        # managed — say so instead of letting the mode line be the only tell.
        print("\n  The leaf installed but this machine is not reading as a "
              "managed hub yet — check `jstack-host doctor` and the leaf "
              "daemon logs.", file=sys.stderr)
    return 0


#: What a code is good for when nobody says. Ten minutes suits the case the
#: printed instructions describe — a person at the other keyboard, typing.
ADOPT_TTL = 600


def _adopt_offline(name: str, row: dict, port: int, as_json: bool = False) -> int:
    """Adopt a Mac that has no route here yet, by carrying the tunnel to it.

    The printed flow assumes the far Mac can reach this one to redeem. Off the
    LAN it cannot: this host publishes no public HTTP, the mesh address is
    unroutable until the tunnel exists, and the tunnel is what redeeming hands
    back. That is a closed loop, and no wording of the instructions opens it.

    So the bundle goes to the machine instead of the machine coming to the hub.
    `tunnel.issue(leaf=True)` mints the peer and writes the install folder —
    the same folder, from the same code path, that has always been the way a
    machine joins this mesh. `adopt_offline.emit` adds the ordered runner, so
    what lands on the far Mac is one command rather than a README of steps.
    """
    from . import adopt_offline, tunnel

    try:
        issued = tunnel.issue(name, leaf=True)
        reused = "" if issued.get("created") else " (its existing peer, reused)"
    except (tunnel.PairingUnsupported, tunnel.PairingRefused,
            tunnel.TunnelError) as exc:
        # "already paired" is not a failure here, it is the common case: a Mac
        # that was once a device, or whose bundle folder was deleted, still
        # holds a peer entry. Re-minting would hand the hub a public key that
        # machine cannot produce, so the bundle is rebuilt off the credentials
        # the peer already has and the peer table is left alone.
        if "already paired" not in str(exc):
            print(f"could not mint the tunnel half for {name}: {exc}",
                  file=sys.stderr)
            return 1
        try:
            adopt_offline.relift(name)
        except tunnel.TunnelError as rebuild:
            print(f"could not rebuild the tunnel half for {name}: {rebuild}",
                  file=sys.stderr)
            return 1
        reused = " (rebuilt from its existing peer — keys unchanged)"

    adopt_offline.emit(name, row["code"], port)
    joiner = adopt_offline.pack(name, row["code"], port)
    mins = row["expires_in"] // 60

    if as_json:
        import json
        print(json.dumps({"name": row["name"], "code": row["code"],
                          "kind": row["kind"], "expires_in": row["expires_in"],
                          "port": port, "offline": True, "file": str(joiner)}))
        return 0

    print(f"\nCarry this one file to that Mac{reused}:\n")
    print(f"    {joiner}\n")
    print("Run it there. Everything is inside it — the tunnel keys, the")
    print("installer and the code. It brings the tunnel up first and redeems")
    print("second, which is the only order that can work: the code is redeemed")
    print("over the tunnel it installs.\n")
    print(f"The code is good for {mins} minute{'' if mins == 1 else 's'}. If "
          "you get there after it expires,\nthe trip is still not wasted — the "
          "tunnel is the permanent half, and once it is\nup that Mac can "
          "redeem a fresh code by itself. The file says so if it happens.\n")
    print("That file IS a credential — it carries that machine's private key. "
          "Delete it\nonce the join succeeds.")
    return 0


def _cmd_adopt(args) -> int:
    """Mint a host code — the hub's half of assigning a leaf to itself.

    `pair` mints a code for a *device*; this mints one for a *machine*, and the
    kinds are not interchangeable (a device code redeemed by `attach` hands back
    a client conf where a leaf bundle was needed, which is why `attach` refuses
    one outright). Until now the only way to mint a host code was a hand-rolled
    POST to `/enrolment/codes` with `kind=host` — the joining end had a front
    door and the adopting end had none, so the feature was unusable by anyone who
    was not reading the router source.

    What it prints is the exact command to run on the other Mac, not the parts to
    assemble one from. The address comes from `addresses.reachable` — the same
    answer `pair` prints — because a code beside a blank is half an enrolment,
    and the machine being adopted cannot work out where to send it.
    """
    _adopt(args)
    from . import addresses, devices, enrolment, mode, tunnel
    if not devices.provisioned():
        print("this host has no token yet — run `jstack-host install` first.",
              file=sys.stderr)
        return 1
    # Refused here rather than at redemption, because redemption happens on the
    # OTHER Mac. A host code is a promise of a leaf bundle, and the bundle is a
    # peer minted off this machine's mesh: a code from a Mac with no mesh burns
    # on the far end, with an error that reads as that machine's fault. The
    # honest place to fail is the machine that cannot keep the promise.
    m = mode.current()
    if m["mode"] == "managed":
        print("this Mac is a managed hub itself — it rides another hub's mesh "
              "and has no peers of its own to mint. Adopt from the parent "
              f"({m.get('parent') or 'the hub this one is attached to'}), or "
              "`jstack-host detach` first.", file=sys.stderr)
        return 1
    if not tunnel.can_pair():
        # `can_pair()` and not the mode's hub test, deliberately: the mode will
        # call a machine a hub on the strength of holding `10.66.0.1`, which is
        # true of a machine whose peer table this process cannot find. Minting
        # is the narrower question — this process has to be able to edit the
        # peer table, not merely be on the mesh it describes.
        print("this Mac cannot mint a mesh peer, so it cannot hand a machine "
              "the tunnel that joining means — the code would fail on the "
              f"other Mac, not here. Expected the peer table at "
              f"{tunnel.HUB_CONF}. If this Mac does run the mesh, that is the "
              "wrong directory: set WG_PEER_DIR to the one its daemons drive. "
              "Otherwise run `install_hub.sh` to make this Mac a hub.",
              file=sys.stderr)
        return 1
    ttl = args.ttl if args.ttl is not None else ADOPT_TTL
    row = enrolment.mint_code(args.name, created_by="", ttl=ttl,
                              kind=enrolment.KIND_HOST)
    port = getattr(args, "port", None) or addresses.DEFAULT_PORT
    found = addresses.reachable(port)

    if getattr(args, "offline", False):
        return _adopt_offline(args.name, row, port,
                              as_json=getattr(args, "json", False))

    if getattr(args, "json", False):
        import json
        print(json.dumps({"name": row["name"], "code": row["code"],
                          "kind": row["kind"], "expires_in": row["expires_in"],
                          "port": port, "addresses": found}))
        return 0

    mins = row["expires_in"] // 60
    print(f"\n    {row['code']}\n")
    print(f"for the machine you are adopting as {row['name']} — good for "
          f"{mins} minute{'' if mins == 1 else 's'}.")
    # Two addresses, each under the condition that picks it — and the
    # condition is the tunnel, never the network.
    #
    # This printed the LAN address alone under a flat "it has to be on this
    # network to redeem". That claim is false, and false in the direction that
    # costs the most: it is the reader who is NOT on this network who opens
    # this, and they were handed the one address that cannot answer them plus
    # a sentence blaming their location.
    #
    # Redemption applies no locational rule at all. `tunnel.issue` drops it on
    # purpose — the code is the authorization that a LAN source address merely
    # stands in for — so a machine already holding the tunnel redeems from
    # anywhere on earth, and that is the steady state of every machine after
    # its first day. Only a machine with nothing on it yet has to be here once,
    # because the first tunnel is exactly what `attach` hands back.
    #
    # `.local` still stays out: it needs the same LAN as the numeric address
    # while resolving less reliably on it, so it is never right when the
    # number is available and never available when it is not.
    lan = next((a for a in found if a["kind"] == "lan"), None)
    mesh = next((a for a in found if a["kind"] == "mesh"), None)
    if mesh or lan:
        print("\nOn that Mac, with jStack installed:\n")
        print(f"    jstack-host attach {row['code']} "
              f"--parent {(mesh or lan)['url']}")
        if mesh:
            print("\nThat address is this hub on the mesh. It reaches here "
                  "from anywhere in the world,\nand it is the answer for any "
                  "Mac that has been on this mesh even once.")
            if lan:
                # Named, not printed as a command. A machine that has never
                # held the tunnel has no route to 10.66 — the tunnel is what
                # creates that route — so it does need a LAN address once.
                # But that is a setup-in-person case, and putting its address
                # beside the real one is what taught the reader to treat the
                # first line as a guess and work down the list.
                print("\nOnly a Mac that has NEVER been on this mesh needs a "
                      "different address, and\nit has to be on this network "
                      "for it — `jstack-host where` prints that one.")
    else:
        # Loopback only, and no mesh to fall back to: nothing another machine
        # can redeem against exists. A real stop, not a prompt to guess.
        print("\n  This Mac has no address another machine can redeem against "
              "— only loopback,\n  and it runs no mesh. Put it on a real "
              "network (check `jstack-host where`),\n  then mint a new code.")
        return 1
    print("\nThat Mac joins this mesh and hands back a grant, so every device "
          "already paired\nhere gets into it without a second code.")
    return 0


def _cmd_reachback(args) -> int:
    """Read or set whether an adopted machine holds a credential back to here.

    Adopting establishes trust in both directions in one request, and only one
    of them is ever the thing somebody meant. The grant this hub receives is
    the point — it is what lets a phone paired here reach the office Mac. The
    token that machine receives is the side effect, and on hardware whose disk
    this hub's owner cannot vouch for it is the half worth refusing.

    Turning it off does not touch machines already adopted unless asked:
    `--existing` is a separate word because it ends access that is live right
    now, and a flag that quietly cut a running machine off would be the same
    class of surprise this setting exists to prevent.
    """
    _adopt(args)
    from . import hub_prefs

    if args.state is None:
        on = hub_prefs.get("leaf_reachback")
        print(f"reachback: {'on' if on else 'off'}")
        print("\nMachines this hub adopts " + (
            "hold a credential back to this hub — they can drive it as an "
            "ordinary device." if on else
            "get no working credential back to this hub. Adoption still works "
            "in the direction\nyou asked for: this hub administers them, and "
            "devices paired here reach them."))
        return 0

    want = args.state == "on"
    hub_prefs.set("leaf_reachback", want)
    print(f"reachback: {'on' if want else 'off'}")

    if want:
        print("\nMachines adopted from now on will hold a credential back to "
              "this hub.\nMachines whose credential was already revoked do "
              "NOT get it back — their row is\ndead, and only attaching again "
              "mints a live one.")
        return 0

    print("\nMachines adopted from now on get no working credential back here.")
    if not args.existing:
        print("Machines already adopted keep theirs — re-run with `--existing` "
              "to revoke those\ntoo, which ends access they are using right "
              "now.")
        return 0

    revoked = hub_prefs.revoke_existing_reachback()
    if revoked:
        print(f"\nRevoked what {len(revoked)} already-adopted machine"
              f"{'' if len(revoked) == 1 else 's'} held back to this hub:")
        for name in revoked:
            print(f"    {name}")
        print("\nEach can still be administered from here. To give one its "
              "credential back it\nhas to attach again.")
    else:
        print("\nNo already-adopted machine held a live credential back here.")
    return 0


def _cmd_detach(args) -> int:
    """Leave the parent hub — the reverse of `attach`, at both ends.

    Prints every step by name rather than one verdict, because the steps fail
    independently and mean different things: the grants are the authority, the
    parent calls are best-effort courtesy over a mesh that is coming down, and
    the tunnel is the transport. A person who ran this needs to know which of
    those did not happen, in the words of the thing they would have to go fix.
    """
    _adopt(args)
    from . import detach_parent, hostenv, mode
    try:
        result = detach_parent.detach(
            host_key=hostenv.host_id(),
            keep_tunnel=getattr(args, "keep_tunnel", False),
            tell_parent=not getattr(args, "local_only", False))
    except detach_parent.DetachError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    m = mode.current()
    if getattr(args, "json", False):
        import json
        print(json.dumps({**result, "mode": m}))
        return 0

    for step in result["steps"]:
        print(f"  {'✓' if step['ok'] else '✗'}  {step['note']}")
    print(f"\nmode  {m['mode']}")
    print(f"      {m['note']}")
    if m["mode"] == "managed":
        # The steps may all have passed and the machine still read as attached —
        # a second leaf install, a plist somewhere else. Say it rather than let
        # the mode line be the only tell, the same way `attach` does.
        print("\n  This machine still reads as managed — something is still "
              "dialling out. Check `jstack-host doctor` and "
              "/Library/LaunchDaemons/com.jremote.leaf*.", file=sys.stderr)
        return 1
    return 0


def _cmd_leaves(args) -> int:
    """The machines this hub adopted, and whether it can let devices into them.

    Two facts per row and they are genuinely different: the registry row is what
    every device sees (the tile), and the grant is whether asking for access
    works. A machine with a tile and no grant is exactly the state that looks
    fine on a phone and fails when tapped, so it is printed, not inferred.
    """
    _adopt(args)
    from . import grants
    from .store import get_store
    rows = get_store().list_hosts()
    holdings = {h["host_key"]: h for h in grants.holdings()}

    if getattr(args, "json", False):
        import json
        print(json.dumps([
            {**r, "delegated": bool(holdings.get(r["key"], {}).get("revoked_at") is None
                                    and r["key"] in holdings)}
            for r in rows]))
        return 0

    if not rows:
        print("no machines adopted — `jstack-host adopt <name>` mints a code "
              "for one.")
        return 0
    print(f"{'MACHINE':<20} {'ADDRESS':<18} {'ACCESS':<10} ADOPTED")
    for r in rows:
        held = holdings.get(r["key"])
        access = ("delegated" if held and held["revoked_at"] is None
                  else "pair-by-hand")
        addr = f"{r['address'] or '—'}:{r['port']}" if r["address"] else "—"
        print(f"{(r['name'] or r['key'])[:19]:<20} {addr:<18} {access:<10} "
              f"{grants.stamp(r['enrolled_at'])}")
    return 0


def _cmd_token(args) -> int:
    _adopt(args)
    path = hostenv.token_path()
    try:
        print(path.read_text().strip())
    except OSError:
        print(f"no token at {path} — run `jstack-host install` first.",
              file=sys.stderr)
        return 1
    return 0


def _cmd_where(args) -> int:
    """Every path this host resolves, so a support question is one paste.

    The seam answers these; printing them is how someone finds out that
    `--state-dir` took effect, or which credentials directory a missing APNs
    key is missing from.
    """
    _adopt(args)
    print(f"name         {hostenv.host_name()}")
    print(f"host id      {hostenv.host_id()}")
    print(f"profile      {hostenv.profile().name}")
    print(f"package      {hostenv.package_root()}")
    print(f"state        {hostenv.state_dir()}")
    print(f"token        {hostenv.token_path()}")
    print(f"credentials  {hostenv.credentials_dir()}")
    print(f"agents       {hostenv.instance_root()}")
    print(f"scheduler    {hostenv.scheduler_dir()}")
    return 0


def _cmd_mode(args) -> int:
    """Which of local / open / managed this host is — the question a person
    asks before they know whether a device off this network can reach it.

    The same verdict the menu bar shows, on a terminal: the mode, whether it is
    live right now, and the one line that says what that mode does and does not
    prove. `_adopt` first, for the same reason `where` does — a host installed
    with `--state-dir` keeps its tunnel state somewhere this shell would
    otherwise not look."""
    _adopt(args)
    from . import mode
    m = mode.current()
    live = "" if m["live"] else "  (not live)"
    print(f"mode  {m['mode']}{live}")
    print(f"      {m['note']}")
    return 0


def _cmd_open(args) -> int:
    """Guide this Mac into open mode, and prove the forward before claiming it.

    Open mode is a host holding its OWN way in from outside — a UDP port forward
    on the router to this Mac's WireGuard endpoint. This walks that: it names the
    exact one-line forward to enter, asks the router to make it automatically
    (NAT-PMP) where it can, and finishes with the honest verification — which is
    not a scan but an observation, because a WireGuard endpoint is silent to any
    packet without a valid key and cannot be probed from outside. So the last
    line asks the one thing that DOES prove it: bring a paired device onto
    cellular and open the app; the handshake that lands is the proof.

    `--verify` skips the setup and reports only that observation — the command to
    run after producing the evidence. `_adopt` first, like every command that
    reads this host's tunnel state.

    Setting the endpoint stays advisory on purpose: this prints the
    `install_hub.sh --endpoint` line to run rather than editing the live tunnel
    behind a `mode`-shaped command. Turning a declaration into a persisted config
    is a deliberate step, not a side effect of asking about reachability.
    """
    _adopt(args)
    from . import open_mode
    if getattr(args, "verify", False):
        v = open_mode.verify()
        if getattr(args, "json", False):
            import json
            print(json.dumps(v))
            return 0
        mark = "verified" if v["verified"] else "not yet verified"
        print(f"off-network reachability: {mark}")
        print(f"  {v['note']}")
        return 0

    try:
        g = open_mode.guide()
    except open_mode.OpenModeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        import json
        print(json.dumps(g))
        return 0

    fwd = g["forward"]
    print("To make this Mac reachable off-network, forward one UDP port on your "
          "router:\n")
    print(f"    {fwd['line']}\n")
    print(f"    protocol       UDP")
    print(f"    external port  {fwd['external_port']}")
    print(f"    to this Mac    {fwd['internal_ip']}:{fwd['internal_port']}")

    mapping = g["mapping"]
    if mapping["ok"]:
        print(f"\nThe router accepted this automatically — {mapping['detail']}.")
    else:
        print(f"\nDo it by hand in the router's admin page — {mapping['detail']}.")

    if g["endpoint"]:
        print(f"\nYour public endpoint is {g['endpoint']}. Record it so devices "
              "off-network can dial in:\n")
        print(f"    sudo bash install_hub.sh --endpoint {g['endpoint']}")
    elif g["public_ip"]:
        print(f"\nYour public endpoint is {g['public_ip']}:{g['wg_port']}.")
    else:
        print("\nThe router did not reveal its public address over NAT-PMP — "
              "find it in the router's status page, then the endpoint is "
              f"<that address>:{g['wg_port']}.")

    v = g["verification"]
    print("\nReachability is not something this Mac can prove by scanning — a "
          "WireGuard endpoint stays silent to any packet without a key. The one "
          "proof is a real connection from outside:\n")
    if v["verified"]:
        print(f"  ✓ {v['note']}")
    else:
        print(f"  {v['note']}")
        print("\n  When you have, run `jstack-host open --verify`.")
    return 0


#: What this build can do, one name per line. A capability is listed here the
#: moment the code behind it lands, and never removed — a reader across the
#: mesh uses the absence of a name to mean "too old for this", so dropping one
#: would tell every newer hub that a current Mac had gone backwards.
CAPABILITIES = (
    # `attach` sends a grant back, so the hub it joins can mint devices onto
    # this Mac without anybody typing a second code (grants.py, attach_parent).
    "delegated-minting",
)


def _cmd_capabilities(args) -> int:
    """Name what this build can do, for a caller deciding whether to proceed.

    Exists because `version` cannot answer that question: it prints the
    packaging version, which has been `0.1.0` since the first commit and is
    identical on a build from today and one from three weeks ago. The offline
    joiner asked only whether `jstack-host` was *installed*, so a Mac running a
    host older than delegated minting attached cleanly, handed back no grant,
    and became a machine the hub could never mint onto again — with every step
    reporting success.

    A build too old to delegate is also too old to have this subcommand, so it
    fails the probe by exiting non-zero on an unknown choice. That is the whole
    mechanism: absence is the answer, and it needs no cooperation from the old
    build.
    """
    for name in CAPABILITIES:
        print(name)
    return 0


def _cmd_version(args) -> int:
    from importlib.metadata import PackageNotFoundError, version
    try:
        print(version("jstack-host"))
    except PackageNotFoundError:
        # Running from a checkout that was never pip-installed. Not an error —
        # `python3 -m jstack_host.cli` is a legitimate way to drive this.
        print("unknown (not installed as a package)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="jstack-host",
        description="The jStack host: the API a phone, an iPad or another Mac "
                    "reaches this machine through.")
    ap.add_argument("--label", default=install_host.LABEL,
                    help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def _serving_args(p, *, bind_default, bind_help):
        p.add_argument("--port", type=int, default=install_host.DEFAULT_PORT)
        p.add_argument("--bind", default=bind_default, help=bind_help)
        p.add_argument("--state-dir", default=None,
                       help="where this host keeps its state "
                            "(default: ~/.local/state/jremote)")

    p = sub.add_parser("install", help="install the host as a user LaunchAgent")
    _serving_args(p, bind_default=install_host.DEFAULT_BIND,
                  bind_help="bind address (default 0.0.0.0 — a host is reached "
                            "over a tunnel or the LAN, and one bound to "
                            "127.0.0.1 is one only this Mac can see)")
    p.add_argument("--force", action="store_true",
                   help="install even if something else already answers on the port")
    p.set_defaults(fn=lambda a: install_host.install(
        port=a.port, bind=a.bind, label=a.label, force=a.force,
        state_dir=_path(a.state_dir)))

    p = sub.add_parser("uninstall", help="remove the LaunchAgent (state and token stay)")
    p.set_defaults(fn=lambda a: install_host.uninstall(label=a.label))

    p = sub.add_parser("status", help="is the host installed, loaded and answering")
    # No default: the agent's own port is the answer, and a default here is
    # what made `status` report on 9090 for a host installed on 9099.
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--state-dir", default=None)
    p.set_defaults(fn=lambda a: (_adopt(a),
                                 install_host.status(port=a.port, label=a.label))[1])

    p = sub.add_parser("doctor", help="grade every dependency this host needs")
    p.add_argument("--state-dir", default=None)
    p.set_defaults(fn=lambda a: (_adopt(a), _doctor())[1])

    p = sub.add_parser("serve", help="run the host in this terminal (no LaunchAgent)")
    _serving_args(p, bind_default="127.0.0.1",
                  bind_help="bind address (default 127.0.0.1 — `serve` is for "
                            "a foreground run you are watching; `install` is "
                            "the one that binds outward)")
    p.set_defaults(fn=lambda a: server.main(
        ["--host", a.bind, "--port", str(a.port)]
        + (["--state-dir", a.state_dir] if a.state_dir else [])))

    p = sub.add_parser("pair", help="mint an enrolment code for a device")
    p.add_argument("name", nargs="?", default="My device",
                   help="what to call the device in this host's device list")
    p.add_argument("--ttl", type=int, default=600,
                   help="seconds the code stays good (default 600)")
    p.add_argument("--open", action="store_true",
                   help="hand the code to the app on this Mac instead of "
                        "printing it — it pairs itself and opens")
    p.add_argument("--json", action="store_true",
                   help="print the code, addresses and pair link as JSON — "
                        "what the menu bar dialog draws its QR from")
    p.add_argument("--state-dir", default=None)
    p.set_defaults(fn=_cmd_pair)

    p = sub.add_parser("attach",
                       help="join a parent hub's mesh — make this Mac a managed hub")
    p.add_argument("code", help="the host code minted on the parent "
                                "(kind=host)")
    p.add_argument("--parent", required=True,
                   help="the parent host's address, e.g. http://studio.local:9090")
    p.add_argument("--port", type=int, default=install_host.DEFAULT_PORT,
                   help="the port THIS Mac serves on, recorded on the parent "
                        f"(default {install_host.DEFAULT_PORT})")
    p.add_argument("--json", action="store_true",
                   help="print the outcome and resulting mode as JSON")
    p.add_argument("--state-dir", default=None)
    p.set_defaults(fn=_cmd_attach)

    p = sub.add_parser("adopt",
                       help="mint a code that joins another Mac to this hub")
    p.add_argument("name", help="what to call that machine in this hub's grid")
    p.add_argument("--offline", action="store_true",
                   help="write a folder to carry to a Mac that cannot reach "
                        "this hub yet (off-LAN, never on the mesh)")
    p.add_argument("--ttl", type=int, default=None,
                   help="seconds the code stays good (default 600)")
    p.add_argument("--port", type=int, default=None,
                   help="the port THIS Mac serves on, for the address printed")
    p.add_argument("--json", action="store_true",
                   help="print the code and addresses as JSON")
    p.add_argument("--state-dir", default=None)
    p.set_defaults(fn=_cmd_adopt)

    p = sub.add_parser("reachback",
                       help="may machines this hub adopts reach back INTO it")
    p.add_argument("state", nargs="?", choices=("on", "off"),
                   help="omit to read the current setting")
    p.add_argument("--existing", action="store_true",
                   help="with `off`, also revoke what already-adopted "
                        "machines hold — this ends live access")
    p.add_argument("--state-dir", default=None)
    p.set_defaults(fn=_cmd_reachback)

    p = sub.add_parser("detach",
                       help="leave the parent hub — the reverse of attach")
    p.add_argument("--keep-tunnel", action="store_true",
                   help="stay on the parent's mesh, but stop being "
                        "administered from it (revokes the grants only)")
    p.add_argument("--local-only", action="store_true",
                   help="do not tell the parent — leave its tile and this "
                        "machine's credential there for someone to clean up")
    p.add_argument("--json", action="store_true",
                   help="print every step and the resulting mode as JSON")
    p.add_argument("--state-dir", default=None)
    p.set_defaults(fn=_cmd_detach)

    p = sub.add_parser("leaves",
                       help="the machines this hub adopted, and their access")
    p.add_argument("--json", action="store_true")
    p.add_argument("--state-dir", default=None)
    p.set_defaults(fn=_cmd_leaves)

    p = sub.add_parser("welcome",
                       help="open the app on a session that checks this install")
    p.add_argument("--agent", default="",
                   help="which agent gets the session (default: the first one "
                        "on this host)")
    p.add_argument("--state-dir", default=None)
    p.set_defaults(fn=_cmd_welcome)

    p = sub.add_parser("token", help="print this host's bearer token")
    p.add_argument("--state-dir", default=None)
    p.set_defaults(fn=_cmd_token)

    p = sub.add_parser("where", help="every path this host resolves")
    p.add_argument("--state-dir", default=None)
    p.set_defaults(fn=_cmd_where)

    p = sub.add_parser("mode", help="is this host local, open or managed")
    p.add_argument("--state-dir", default=None)
    p.set_defaults(fn=_cmd_mode)

    p = sub.add_parser("open",
                       help="guide this Mac into open mode and prove it's reachable")
    p.add_argument("--verify", action="store_true",
                   help="skip the setup; only report whether an off-network "
                        "device has been observed reaching this Mac")
    p.add_argument("--json", action="store_true",
                   help="print the guide (or --verify result) as JSON")
    p.add_argument("--state-dir", default=None)
    p.set_defaults(fn=_cmd_open)

    p = sub.add_parser("capabilities",
                       help="what this build can do, one name per line")
    p.set_defaults(fn=_cmd_capabilities)

    p = sub.add_parser("version", help="the installed package version")
    p.set_defaults(fn=_cmd_version)
    return ap


def _path(raw):
    from pathlib import Path
    return Path(raw).expanduser() if raw else None


def _doctor() -> int:
    from . import doctor
    return doctor.report()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
