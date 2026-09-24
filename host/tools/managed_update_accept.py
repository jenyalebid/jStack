"""One unattended acceptance run over disposable Macs, writing eight receipts.

The subject is a commit, not a package. Every Mac here fetches the ref under
test from the public repo and builds the Hub itself, which is what install and
update now mean — so the runner hands a guest nothing but the closed-source
Mac app, and then checks that what the machine ends up running is that commit.

Every journey drives the shipped code: the real installer, the real supervisor
under launchd, the real authenticated routes, real bundle replacement and the
real fault injector. Nothing is simulated, and nothing is asserted about a
machine this run did not ask.

The runner cannot mark a journey passed. It checks an expectation and then
records what it observed; `jstack_host.acceptance` writes the receipt from
those observations and refuses to call an unobserved journey anything but
incomplete. A journey the plan cannot support — two leaves that do not exist,
a leaf that cannot be taken off the LAN — is recorded as skipped with its reason, and a
skip keeps promotion closed exactly like a failure.

Disposable guests only. The plan names the VMs; every guest is checked for the
fixture account and candidate-test trust before it is touched.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from jstack_host.acceptance import HarnessFault

GUEST_HOME = "/Users/admin"
GUEST_TOOL = GUEST_HOME + "/update-vm.py"
GUEST_FAULT = GUEST_HOME + "/update-fault.py"
GUEST_PYTHON = "/Applications/jStack Hub.app/Contents/MacOS/JStackPython"
GUEST_HOST_CLI = GUEST_HOME + "/.local/bin/jstack-host"
GUEST_TMUX = "/Applications/jStack Hub.app/Contents/MacOS/tmux"
# The guest-side halves of this runner, shipped beside it. A reset guest has
# neither, so the runner lays them down itself before any journey uses one.
GUEST_TOOLS = {GUEST_TOOL: Path(__file__).resolve().parent / "managed_update_vm.py",
               GUEST_FAULT: Path(__file__).resolve().parent / "managed_update_fault.py"}
#: How long a spawned session gets to answer on its own before the runner looks
#: at its pane. Releases before ca7cf89 left the bypass warning on screen (the
#: watcher ran an unquoted tmux path), so a session on such a PRIOR never
#: starts; answering it is the runner standing in for that fixed watcher, and
#: the receipt says so. A CANDIDATE session is never helped.
NUDGE_AFTER = 30
API_BOOT = 180
SETTLE = 8


class AcceptanceFailure(RuntimeError):
    """An expectation the commit did not meet. It ends one journey, not the run."""


def expect(condition, message: str) -> None:
    if not condition:
        raise AcceptanceFailure(message)


def git(checkout: Path, *argv: str, timeout: int = 300) -> str:
    done = subprocess.run(["git", "-C", str(checkout), *argv],
                          capture_output=True, text=True, timeout=timeout)
    if done.returncode:
        raise AcceptanceFailure("git " + " ".join(argv) + ": " + done.stderr.strip()[-400:])
    return done.stdout


class Build:
    """The commit under test: a ref, the sha it resolves to, and what that
    commit declares it ships.

    Nothing here is signed and nothing but the Mac app is carried to a guest.
    Each machine clones this ref and builds the Hub with its own key, so two
    honest installs of one commit differ byte for byte and the only thing they
    can be held to is the commit itself.
    """

    def __init__(self, repo_url: str, ref: str, *, checkout: Path, client: Path | None = None):
        self.repo_url = repo_url.rstrip("/")
        self.ref = ref
        self.checkout = Path(checkout).expanduser()
        self.client = Path(client).expanduser() if client else None
        listing = subprocess.run(["git", "ls-remote", self.repo_url, "refs/heads/" + ref],
                                 capture_output=True, text=True, timeout=120)
        head = listing.stdout.split("\t", 1)[0].strip()
        if listing.returncode or not re.fullmatch(r"[0-9a-f]{40}", head):
            raise AcceptanceFailure(
                f"{self.repo_url} has no branch {ref}: {(listing.stderr or listing.stdout)[-300:]}")
        self.sha = head
        # The version the guests will install is the one inside the commit,
        # never the one in this checkout's working tree.
        git(self.checkout, "fetch", "--quiet", self.repo_url, ref)
        declared = git(self.checkout, "show",
                       self.sha + ":plugins/jstack/.claude-plugin/plugin.json")
        self.stack_version = str(json.loads(declared)["version"])
        self.client_version = bundle_build(self.client) if self.client else ""

    @property
    def slug(self) -> str:
        return f"{self.ref}@{self.sha[:8]}"

    @property
    def raw_url(self) -> str:
        """Where the README tells a Mac to fetch the installer from."""
        owner = self.repo_url.removesuffix(".git").split("github.com/", 1)[-1]
        return f"https://raw.githubusercontent.com/{owner}/{self.sha}/install.sh"

    def version(self, component: str) -> str:
        if component == "stack":
            return self.stack_version
        if component == "client":
            return self.client_version
        raise AcceptanceFailure(f"a build does not declare a {component} version up front")


def bundle_build(app: Path) -> str:
    """CFBundleVersion of an app bundle or of the zip holding one."""
    if app.is_dir():
        plist = app / "Contents/Info.plist"
        return subprocess.run(["/usr/bin/defaults", "read", str(plist), "CFBundleVersion"],
                              capture_output=True, text=True, timeout=60).stdout.strip()
    raise AcceptanceFailure(f"--client must name an app bundle: {app}")


class Guest:
    """One disposable Mac, reached the way the lab reaches it: vm.sh and ssh."""

    def __init__(self, name: str, tool: Path, *, run=subprocess.run):
        self.name, self.tool, self._run = name, Path(tool), run

    def vm(self, *argv: str, timeout: int = 900) -> str:
        done = self._run([str(self.tool), *argv], capture_output=True, text=True, timeout=timeout)
        if done.returncode:
            raise AcceptanceFailure(f"{self.name}: vm.sh {argv[0]} failed: "
                                    + (done.stderr or done.stdout).strip()[-800:])
        return done.stdout

    def sh(self, command: str, *, timeout: int = 600) -> str:
        return self.vm("ssh", self.name, command, timeout=timeout)

    def copy(self, source: Path, destination: str) -> None:
        self.vm("cp", self.name, str(source), destination)

    def absent(self, paths: list[str]) -> list[str]:
        """Which of these guest paths do not exist, asked in one round trip."""
        if not paths:
            return []
        listed = " ".join(shlex.quote(path) for path in paths)
        answer = self.sh(f"for p in {listed}; do [ -e \"$p\" ] || printf '%s\\n' \"$p\"; done")
        return [line for line in answer.splitlines() if line in paths]

    def start(self) -> str:
        """GUI, because the menu bar and the client app are part of the proof."""
        booted = self.vm("gui", self.name, timeout=600).strip()
        self.await_api()
        return booted

    def await_api(self, timeout: int = API_BOOT) -> str:
        """The guest's own host API answering, or a failure that names it.

        `gui` returns on sshd; the host daemon is a login agent that comes up
        tens of seconds later (27s measured on acc-leaf1), and a call landing
        in that gap reads as a dead daemon. A guest with no installed host has
        no API to wait for; a guest whose API never comes up is a real finding.
        """
        script = (
            "cfg=~/.local/state/jremote/updates/config.json; "
            "[ -f \"$cfg\" ] || { echo no-host; exit 0; }; "
            "url=$(grep -o '\"local_url\": *\"[^\"]*\"' \"$cfg\" | cut -d'\"' -f4); "
            f"for i in $(seq 1 {int(timeout)}); do "
            "code=$(curl -s -o /dev/null -m 2 -w '%{http_code}' \"$url/api/jremote/v1/host\"); "
            "[ \"$code\" != 000 ] && { echo \"up $code ${i}s\"; exit 0; }; sleep 1; done; "
            "echo down; exit 3")
        try:
            return self.sh(script, timeout=timeout + 60).strip()
        except AcceptanceFailure as exc:
            raise AcceptanceFailure(
                f"{self.name}: booted, but its host API never answered within {timeout}s") from exc

    def stop(self) -> None:
        self.vm("stop", self.name, timeout=300)

    def tool_call(self, *argv: str, timeout: int = 600) -> dict:
        command = " ".join(shlex.quote(part) for part in [GUEST_PYTHON, GUEST_TOOL, *argv])
        output = self.sh(command, timeout=timeout)
        try:
            return json.loads(output[output.index("{"):])
        except ValueError as exc:
            raise AcceptanceFailure(f"{self.name}: unreadable fixture answer: {output[-400:]}") from exc

    def background(self, command: str) -> subprocess.Popen:
        """A guest command whose output is read while something else happens."""
        return subprocess.Popen([str(self.tool), "ssh", self.name, command],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def probe(self) -> dict:
        return self.tool_call("probe", timeout=180)

    def call(self, path: str, body: dict | None = None, *, timeout: int = 120) -> dict:
        """One authenticated request on the guest's own API, refusals included."""
        argv = ["call", "--path", path, "--timeout", str(timeout)]
        if body is not None:
            argv += ["--body", json.dumps(body)]
        return self.tool_call(*argv, timeout=timeout + 60)

    def queue(self, target: str, request: str) -> dict:
        return self.tool_call("queue", "--target", target, "--request", request)

    def inventory(self) -> dict:
        return self.tool_call("inventory", timeout=120)

    def row(self, machine: str) -> dict:
        for row in self.inventory()["machines"]:
            if row["machine"] == machine:
                return row
        raise AcceptanceFailure(f"{self.name}: no inventory row for {machine}")

    def host_id(self) -> str:
        return self.probe()["host_id"]

    def installed(self) -> dict:
        """What is actually running here, as the product's own observer sees it."""
        observed = self.probe()["observed"]
        return {"build": observed.get("release"),
                "sha": observed.get("host_source", {}).get("sha"),
                "dirty": observed.get("host_source", {}).get("dirty"),
                "client": observed["components"]["client"]["installed"],
                "menubar": observed["components"]["menubar"]["installed"],
                "menubar_pids": observed["components"]["menubar"]["running_pids"],
                "client_pids": observed["components"]["client"]["running_pids"],
                "plugins": {name: value.get("version") if isinstance(value, dict) else value
                            for name, value in observed["components"].get("plugins", {}).items()},
                "updater": observed.get("updater_source", {}).get("sha"),
                "verified": observed.get("verified")}

    def wait_for(self, state: str, machine: str, *, timeout: int = 1800, poll: int = 10) -> dict:
        """Wait on the hub's own row for a machine. A leaf's claim is not a state."""
        deadline = time.monotonic() + timeout
        last, refused = {}, None
        while time.monotonic() < deadline:
            # A hub updating itself restarts its own API mid-wait; a refused
            # poll during that window is the update happening, not a verdict.
            # The deadline decides — only an answered poll can fail the wait.
            try:
                last = self.row(machine)
            except AcceptanceFailure as exc:
                refused = exc
                time.sleep(poll)
                continue
            refused = None
            if last["state"] == state:
                return last
            if last["state"] in {"failed", "rolled_back", "cancelled"} and last["state"] != state:
                raise AcceptanceFailure(
                    f"{machine} settled on {last['state']} waiting for {state}: "
                    + str((last.get("job") or {}).get("detail", "")))
            time.sleep(poll)
        if refused is not None:
            raise AcceptanceFailure(
                f"{machine} never reached {state}; the hub stopped answering: {refused}")
        raise AcceptanceFailure(f"{machine} never reached {state}; last was {last.get('state')}")


#: The subnet the lab's guests share. `vm.sh` puts a guest there only when it
#: boots it with softnet, so a guest that was already running when the runner
#: arrived can be on the host's default NAT instead — reachable from this Mac,
#: invisible to every other guest, and the hub's own address is what the adopt
#: script looks for. Overridden per plan with `lab_network`.
LAB_NETWORK = "192.168.2."


class Fleet:
    """The disposable machines this run may touch, and nothing else."""

    def __init__(self, plan: dict, *, run=subprocess.run):
        tool = Path(plan["vm_tool"]).expanduser()
        self.plan = plan
        self.hub = Guest(plan["hub"], tool, run=run)
        self.leaves = [Guest(name, tool, run=run) for name in plan.get("leaves", [])]
        self.fresh = Guest(plan["fresh"], tool, run=run) if plan.get("fresh") else None
        self.off_lan = plan.get("off_lan")
        self.slots = int(plan.get("vm_slots") or 0)
        self.fixtures = list(plan.get("fixtures") or [])
        self.provision = plan.get("provision")
        self.network = plan.get("lab_network") or LAB_NETWORK
        self._run = run
        self._ids: dict[str, str] = {}
        self.build: Build | None = None
        self.prior: Build | None = None
        #: Leaves owing a re-pin because the hub built since they last adopted.
        #: Paid when the leaf is next booted, not at build time: a parked guest
        #: cannot be re-adopted, and a two-slot host parks most of them.
        self.owed: set[str] = set()
        #: What the hub's own build of a ref came out as. A build id folds in
        #: the client and the dependency set the *building* machine holds, so
        #: the hub's id for a commit and a fresh Mac's id for the same commit
        #: are not required to match; the commit is what both are held to.
        self.offered: dict[str, str] = {}

    def guests(self) -> list[Guest]:
        return [self.hub, *self.leaves, *([self.fresh] if self.fresh else [])]

    def cast(self, *wanted: Guest | None) -> None:
        """Fit one journey's cast into the host's concurrent-VM slots.

        Apple's Virtualization framework boots two guests at a time, and the
        update contract never asked for simultaneous uptime — a parked Mac
        reports pending/offline and catches up on the same queued job, which
        is itself contract behavior (offline_catchup). A plan that declares
        vm_slots gets every guest outside the cast stopped before the cast is
        booted; a plan without it keeps every guest running, as before.
        """
        cast = [guest for guest in wanted if guest is not None]
        if self.slots:
            if len(cast) > self.slots:
                raise AcceptanceFailure(
                    f"this journey needs {len(cast)} live guests; the host has {self.slots} slots")
            names = {guest.name for guest in cast}
            for guest in self.guests():
                if guest.name not in names:
                    guest.stop()
        for guest in cast:
            guest.start()
        self.prepare(*cast)
        for guest in cast:
            self.readopt(guest)

    def prepare(self, *guests: Guest) -> None:
        """Put back what `vm.sh reset` takes away before a journey leans on it.

        A reset clones a pristine Mac, which carries none of the lab's
        fixtures. The runner's own guest tools it copies every time. The
        plan's `fixtures` (absolute guest paths — the adopt script, the lab's
        env agent) are re-laid by its `provision` host command, `{guest}`
        standing for the guest's name, when any is absent. One still absent
        after that is a harness fault: the journey never reached the product.
        """
        for guest in guests:
            try:
                for remote, local in GUEST_TOOLS.items():
                    guest.copy(local, remote)
                missing = guest.absent(self.fixtures)
                if missing and self.provision:
                    command = [part.replace("{guest}", guest.name)
                               for part in shlex.split(self.provision)]
                    done = self._run(command, capture_output=True, text=True, timeout=1800)
                    if done.returncode:
                        raise HarnessFault(f"{guest.name}: provisioning failed: "
                                           + (done.stderr or done.stdout).strip()[-800:])
                    missing = guest.absent(self.fixtures)
            except AcceptanceFailure as exc:
                raise HarnessFault(f"{guest.name}: could not stage fixtures: {exc}") from exc
            if missing:
                raise HarnessFault(f"fixture missing: {guest.name}:{missing[0]}")
            self.on_lab_network(guest)

    def on_lab_network(self, guest: Guest) -> None:
        """Every guest on one subnet, asked before anything leans on it.

        A guest booted outside the lab keeps the default NAT, where no other
        guest can reach it. What that costs is not an error: the adopt script
        resolves the hub, connects to an address nothing answers on, and sits
        there — this run spent seven minutes in a silent ssh before anyone
        asked the one question that names it.
        """
        try:
            addresses = guest.sh("/sbin/ifconfig | awk '/inet /{print $2}'").split()
        except AcceptanceFailure as exc:
            raise HarnessFault(f"{guest.name}: could not read its addresses: {exc}") from exc
        if not any(address.startswith(self.network) for address in addresses):
            raise HarnessFault(
                f"{guest.name} is not on the lab network {self.network}0/24 — it holds "
                + (", ".join(a for a in addresses if a != "127.0.0.1") or "no address")
                + ". It was booted outside the lab: stop it and let this runner boot it.")

    def machine(self, guest: Guest) -> str:
        if guest.name not in self._ids:
            self._ids[guest.name] = guest.host_id()
        return self._ids[guest.name]

    def offer(self, build: Build) -> str:
        """Make the fixture hub build a ref and offer the result to its fleet.

        This is the product's own door — `updates build` is the only call that
        builds — so what the leaves install is what a real hub would serve
        them. A second build on a hub that has already adopted machines
        rotates the key those machines pinned (#144), so the leaves are
        re-adopted after one, and the receipt records that it happened.
        """
        self.hub.sh(host_cli(f"updates channel {shlex.quote(build.ref)}"), timeout=120)
        adopted = bool(self.leaves)
        despite = "JSTACK_BUILD_DESPITE_LEAVES=1 " if adopted else ""
        output = self.hub.sh(despite + host_cli(f"updates build --ref {shlex.quote(build.ref)}"),
                             timeout=3600)
        try:
            built = json.loads(output[output.index("{"):])
        except ValueError as exc:
            raise AcceptanceFailure(
                f"the hub did not report a build of {build.slug}: {output[-500:]}") from exc
        self.offered[build.ref] = built["release"]
        self.build = build
        if adopted:
            self.owed = {leaf.name for leaf in self.leaves}
        return built["release"]

    def readopt(self, guest: Guest) -> None:
        """Re-pin a leaf on the key the hub's newest build minted (#144)."""
        if guest.name not in self.owed:
            return
        expect(self.plan.get("adopt_command"),
               "re-adopting a leaf after a hub build needs 'adopt_command' in the plan")
        guest.sh(self.plan["adopt_command"], timeout=900)
        self.owed.discard(guest.name)
        time.sleep(SETTLE)


def request_id(prefix: str) -> str:
    return f"accept-{prefix}-{uuid.uuid4().hex[:12]}"


def host_cli(arguments: str) -> str:
    return shlex.quote(GUEST_HOST_CLI) + " " + arguments


def component_check(journey, check: str, state: dict, build: Build) -> None:
    """Installed *and* running, for both apps and every installed plugin."""
    if build.client_version:
        expect(str(state["client"]) == build.version("client"),
               f"client is {state['client']}, this run installed {build.version('client')}")
    # The menu bar's CFBundleVersion is the build's own date, digits only —
    # the one thing `build_hub.bundle_version` says may not be derived twice.
    # It is not knowable before a machine builds, so it is read back against
    # the build id that machine is running.
    day = str(state["build"] or "").split("-")[:3]
    expect(len(day) == 3 and str(state["menubar"]) == "".join(day),
           f"menu bar is {state['menubar']}, which is not the date in {state['build']}")
    expect(state["menubar_pids"], "the menu bar is installed but not running")
    expect(state["plugins"], "no installed plugin versions were observed")
    for name, version in state["plugins"].items():
        expect(version == build.version("stack"),
               f"plugin {name} is {version}, the commit declares {build.version('stack')}")
    journey.observe(check, {"client": state["client"], "menubar": state["menubar"],
                            "plugins": state["plugins"]})


def identity_check(journey, check: str, state: dict, build: Build) -> None:
    """The machine runs the commit under test.

    The commit, not the build id: a build id folds in the client and the
    dependency set of whichever machine did the building, so a hub's id for a
    commit and a freshly installed Mac's id for the same commit legitimately
    differ. The sha is what every honest install of this ref shares.
    """
    expect(state["sha"] == build.sha,
           f"running host built {state['sha']}, not {build.sha} ({build.slug})")
    expect(not state["dirty"], "the running host reports modified source")
    journey.observe(check, {"build": state["build"], "sha": state["sha"],
                            "ref": build.ref, "updater": state["updater"]})


# ── The journeys


def fresh_install(journey, fleet: Fleet, build: Build) -> None:
    guest = fleet.fresh
    expect(guest is not None, "the plan names no pristine guest for a fresh install")
    journey.note(f"installing {build.slug} on pristine guest {guest.name}")
    guest.start()
    install_build(guest, build, fresh=True)
    state = guest.installed()
    identity_check(journey, "installed_build", state, build)
    journey.observe("host_identity", {"host_id": guest.host_id(), "updater": state["updater"]})
    component_check(journey, "app_versions", state, build)
    journey.observe("plugin_versions", state["plugins"])
    machine = adopt(fleet, guest)
    row = fleet.hub.row(machine)
    expect(row["state"] not in {"unknown", "unknown/offline"},
           f"the hub does not see the newly paired Mac: {row['state']}")
    journey.observe("pairing", {"machine": machine, "hub_state": row["state"],
                                "supervisor": row["supervisor"]})
    journey.observe("new_session", new_session(guest))


def upgrade(journey, fleet: Fleet, build: Build) -> None:
    guest = fleet.leaves[0]
    expect(fleet.prior is not None, "this run names no earlier ref to upgrade from")
    before = guest.installed()
    expect(before["sha"] != build.sha,
           "this Mac already runs the commit under test; an upgrade needs the earlier one")
    journey.observe("previous_build", {"build": before["build"], "sha": before["sha"],
                                       "client": before["client"], "menubar": before["menubar"]})
    machine = fleet.machine(guest)
    job = fleet.hub.queue(machine, request_id("upgrade"))
    journey.note(f"hub queued {job['jobs'][0]['id']} for {machine}")
    fleet.hub.wait_for("current", machine)
    state = guest.installed()
    identity_check(journey, "installed_build", state, build)
    journey.observe("host_identity", {"host_id": machine, "verified": state["verified"]})
    component_check(journey, "app_versions", state, build)
    journey.observe("plugin_versions", state["plugins"])


def fleet_journey(journey, fleet: Fleet, build: Build) -> None:
    expect(len(fleet.leaves) >= 2, "the contract's fleet case needs a hub and two managed Macs")
    hub_machine = fleet.machine(fleet.hub)
    first, second = fleet.leaves[0], fleet.leaves[1]
    journey.observe("hub_self_update", update_to_build(fleet, fleet.hub, hub_machine, build))
    # The leaf's own Update action: queued on the leaf, owned by the hub.
    leaf_machine = fleet.machine(first)
    local = first.queue("self", request_id("leaf-local"))
    journey.note(f"leaf-initiated job {local['jobs'][0]['id']}")
    fleet.hub.wait_for("current", leaf_machine)
    expect(first.installed()["sha"] == build.sha, "the leaf did not reach the commit under test")
    journey.observe("leaf_local_update", {"machine": leaf_machine, "job": local["jobs"][0]["id"]})
    fleet.cast(fleet.hub, second)
    other = fleet.machine(second)
    journey.observe("leaf_remote_update", update_to_build(fleet, second, other, build))
    request = request_id("update-all")
    everything = fleet.hub.queue("all", request)
    machines = {job["machine"]: job["id"] for job in everything["jobs"]}
    expect(set(machines) >= {hub_machine, leaf_machine, other},
           f"Update All reached {sorted(machines)}, not the whole fleet")
    for machine in (hub_machine, other):
        fleet.hub.wait_for("current", machine)
    # The parked leaf comes back to find the same queued job and catches up —
    # never a second job minted for the same request.
    completed = {hub_machine, other}
    for guest in [first, *([fleet.fresh] if fleet.fresh else [])]:
        fleet.cast(fleet.hub, guest)
        machine = fleet.machine(guest)
        if machine in machines:
            fleet.hub.wait_for("current", machine)
            completed.add(machine)
    expect(completed == set(machines),
           f"Update All includes machines outside this fixture: {sorted(set(machines) - completed)}")
    journey.observe("update_all", {"request": request, "jobs": machines})
    repeated = fleet.hub.queue("all", request)
    again = {job["machine"]: job["id"] for job in repeated["jobs"]}
    expect(again == machines, f"repeating one request changed its jobs: {again} vs {machines}")
    journey.observe("duplicate_request", again)
    fleet.cast(fleet.hub, first)
    journey.observe("denied_authority", denied(fleet, first))


def offline_catchup(journey, fleet: Fleet, build: Build) -> None:
    guest = fleet.leaves[-1]
    machine = fleet.machine(guest)
    stage_prior(fleet, guest)
    guest.stop()
    journey.note(f"{guest.name} stopped; waiting for its report to go stale")
    time.sleep(100)
    job = fleet.hub.queue(machine, request_id("offline"))["jobs"][0]
    journey.observe("queued_offline", {"job": job["id"], "machine": machine})
    row = fleet.hub.row(machine)
    expect(row["state"] == "pending/offline", f"an unreachable Mac reported {row['state']}")
    journey.observe("offline_state", {"state": row["state"], "last_contact": row["last_contact"]})
    guest.start()
    settled = fleet.hub.wait_for("current", machine)
    expect((settled.get("job") or {}).get("id") == job["id"],
           "catching up created a second job instead of finishing the queued one")
    journey.observe("same_job_current", {"job": job["id"], "state": settled["state"]})
    expect(settled["observed"].get("release") == fleet.offered[build.ref],
           "the returning Mac did not report the build the hub offers")
    journey.observe("fresh_observation", {"last_contact": settled["last_contact"],
                                          "build": settled["observed"].get("release")})


def session_survival(journey, fleet: Fleet, build: Build) -> None:
    guest = fleet.leaves[0]
    machine = fleet.machine(guest)
    stage_prior(fleet, guest)
    session = new_session(guest, prior=True)
    journey.observe("session_pid", {"session": session["session"], "pid": session["pid"],
                                    "bypass_prompt_nudged": session["nudged"],
                                    "request_redelivered": session["reprompted"]})
    update_to_build(fleet, guest, machine, build)
    after = guest.tool_call("session-proof", "--session", session["session"])
    expect(after.get("session") == session["session"] and
           after.get("holders") == session["holders"],
           "the same session-bound provider process did not survive the update")
    journey.observe("surviving_pid", {"session": session["session"], "holders": after["holders"]})
    reply = send_to_session(guest, session["session"])
    journey.observe("new_output", reply)
    state = guest.installed()
    expect(state["menubar_pids"], "the menu bar did not relaunch after the update")
    journey.observe("app_relaunch", {"menubar": state["menubar_pids"], "client": state["client_pids"]})
    # This commit's own start path: a brand-new session on the updated Mac
    # answers with nobody touching its pane, or the build still wedges a
    # remote start on the bypass warning.
    fresh = new_session(guest)
    journey.observe("built_new_session", {"session": fresh["session"], "pid": fresh["pid"],
                                              "bypass_prompt_nudged": fresh["nudged"]})


#: What an abandoned job's detail leads with. `update_supervisor.abandon`
#: appends which release is running to it, so these are prefixes.
RECOVERED = ("updated components failed verification",
             "an interrupted application could not be resumed")
RECOVERED_BY_REBOOT = "an interrupted application could not be resumed"


RESIDUE_LISTING = ("/usr/bin/find /Applications -maxdepth 1 "
                   r"\( -name '*.incoming-*' -o -name '*.previous-*' -o -name '*.failed-*' \) -print")


def residue(guest: Guest) -> list[str]:
    """Bundles the updater left next to the installed ones — a half-written
    copy, a swapped-out one, or a candidate an updater before #118 parked.

    find, not a shell glob: the guest's login shell is zsh, which aborts a
    command line whose glob matches nothing, so the same pattern reads as
    "no copies" on the listing and as a failure on the removal."""
    listing = guest.sh(RESIDUE_LISTING)
    return sorted(line for line in listing.splitlines() if line.startswith("/Applications/"))


INSTALLED_BUNDLE = "/Applications/jStack Hub.app/"
UPDATER_BUNDLE = (
    'pid=$(/usr/bin/pgrep -f "JStackRuntime updater" | /usr/bin/head -1); '
    '[ -n "$pid" ] && /usr/sbin/lsof -p "$pid" 2>/dev/null | '
    "/usr/bin/awk '$4==\"txt\" && /JStackRuntime/ { i=index($0,\"/\"); print substr($0,i); exit }'")


def updater_bundle(guest: Guest) -> str:
    """The executable the guest's live updater is running from — the bundle at
    /Applications, or one that was moved out from under the process (#119)."""
    return guest.sh(UPDATER_BUNDLE).strip()


def ensure_true_updater(journey, guest: Guest) -> str:
    """A journey that queues a job on a leaf must know whose code will run it.

    `update_app.restart_required` exits the supervisor whenever a settled job
    replaced its own bundle, so the relaunched process runs the code that is
    installed. A leaf carried here from an older release predates that: its
    process can still be executing a bundle that is no longer at
    /Applications, and the next job would be applied by that code while the
    receipt claimed to measure this one (#119). Record it and reboot."""
    running = updater_bundle(guest)
    expect(bool(running), f"{guest.name} has no updater process to read")
    if not running.startswith(INSTALLED_BUNDLE):
        journey.note(f"the live updater ran from {running}, not the installed bundle (#119); "
                     "rebooted so the installed release's own code runs")
        guest.stop()
        guest.start()
        running = updater_bundle(guest)
        expect(running.startswith(INSTALLED_BUNDLE),
               f"after a reboot the updater still runs from {running}")
    return running


def sweep_prior_residue(journey, guest: Guest) -> list[str]:
    """The published prior names its incoming copy per RELEASE and never
    removes it (#117): an updater killed during the client copy leaves
    `jRemote.app.incoming-<release>` behind, and every later attempt at that
    release on that machine dies on "unfinished incoming app requires
    recovery". The candidate names copies per job, clears the ground on entry
    and keeps nothing when a job fails. The rest of this journey measures the
    candidate, so the prior's leavings are removed here and the receipt says
    what was found."""
    found = residue(guest)
    if found:
        quoted = " ".join(shlex.quote(path) for path in found)
        # What was there goes on the record before anything touches it.
        shape = guest.sh(f"/bin/ls -lad -- {quoted} 2>&1; "
                         f"/usr/bin/find {quoted} -type f 2>/dev/null | /usr/bin/wc -l").strip()
        journey.note("the prior's updater left " + json.dumps(found) + " (#117): "
                     + " | ".join(line.strip() for line in shape.splitlines() if line.strip()))
        guest.sh(f"/bin/rm -rf -- {quoted}")
        remaining = residue(guest)
        expect(not remaining, f"leftover copies survived removal: {remaining}")
        journey.note("removed " + json.dumps(found))
    return found


def reboot_mid_apply(journey, fleet: Fleet, guest: Guest, machine: str, build: Build) -> dict:
    """Cut the guest with the candidate's own updater frozen half-way through
    an app copy, boot it, and read what the journal did.

    The design under test (update_supervisor.tick): an apply interrupted by
    reboot never resumes over a half-replaced installation — the job ends
    failed, naming which build the machine is left running, the half-written
    copy is removed, and the next request lands. The job installs the earlier
    ref, because that is the only other commit this run builds; what is
    measured is this commit's updater, which is what every machine runs once
    the fleet is moved onto it.
    """
    state = guest.installed()
    expect(state["sha"] == build.sha,
           f"the reboot leg needs this commit's updater; {guest.name} built {state['sha']}")
    # The reboot leg must be applied by the installed bundle's own updater, so
    # a process left running out of a bundle that moved is rebooted (#119).
    ensure_true_updater(journey, guest)
    previous = fleet.prior
    try:
        fleet.offer(previous)
        fault = arm_fault(guest, "freeze")
        frozen = fleet.hub.queue(machine, request_id("reboot"))["jobs"][0]
        injected = read_freeze(fault)
        expect(injected.get("job") == frozen["id"],
               f"the freeze landed on job {injected.get('job')}, not {frozen['id']}")
        guest.stop()
        guest.start()
        settled = fleet.hub.wait_for("failed", machine)
        job = settled.get("job") or {}
        expect(job.get("id") == frozen["id"], "the reboot settled a different job")
        expect(str(job.get("detail") or "").startswith(RECOVERED_BY_REBOOT),
               f"the reboot settled the job as {job.get('detail')!r}")
        kept = guest.installed()
        expect(kept["sha"] == build.sha,
               f"the reboot left {kept['sha']} in place of the commit under test")
        left = residue(guest)
        expect(not left, f"the recovered updater left {left}")
        retry = fleet.hub.queue(machine, request_id("reboot-retry"))["jobs"][0]
        fleet.hub.wait_for("current", machine)
        after = guest.installed()
        expect(after["sha"] == previous.sha,
               f"the request after the reboot left {after['sha']}, not {previous.sha}")
    finally:
        fleet.offer(build)
    return {"job": frozen["id"], "state": settled["state"], "detail": job["detail"],
            "frozen_copies": injected.get("copies"), "build_kept": kept["build"],
            "retry_job": retry["id"], "retry_sha": after["sha"]}


def interruption(journey, fleet: Fleet, build: Build) -> None:
    guest = fleet.leaves[0]
    machine = fleet.machine(guest)
    stage_prior(fleet, guest)
    ensure_true_updater(journey, guest)
    fault = arm_fault(guest, "interruption")
    job = fleet.hub.queue(machine, request_id("interrupt"))["jobs"][0]
    result = read_fault(fault)
    expect(result.get("state") == "failed",
           f"killing the updater mid-apply left {result.get('state')}")
    expect(str(result.get("detail") or "").startswith(RECOVERED),
           f"the killed job failed for {result.get('detail')!r}, not from an interrupted apply")
    journey.observe("interrupted_job", {"job": job["id"], "injected": result.get("job")})
    journey.observe("recovered_state", {"state": result["state"], "detail": result.get("detail")})
    sweep_prior_residue(journey, guest)
    retry = fleet.hub.queue(machine, request_id("interrupt-retry"))["jobs"][0]
    fleet.hub.wait_for("current", machine)
    journey.observe("retry_current", {"job": retry["id"], "sha": guest.installed()["sha"]})
    journey.observe("reboot_resume", reboot_mid_apply(journey, fleet, guest, machine, build))


def revocation(journey, fleet: Fleet, build: Build) -> None:
    guest = fleet.leaves[-1]
    machine = fleet.machine(guest)
    stage_prior(fleet, guest)
    guest.sh("'/Applications/jStack Hub.app/Contents/MacOS/JStackHub' unregister updater")
    try:
        job = fleet.hub.queue(machine, request_id("revoked"))["jobs"][0]
        journey.observe("revoked_device", fleet.hub.tool_call("revoke", "--machine", machine))
        row = fleet.hub.wait_for("cancelled", machine, timeout=300, poll=5)
        journey.observe("cancelled_job", {"job": job["id"], "state": row["state"]})
    finally:
        guest.sh("'/Applications/jStack Hub.app/Contents/MacOS/JStackHub' register updater")
        guest.sh("/bin/launchctl print gui/$(id -u)/live.jstack.hub.updater")
    time.sleep(SETTLE * 4)
    log = guest.sh("/usr/bin/tail -n 40 ~/.local/state/jremote/logs/updater.log")
    expect("401" in log or "not authoriz" in log or "cancelled" in log,
           "the restarted updater did not record a rejected request")
    journey.observe("rejected_request", log.strip().splitlines()[-3:])
    state = guest.installed()
    expect(state["sha"] != build.sha,
           "a revoked machine built the commit it was no longer authorized for")
    journey.observe("unchanged_build", {"build": state["build"], "sha": state["sha"],
                                        "client": state["client"]})


def off_network(journey, fleet: Fleet, build: Build) -> None:
    """Cut direct Hub HTTP access while retaining WireGuard's UDP underlay.

    A host-route blackhole also cuts the tunnel when both use the same IP.
    This isolates the service path, not carrier NAT or a phone's radio.
    """
    guest = fleet.leaves[-1]
    machine = fleet.machine(guest)

    hub_lan = lan_address(fleet.hub)
    hub_mesh = mesh_address(fleet.hub)
    expect(hub_mesh != hub_lan, "the hub's mesh and LAN addresses are the same host route")
    journey.observe("device", {"machine": machine, "guest": guest.name,
                               "hub_lan": hub_lan, "hub_mesh": hub_mesh})

    baseline = guest.sh(
        f"/usr/bin/curl -s -m 8 -o /dev/null -w '%{{http_code}}' "
        f"http://{hub_lan}:9090/api/health || true").strip()
    expect(baseline == "200", f"the LAN path was not usable before isolation ({baseline})")

    anchor = "com.apple/jstack-accept-" + uuid.uuid4().hex
    token = None
    try:
        rule = f"block drop out quick inet proto tcp from any to {hub_lan} port 9090\n"
        guest.sh(f"printf %s {shlex.quote(rule)} | sudo /sbin/pfctl -a {anchor} -f -")
        enabled = guest.sh("sudo /sbin/pfctl -E 2>&1")
        match = re.search(r"Token\s*:\s*(\d+)", enabled)
        expect(match is not None, "pfctl did not return its enable reference")
        token = match.group(1)
        lan_probe = guest.sh(
            f"/usr/bin/curl -s -m 8 -o /dev/null -w '%{{http_code}}' "
            f"http://{hub_lan}:9090/api/health || true").strip()
        expect(lan_probe in ("", "000"),
               f"the LAN path to the hub is still open ({lan_probe}); "
               "this run would have proved nothing")
        mesh_probe = guest.sh(
            f"/usr/bin/curl -s -m 15 -o /dev/null -w '%{{http_code}}' "
            f"http://{hub_mesh}:9090/api/health || true").strip()
        expect(mesh_probe == "200",
               f"with the LAN path cut the hub is unreachable over the mesh ({mesh_probe})")
        journey.observe("transport", {"lan_target": hub_lan, "lan_http": lan_probe or "000",
                                      "mesh_target": hub_mesh, "mesh_http": mesh_probe,
                                      "filter": guest.sh(f"sudo /sbin/pfctl -a {anchor} -sr").strip()})

        # The build has to be visible from out here, not just installable.
        offered = fleet.offered[build.ref]
        row = fleet.hub.row(machine)
        expect(row["desired"] == offered, f"the hub offers {row['desired']}, not {offered}")
        inventory = guest.inventory()
        expect(inventory.get("release") == offered,
               f"off the LAN the Mac sees {inventory.get('release')}, not {offered}")
        journey.observe("build_notice", {"build": inventory.get("release"),
                                         "hub_state": row["state"], "seen_over": hub_mesh})

        journey.observe("session_journey", new_session(guest))
    finally:
        try:
            guest.sh(f"sudo /sbin/pfctl -a {anchor} -F rules")
        finally:
            if token is not None:
                guest.sh(f"sudo /sbin/pfctl -X {token}")
        time.sleep(SETTLE)


def mesh_address(guest: Guest) -> str:
    """The guest's own tunnel address, read off the interface rather than assumed."""
    out = guest.sh("/sbin/ifconfig 2>/dev/null | /usr/bin/awk '/inet 10\\.66\\./ {print $2}'")
    addresses = [line.strip() for line in out.splitlines() if line.strip()]
    expect(addresses, f"{guest.name} has no mesh address; the tunnel is not up there")
    return addresses[0]


def lan_address(guest: Guest) -> str:
    """The guest's LAN address, off whichever interface actually carries it.

    Naming an interface guesses: this hub reaches its LAN over Wi-Fi (en1) and
    `ipconfig getifaddr en0` answers with an empty string there, which read as
    "no LAN address to take away" rather than "asked the wrong interface". So
    ask the routing table which interface holds the default route, and take the
    address off that one.
    """
    interface = guest.sh("/sbin/route -n get default 2>/dev/null "
                         "| /usr/bin/awk '/interface:/ {print $2}'").strip()
    expect(interface, f"{guest.name} has no default route; it is on no LAN")
    address = guest.sh(f"/usr/sbin/ipconfig getifaddr {interface} || true").strip()
    expect(address, f"{guest.name} carries no address on {interface}, its default-route interface")
    return address


JOURNEYS = {"fresh_install": fresh_install, "upgrade": upgrade, "fleet": fleet_journey,
            "offline_catchup": offline_catchup, "session_survival": session_survival,
            "interruption": interruption, "off_network": off_network,
            "revocation": revocation}

# The guests each journey drives; everyone else may be parked on a
# slot-limited host. The fleet journey swaps its own leaves mid-flight.
CAST = {"fresh_install": lambda f: (f.hub, f.fresh),
        "upgrade": lambda f: (f.hub, f.leaves[0] if f.leaves else None),
        "fleet": lambda f: (f.hub, f.leaves[0] if f.leaves else None),
        "offline_catchup": lambda f: (f.hub, f.leaves[-1] if f.leaves else None),
        "session_survival": lambda f: (f.hub, f.leaves[0] if f.leaves else None),
        "interruption": lambda f: (f.hub, f.leaves[0] if f.leaves else None),
        "revocation": lambda f: (f.hub, f.leaves[-1] if f.leaves else None),
        "off_network": lambda f: (f.hub, f.leaves[-1] if f.leaves else None)}


# ── Operations the journeys are written in terms of


def install_build(guest: Guest, build: Build, *, fresh: bool = False) -> None:
    """Install the commit under test the way the README tells a Mac to.

    The installer is fetched from the ref and clones that ref itself, so what
    lands on the guest is a checkout the machine then builds the Hub out of —
    the runner carries in no stack bytes at all. The Mac app is the exception:
    it is closed source and built elsewhere, so it is copied in *before* the
    installer runs, because `install.sh` names an already-installed
    /Applications/jRemote.app as the client of the build it is about to make.
    """
    if fresh:
        existing = guest.sh("for p in ~/.local/state/jremote ~/jStack "
                            "'/Applications/jStack Hub.app' "
                            "~/Library/LaunchAgents/com.jremote.host.plist "
                            "~/Library/LaunchAgents/com.jremote.menubar.plist "
                            "~/Library/LaunchAgents/com.jremote.updater.plist "
                            "~/Library/'Application Support'/jStack/'JStack Host.app' "
                            "~/Applications/'JStack Host.app' /Applications/'JStack Host.app' "
                            "~/Applications/jRemote.app /Applications/jRemote.app; "
                            "do [ ! -e \"$p\" ] || printf '%s\\n' \"$p\"; done").strip()
        expect(not existing, f"{guest.name} is not pristine: {existing}")
    remote = f"{GUEST_HOME}/accept-install-{uuid.uuid4().hex}"
    guest.sh(f"/bin/mkdir {remote}")
    if build.client:
        work = Path(tempfile.mkdtemp(prefix="accept-client-"))
        try:
            archive = work / "jRemote.zip"
            subprocess.run(["/usr/bin/ditto", "-c", "-k", "--keepParent",
                            str(build.client), str(archive)], check=True, timeout=600)
            guest.copy(archive, f"{remote}/jRemote.zip")
        finally:
            shutil.rmtree(work, ignore_errors=True)
        guest.sh(f"/usr/bin/ditto -x -k {remote}/jRemote.zip /Applications", timeout=600)
    guest.sh(f"/usr/bin/curl -fsSL {shlex.quote(build.raw_url)} -o {remote}/install.sh",
             timeout=300)
    guest.sh(f"JSTACK_REPO_URL={shlex.quote(build.repo_url)} "
             f"/bin/bash {remote}/install.sh --yes --no-claude --no-app "
             f"--ref {shlex.quote(build.ref)}", timeout=3600)
    if build.client:
        guest.sh("/usr/bin/open -a /Applications/jRemote.app")
    # The sealed provisioner writes candidate_test false unconditionally and
    # its repair path refuses to change it — that guard is for production
    # machines. A disposable lab guest gets the flag flipped in state, then
    # the updater restarted so its next cycle verifies unpromoted envelopes.
    flip = ("import json,pathlib; "
            "p=pathlib.Path.home()/'.local/state/jremote/updates/config.json'; "
            "c=json.loads(p.read_text()); c['candidate_test']=True; "
            "p.write_text(json.dumps(c, indent=2))")
    guest.sh(f"{shlex.quote(GUEST_PYTHON)} -c {shlex.quote(flip)}")
    guest.sh("/bin/launchctl kickstart -k gui/$(id -u)/live.jstack.hub.updater")
    time.sleep(SETTLE)


def stage_prior(fleet: Fleet, guest: Guest) -> dict:
    """Put a guest back on the earlier commit so a journey starts where it must.

    The hub builds the prior ref and serves it; the leaf takes it the way it
    takes any update. The hub is left holding the ref under test again, or
    the journey that follows would measure the wrong commit.
    """
    expect(fleet.prior is not None, "the plan names no earlier ref to stage from")
    state = guest.installed()
    if state["sha"] == fleet.prior.sha:
        return state
    if fleet.plan.get("stage_prior_command"):
        guest.sh(fleet.plan["stage_prior_command"], timeout=2400)
    else:
        target = fleet.build
        expect(target is not None, "build the ref under test before staging an earlier one")
        machine = fleet.machine(guest)
        try:
            fleet.offer(fleet.prior)
            fleet.hub.queue(machine, request_id("stage-prior"))
            fleet.hub.wait_for("current", machine)
        finally:
            fleet.offer(target)
    time.sleep(SETTLE)
    state = guest.installed()
    expect(state["sha"] == fleet.prior.sha,
           f"{guest.name} built {state['sha']} after staging, not {fleet.prior.sha}")
    return state


def update_to_build(fleet: Fleet, guest: Guest, machine: str, build: Build) -> dict:
    state = guest.installed()
    if state["sha"] != build.sha:
        job = fleet.hub.queue(machine, request_id("to-build"))["jobs"][0]
        fleet.hub.wait_for("current", machine)
        state = guest.installed()
        expect(state["sha"] == build.sha,
               f"{machine} reported current on {state['sha']}, not {build.slug}")
        return {"machine": machine, "job": job["id"], "build": state["build"],
                "sha": state["sha"]}
    return {"machine": machine, "build": state["build"], "sha": state["sha"],
            "job": "already current"}


def adopt(fleet: Fleet, guest: Guest) -> str:
    """Pair a freshly installed Mac to the fixture hub, the way the lab does."""
    expect(fleet.plan.get("adopt_command"),
           "pairing a fresh Mac needs 'adopt_command' in the plan")
    guest.sh(fleet.plan["adopt_command"], timeout=900)
    time.sleep(SETTLE)
    machine = guest.host_id()
    fleet._ids[guest.name] = machine
    return machine


def bypass_prompt_up(guest: Guest, session: str) -> bool:
    """Whether the session's pane is sitting on Claude's bypass warning."""
    pane = guest.sh(f"{shlex.quote(GUEST_TMUX)} -L jremote capture-pane -p -t jr-{session[:8]} "
                    "2>/dev/null || true")
    return "Yes, I accept" in pane


def accept_bypass_prompt(guest: Guest, session: str) -> None:
    """The keys the product's watcher sends, from the runner instead."""
    target = f"jr-{session[:8]}"
    guest.sh(f"{shlex.quote(GUEST_TMUX)} -L jremote send-keys -t {target} Down; "
             f"{shlex.quote(GUEST_TMUX)} -L jremote send-keys -t {target} Enter")


INITIAL_REQUEST = ("Use the shell tool to run pwd once, then report the path. "
                   "Do not modify files or make network requests.")


def prompt_ready(guest: Guest, session: str) -> bool:
    """Whether the session's pane is at Claude's input box with bypass on."""
    pane = guest.sh(f"{shlex.quote(GUEST_TMUX)} -L jremote capture-pane -p -t jr-{session[:8]} "
                    "2>/dev/null || true")
    return "bypass permissions on" in pane and "Yes, I accept" not in pane


def reprompt(guest: Guest, session: str) -> dict | None:
    """Deliver the session's initial request again, through the product's own
    input route; None when the request is already on screen. The published ffe85dc9 prior types it from a startup helper
    that never runs on the sealed bundle (unquoted bundled tmux, #116), so a
    prior whose warning the runner answered still sits at an empty prompt.
    The input route types with an argv, which is why it works where the
    helper does not."""
    deadline = time.monotonic() + 30
    while not prompt_ready(guest, session) and time.monotonic() < deadline:
        time.sleep(1)
    if "run pwd once" in guest.sh(f"{shlex.quote(GUEST_TMUX)} -L jremote capture-pane -p "
                                  f"-t jr-{session[:8]} 2>/dev/null || true"):
        return None  # the prior typed it after all; a second copy would be the runner's answer
    answer = guest.call(f"/sessions/{session}/input", {"text": INITIAL_REQUEST}, timeout=120)
    expect(answer["status"] == 200,
           f"the prior refused the re-delivered request: {answer['status']} {answer['body'][:200]}")
    return answer


def new_session(guest: Guest, *, prior: bool = False) -> dict:
    """A real session opened through the product's own API, executing a tool.

    `prior` marks a session on the release being upgraded FROM. A prior that
    leaves the bypass warning up gets it answered here after NUDGE_AFTER and
    its initial request re-delivered (its own typer never ran either), and
    both `nudged` and `reprompted` come back so the receipt carries them. The
    candidate never gets that help: a session on it that needs the runner's
    keys is a failure.
    """
    answer = guest.tool_call("spawn", "--agent", "update-proof-chat", timeout=900)
    session = answer.get("session_id") or answer.get("session") or answer.get("id")
    expect(session, f"opening a session returned no identity: {answer}")
    started = time.monotonic()
    deadline = started + 240
    proof = {}
    nudged = reprompted = False
    while time.monotonic() < deadline:
        time.sleep(SETTLE)
        proof = guest.tool_call("session-proof", "--session", session)
        answered = any(message.get("role") == "assistant" and
                       GUEST_HOME in message.get("text", "")
                       for message in proof.get("messages", []))
        if proof.get("session") == session and len(proof.get("holders", [])) == 1 and answered:
            break
        if prior and not reprompted and time.monotonic() - started >= NUDGE_AFTER:
            # The warning is only up the first time a guest runs Claude; a
            # guest that accepted it in an earlier journey goes straight to an
            # empty prompt, and the prior's dead typer leaves it there just
            # the same. The request is delivered either way; the receipt says so.
            if bypass_prompt_up(guest, session):
                accept_bypass_prompt(guest, session)
                nudged = True
            reprompted = reprompt(guest, session) is not None
    expect(proof.get("session") == session and len(proof.get("holders", [])) == 1,
           "the new session never acquired exactly one identified provider")
    if not answered and not prior and bypass_prompt_up(guest, session):
        raise AcceptanceFailure("the candidate left a new session on the bypass warning; "
                                "its startup watcher never answered it")
    expect(answered, "the new session did not answer its initial pwd request")
    reply = send_to_session(guest, session)
    return {"session": session, "pid": proof["holders"][0]["pid"],
            "holders": proof["holders"], "reply": reply, "nudged": nudged,
            "reprompted": reprompted}


def send_to_session(guest: Guest, session: str) -> dict:
    """Input into the session that survived, and new output back out of it."""
    marker = uuid.uuid4().hex[:8]
    before = guest.tool_call("session-proof", "--session", session)
    cursor = before["cursor"]
    answer = guest.call(f"/sessions/{session}/input",
                        {"text": f"Reply with exactly: accepted {marker}"}, timeout=300)
    expect(answer["status"] == 200,
           f"the updated Mac refused new input: {answer['status']} {answer['body'][:200]}")
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        proof = guest.tool_call("session-proof", "--session", session, "--after", str(cursor))
        expect(proof.get("session") == session and proof["cursor"] >= cursor,
               "session transcript identity or cursor changed")
        expect(proof.get("holders") == before.get("holders") and len(proof.get("holders", [])) == 1,
               "provider changed while checking resumed input")
        for message in proof.get("messages", []):
            if message.get("role") == "assistant" and marker in message.get("text", ""):
                return {"session": session, "marker": marker, "reply": message["text"][-400:]}
        time.sleep(SETTLE)
    raise AcceptanceFailure("no new assistant reply contains the post-update marker")


def denied(fleet: Fleet, leaf: Guest) -> dict:
    """A managed Mac owns only itself: it cannot queue another machine's update."""
    other = fleet.machine(fleet.leaves[0] if leaf is not fleet.leaves[0] else fleet.leaves[-1])
    answer = leaf.call("/updates/queue", {"target": other, "request_id": request_id("denied")})
    expect(answer["status"] == 403,
           f"a managed Mac was allowed to queue another machine's update: {answer}")
    return answer


def refused_build(fleet: Fleet, guest: Guest, machine: str) -> dict:
    """Bad bytes are refused before anything running is replaced."""
    before = guest.installed()
    if fleet.plan.get("tamper_command"):
        expect(fleet.plan.get("restore_command"), "a custom artifact fault requires restoration")
        fleet.hub.sh(fleet.plan["tamper_command"], timeout=300)
    else:
        fleet.hub.tool_call("tamper")
    try:
        response = fleet.hub.call("/updates/queue", {
            "target": machine, "request_id": request_id("tampered")})
        if response["status"] == 503:
            detail = response["body"]
            expect(any(word in detail.lower() for word in ("signature", "artifact", "mismatch")),
                   f"the hub refused for an unrelated reason: {detail}")
            after = guest.installed()
            expect(all(after[key] == before[key] for key in ("build", "sha", "client", "menubar")),
                   "the rejected artifact changed the running installation")
            return {"refused_by": "hub", "status": 503, "detail": detail,
                    "unchanged_build": after["build"]}
        expect(response["status"] == 200, f"unexpected queue response: {response}")
        job = json.loads(response["body"])["jobs"][0]
        row = fleet.hub.wait_for("failed", machine, timeout=900, poll=5)
        detail = (row.get("job") or {}).get("detail", "")
        expect("signature" in detail or "artifact" in detail or "match" in detail,
               f"a tampered artifact failed for the wrong reason: {detail}")
        return {"job": job["id"], "detail": detail}
    finally:
        if fleet.plan.get("tamper_command"):
            fleet.hub.sh(fleet.plan["restore_command"], timeout=300)
        else:
            fleet.hub.tool_call("restore-artifact")


def arm_fault(guest: Guest, fault: str) -> subprocess.Popen:
    guest.copy(Path(__file__).with_name("managed_update_fault.py"), GUEST_FAULT)
    return guest.background(" ".join(shlex.quote(part)
                                     for part in [GUEST_PYTHON, GUEST_FAULT, fault]))


def read_freeze(process: subprocess.Popen, *, timeout: int = 900) -> dict:
    """The injector's proof that it stopped the updater with a copy half written."""
    output, _ = process.communicate(timeout=timeout)
    if process.returncode != 0:
        raise AcceptanceFailure(f"the freeze injector exited {process.returncode}: {output[-600:]}")
    records = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    injected = next((record for record in records if record.get("injected") == "freeze"), None)
    if not injected or not injected.get("job") or not injected.get("pid") or not injected.get("copies"):
        raise AcceptanceFailure(f"the injector did not prove a frozen mid-copy updater: {output[-600:]}")
    return injected


def read_fault(process: subprocess.Popen, *, timeout: int = 1800) -> dict:
    output, _ = process.communicate(timeout=timeout)
    if process.returncode != 0:
        raise AcceptanceFailure(f"the fault injector exited {process.returncode}: {output[-600:]}")
    records = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    injected = next((record for record in records if record.get("injected")), None)
    terminal = records[-1] if records else {}
    if (not injected or not injected.get("job")
            or terminal.get("job") != injected["job"]
            or terminal.get("fault") != injected["injected"]
            or terminal.get("state") != "failed"):
        raise AcceptanceFailure(f"the fault injector did not prove injection and a failed job: {output[-600:]}")
    return terminal


# ── The run


#: Journeys that start on an earlier commit and move forward from it. Without
#: `--prior-ref` there is nothing to move from, and that is a skip with a
#: reason, never a journey quietly reduced to a no-op.
NEEDS_PRIOR = {"upgrade", "offline_catchup", "session_survival", "interruption", "revocation"}


def unsupported(fleet: Fleet, name: str) -> str | None:
    """Why this plan cannot run a journey — never a reason to pass it quietly."""
    if name in NEEDS_PRIOR and fleet.prior is None:
        return "this run names no earlier ref to start from (--prior-ref)"
    if name == "off_network" and not fleet.leaves:
        return "the plan names no managed Mac to take off the LAN"
    if name == "fresh_install" and fleet.fresh is None:
        return "the plan names no pristine guest"
    if name == "fleet" and len(fleet.leaves) < 2:
        return "the plan names fewer than two managed Macs"
    if name != "fresh_install" and not fleet.leaves:
        return "the plan names no managed Mac"
    return None


DEFAULT_REPO = "https://github.com/jenyalebid/jStack.git"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", required=True, help="the branch under test")
    parser.add_argument("--prior-ref", help="an earlier branch the upgrade legs start from")
    parser.add_argument("--repo", default=DEFAULT_REPO,
                        help="where the guests fetch the installer and the source from")
    parser.add_argument("--client", type=Path,
                        help="the jRemote.app bundle to install; it is not built from this repo")
    parser.add_argument("--checkout", type=Path,
                        default=Path(__file__).resolve().parents[2],
                        help="a checkout used only to read what the commit declares")
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--only", nargs="+", choices=sorted(JOURNEYS),
                        help="run these journeys, retaining previous receipts for the others")
    args = parser.parse_args()
    from jstack_host import acceptance
    plan = json.loads(args.plan.read_text())
    if plan.get("production") or not plan.get("disposable"):
        parser.error("acceptance runs only against a plan marked disposable")
    build = Build(args.repo, args.ref, checkout=args.checkout, client=args.client)
    fleet = Fleet(plan, run=subprocess.run)
    if args.prior_ref:
        fleet.prior = Build(args.repo, args.prior_ref, checkout=args.checkout,
                            client=args.client)
    print(f"Acceptance for {build.slug} over {plan['hub']} "
          f"and {len(fleet.leaves)} managed Macs", flush=True)
    fleet.hub.start()
    if not fleet.slots:
        for leaf in fleet.leaves:
            leaf.start()
    fleet.prepare(fleet.hub)
    # The hub builds the commit before any receipt is written: the build id it
    # comes out with is what the fleet is offered, and a run that cannot even
    # build has nothing to write receipts about.
    identity = {"build": fleet.offer(build), "sha": build.sha}
    run = acceptance.Run(args.receipts, identity)
    for name in JOURNEYS:
        if args.only and name not in args.only:
            continue
        reason = unsupported(fleet, name)
        if reason:
            run.skip(name, reason)
            continue
        with run.journey(name) as journey:
            fleet.cast(*CAST[name](fleet))
            JOURNEYS[name](journey, fleet, build)
    state = acceptance.inspect(args.receipts, identity)
    summary = {"build": identity["build"], "ref": build.ref, "sha": build.sha,
               "results": {name: row["state"] for name, row in state.items()}}
    print(json.dumps(summary, indent=2), flush=True)
    return 0 if all(row["state"] == "passed" for row in state.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
