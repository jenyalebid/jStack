"""One unattended acceptance run over disposable Macs, writing nine receipts.

Every journey here drives the shipped code: the real installer, the real
supervisor under launchd, the real authenticated routes, real bundle
replacement and the real fault injector. Nothing is simulated, and nothing is
asserted about a machine this run did not ask.

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
import http.server
import json
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

GUEST_HOME = "/Users/admin"
GUEST_TOOL = GUEST_HOME + "/update-vm.py"
GUEST_FAULT = GUEST_HOME + "/update-fault.py"
GUEST_PYTHON = "/Applications/jStack Hub.app/Contents/MacOS/JStackPython"
GUEST_TMUX = "/Applications/jStack Hub.app/Contents/MacOS/tmux"
#: How long a spawned session gets to answer on its own before the runner looks
#: at its pane. Releases before ca7cf89 left the bypass warning on screen (the
#: watcher ran an unquoted tmux path), so a session on such a PRIOR never
#: starts; answering it is the runner standing in for that fixed watcher, and
#: the receipt says so. A CANDIDATE session is never helped.
NUDGE_AFTER = 30
API_BOOT = 180
SETTLE = 8


class AcceptanceFailure(RuntimeError):
    """An expectation the candidate did not meet. It ends one journey, not the run."""


def expect(condition, message: str) -> None:
    if not condition:
        raise AcceptanceFailure(message)


class Candidate:
    """The exact bytes under test, read from the signed candidate only."""

    def __init__(self, directory: Path, public_key: str):
        from jstack_host import acceptance, release_manifest
        self.dir = Path(directory)
        self.public_key = public_key
        envelope = json.loads((self.dir / "candidate.json").read_text())
        self.manifest = release_manifest.verify(envelope, public_key, promoted=False)
        for item in self.manifest["components"].values():
            release_manifest.check_artifact(self.dir / item["file"], item)
        self.release = self.manifest["release"]
        self.artifacts = acceptance.artifact_set(self.manifest)

    def version(self, component: str) -> str:
        return str(self.manifest["components"][component]["version"])

    def file(self, component: str) -> Path:
        return self.dir / self.manifest["components"][component]["file"]

    @property
    def stack_sha(self) -> str:
        return self.manifest["sources"]["stack"]


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
        return {"release": observed.get("release"),
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


class Fleet:
    """The disposable machines this run may touch, and nothing else."""

    def __init__(self, plan: dict, *, run=subprocess.run):
        tool = Path(plan["vm_tool"]).expanduser()
        self.plan = plan
        self.hub = Guest(plan["hub"], tool, run=run)
        self.leaves = [Guest(name, tool, run=run) for name in plan.get("leaves", [])]
        self.fresh = Guest(plan["fresh"], tool, run=run) if plan.get("fresh") else None
        self.prior = Path(plan["prior_candidate"]).expanduser() if plan.get("prior_candidate") else None
        self.off_lan = plan.get("off_lan")
        self.slots = int(plan.get("vm_slots") or 0)
        self._ids: dict[str, str] = {}
        self.candidate: Candidate | None = None

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

    def machine(self, guest: Guest) -> str:
        if guest.name not in self._ids:
            self._ids[guest.name] = guest.host_id()
        return self._ids[guest.name]

    def offer(self, candidate: Candidate) -> None:
        """Point the fixture hub at the exact candidate under test."""
        remote = f"{GUEST_HOME}/accept-candidate/{candidate.release}"
        self.hub.sh("mkdir -p " + shlex.quote(remote))
        for name in ("stack", "menubar", "client"):
            self.hub.copy(candidate.file(name), f"{remote}/{candidate.file(name).name}")
        self.hub.copy(candidate.dir / "candidate.json", remote + "/candidate.json")
        self.hub.tool_call("offer", "--candidate", remote)
        self.candidate = candidate


def request_id(prefix: str) -> str:
    return f"accept-{prefix}-{uuid.uuid4().hex[:12]}"


def component_check(journey, check: str, state: dict, candidate: Candidate) -> None:
    """Installed *and* running, for both apps and every installed plugin."""
    expect(str(state["client"]) == candidate.version("client"),
           f"client is {state['client']}, candidate is {candidate.version('client')}")
    expect(str(state["menubar"]) == candidate.version("menubar"),
           f"menu bar is {state['menubar']}, candidate is {candidate.version('menubar')}")
    expect(state["menubar_pids"], "the menu bar is installed but not running")
    expect(state["plugins"], "no installed plugin versions were observed")
    for name, version in state["plugins"].items():
        expect(version == candidate.version("stack"),
               f"plugin {name} is {version}, candidate is {candidate.version('stack')}")
    journey.observe(check, {"client": state["client"], "menubar": state["menubar"],
                            "plugins": state["plugins"]})


def identity_check(journey, check: str, state: dict, candidate: Candidate) -> None:
    expect(state["release"] == candidate.release,
           f"running host reports release {state['release']}, not {candidate.release}")
    expect(state["sha"] == candidate.stack_sha,
           f"running host reports source {state['sha']}, not {candidate.stack_sha}")
    expect(not state["dirty"], "the running host reports modified source")
    journey.observe(check, {"release": state["release"], "sha": state["sha"],
                            "updater": state["updater"]})


# ── The journeys


def fresh_install(journey, fleet: Fleet, candidate: Candidate) -> None:
    guest = fleet.fresh
    expect(guest is not None, "the plan names no pristine guest for a fresh install")
    journey.note(f"installing the candidate on pristine guest {guest.name}")
    guest.start()
    install_candidate(guest, candidate, fresh=True)
    state = guest.installed()
    identity_check(journey, "installed_release", state, candidate)
    journey.observe("host_identity", {"host_id": guest.host_id(), "updater": state["updater"]})
    component_check(journey, "app_versions", state, candidate)
    journey.observe("plugin_versions", state["plugins"])
    machine = adopt(fleet, guest)
    row = fleet.hub.row(machine)
    expect(row["state"] not in {"unknown", "unknown/offline"},
           f"the hub does not see the newly paired Mac: {row['state']}")
    journey.observe("pairing", {"machine": machine, "hub_state": row["state"],
                                "supervisor": row["supervisor"]})
    journey.observe("new_session", new_session(guest))


def upgrade(journey, fleet: Fleet, candidate: Candidate) -> None:
    guest = fleet.leaves[0]
    expect(fleet.prior is not None, "the plan names no previous release to upgrade from")
    before = guest.installed()
    expect(before["release"] != candidate.release,
           "this Mac already runs the candidate; an upgrade needs the previous release")
    journey.observe("previous_release", {"release": before["release"], "client": before["client"],
                                         "menubar": before["menubar"]})
    machine = fleet.machine(guest)
    job = fleet.hub.queue(machine, request_id("upgrade"))
    journey.note(f"hub queued {job['jobs'][0]['id']} for {machine}")
    fleet.hub.wait_for("current", machine)
    state = guest.installed()
    identity_check(journey, "installed_release", state, candidate)
    journey.observe("host_identity", {"host_id": machine, "verified": state["verified"]})
    component_check(journey, "app_versions", state, candidate)
    journey.observe("plugin_versions", state["plugins"])


def fleet_journey(journey, fleet: Fleet, candidate: Candidate) -> None:
    expect(len(fleet.leaves) >= 2, "the contract's fleet case needs a hub and two managed Macs")
    hub_machine = fleet.machine(fleet.hub)
    first, second = fleet.leaves[0], fleet.leaves[1]
    journey.observe("hub_self_update", update_to_candidate(fleet, fleet.hub, hub_machine, candidate))
    # The leaf's own Update action: queued on the leaf, owned by the hub.
    leaf_machine = fleet.machine(first)
    local = first.queue("self", request_id("leaf-local"))
    journey.note(f"leaf-initiated job {local['jobs'][0]['id']}")
    fleet.hub.wait_for("current", leaf_machine)
    expect(first.installed()["release"] == candidate.release, "the leaf did not reach the candidate")
    journey.observe("leaf_local_update", {"machine": leaf_machine, "job": local["jobs"][0]["id"]})
    fleet.cast(fleet.hub, second)
    other = fleet.machine(second)
    journey.observe("leaf_remote_update", update_to_candidate(fleet, second, other, candidate))
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


def offline_catchup(journey, fleet: Fleet, candidate: Candidate) -> None:
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
    expect(settled["observed"].get("release") == candidate.release,
           "the returning Mac did not report the candidate")
    journey.observe("fresh_observation", {"last_contact": settled["last_contact"],
                                          "release": settled["observed"].get("release")})


def session_survival(journey, fleet: Fleet, candidate: Candidate) -> None:
    guest = fleet.leaves[0]
    machine = fleet.machine(guest)
    stage_prior(fleet, guest)
    session = new_session(guest, prior=True)
    journey.observe("session_pid", {"session": session["session"], "pid": session["pid"],
                                    "bypass_prompt_nudged": session["nudged"],
                                    "request_redelivered": session["reprompted"]})
    update_to_candidate(fleet, guest, machine, candidate)
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
    # The candidate's own start path: a brand-new session on the updated Mac
    # answers with nobody touching its pane, or the release still wedges a
    # remote start on the bypass warning.
    fresh = new_session(guest)
    journey.observe("candidate_new_session", {"session": fresh["session"], "pid": fresh["pid"],
                                              "bypass_prompt_nudged": fresh["nudged"]})


RECOVERED = {"updated components failed verification", "recovered interrupted application"}
RECOVERED_BY_REBOOT = "recovered interrupted application"


RESIDUE_LISTING = "/usr/bin/find /Applications -maxdepth 1 -name '*.incoming-*' -print"


def residue(guest: Guest) -> list[str]:
    """Half-written app copies left next to the installed bundles.

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
    """The executable the guest's live updater is running from — the bundle
    at /Applications, or the one a rollback moved aside (#119)."""
    return guest.sh(UPDATER_BUNDLE).strip()


def ensure_true_updater(journey, guest: Guest) -> str:
    """A journey that queues a job on a leaf must know whose code will run it.

    The supervisor exits for relaunch only when a job reaches `current`
    (update_app.restart_required). After a rollback that swapped the Hub in,
    launchd's relaunched process keeps executing the rejected release from
    `jStack Hub.app.failed-<job>` while /Applications holds the previous one
    (#119). Then the next job is applied by the wrong updater and the receipt
    would measure it. Record the occurrence and reboot so the installed
    release's own code runs."""
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
    """The published prior names its incoming copy per RELEASE and its rollback
    never removes it (#117): an updater killed during the client copy leaves
    `jRemote.app.incoming-<release>` behind, and every later attempt at that
    release on that machine dies on "unfinished incoming app requires
    recovery". The candidate names copies per job and removes them (71a018f,
    ae87a9a). The rest of this journey measures the candidate, so the prior's
    leftover is removed here and the receipt says what was found."""
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


def reboot_mid_apply(journey, fleet: Fleet, guest: Guest, machine: str, candidate: Candidate) -> dict:
    """Cut the guest with the candidate's own updater frozen half-way through
    an app copy, boot it, and read what the journal did.

    The design under test (update_supervisor.tick): an apply interrupted by
    reboot never resumes over a half-replaced installation — it is rolled back
    as "recovered interrupted application", the release that was running stays
    intact, the copy is removed, and the next request lands. The job installs
    the PRIOR, because that is the only other signed release in the plan; what
    is measured is the candidate's updater, which is what every machine runs
    after this release ships.
    """
    state = guest.installed()
    expect(state["release"] == candidate.release,
           f"the reboot leg needs the candidate's updater; {guest.name} runs {state['release']}")
    # After the kill leg's rollback the relaunched supervisor keeps running the
    # candidate's code from `.failed-<job>` (#119); the reboot leg must read the
    # installed bundle's own updater, so a stale one is rebooted and recorded.
    ensure_true_updater(journey, guest)
    previous = Candidate(fleet.prior, candidate.public_key)
    try:
        fleet.offer(previous)
        fault = arm_fault(guest, "freeze")
        frozen = fleet.hub.queue(machine, request_id("reboot"))["jobs"][0]
        injected = read_freeze(fault)
        expect(injected.get("job") == frozen["id"],
               f"the freeze landed on job {injected.get('job')}, not {frozen['id']}")
        guest.stop()
        guest.start()
        settled = fleet.hub.wait_for("rolled_back", machine)
        job = settled.get("job") or {}
        expect(job.get("id") == frozen["id"], "the reboot settled a different job")
        expect(job.get("detail") == RECOVERED_BY_REBOOT,
               f"the reboot settled the job as {job.get('detail')!r}")
        kept = guest.installed()
        expect(kept["release"] == candidate.release,
               f"the reboot left {kept['release']} in place of the candidate")
        left = residue(guest)
        expect(not left, f"the recovered updater left {left}")
        retry = fleet.hub.queue(machine, request_id("reboot-retry"))["jobs"][0]
        fleet.hub.wait_for("current", machine)
        after = guest.installed()
        expect(after["release"] == previous.release,
               f"the request after the reboot left {after['release']}, not {previous.release}")
    finally:
        fleet.offer(candidate)
    return {"job": frozen["id"], "state": settled["state"], "detail": job["detail"],
            "frozen_copies": injected.get("copies"), "release_kept": kept["release"],
            "retry_job": retry["id"], "retry_release": after["release"]}


def interruption(journey, fleet: Fleet, candidate: Candidate) -> None:
    guest = fleet.leaves[0]
    machine = fleet.machine(guest)
    stage_prior(fleet, guest)
    ensure_true_updater(journey, guest)
    fault = arm_fault(guest, "interruption")
    job = fleet.hub.queue(machine, request_id("interrupt"))["jobs"][0]
    result = read_fault(fault)
    expect(result.get("state") == "rolled_back",
           f"killing the updater mid-apply left {result.get('state')}")
    expect(result.get("detail") in RECOVERED,
           f"the killed job rolled back for {result.get('detail')!r}, not from recovery")
    journey.observe("interrupted_job", {"job": job["id"], "injected": result.get("job")})
    journey.observe("recovered_state", {"state": result["state"], "detail": result.get("detail")})
    sweep_prior_residue(journey, guest)
    retry = fleet.hub.queue(machine, request_id("interrupt-retry"))["jobs"][0]
    fleet.hub.wait_for("current", machine)
    journey.observe("retry_current", {"job": retry["id"], "release": guest.installed()["release"]})
    journey.observe("reboot_resume", reboot_mid_apply(journey, fleet, guest, machine, candidate))


def rollback(journey, fleet: Fleet, candidate: Candidate) -> None:
    guest = fleet.leaves[0]
    machine = fleet.machine(guest)
    before = stage_prior(fleet, guest)
    ensure_true_updater(journey, guest)
    fault = arm_fault(guest, "rollback")
    job = fleet.hub.queue(machine, request_id("rollback"))["jobs"][0]
    result = read_fault(fault)
    expect(result.get("state") == "rolled_back",
           f"a failed apply left {result.get('state')} instead of rolling back")
    detail = str(result.get("detail") or "")
    expect(detail.startswith("ditto failed") and "client/jRemote.app" in detail,
           f"the job rolled back for {detail!r}, not for the withheld client bundle")
    journey.observe("failed_job", {"job": job["id"], "detail": result.get("detail")})
    journey.observe("rolled_back_state", result["state"])
    after = guest.installed()
    expect(after["client"] == before["client"] and after["menubar"] == before["menubar"],
           f"rollback left {after['client']}/{after['menubar']}, not {before['client']}/{before['menubar']}")
    journey.observe("restored_components", {"client": after["client"], "menubar": after["menubar"]})
    journey.observe("refused_release", refused_release(fleet, guest, machine))
    row = fleet.hub.row(machine)
    expect(row["supervisor"] and row["state"] not in {"unknown", "unknown/offline"},
           "the machine lost its pairing or its supervisor through the failure")
    journey.observe("pairing_intact", {"state": row["state"], "supervisor": row["supervisor"]})


def revocation(journey, fleet: Fleet, candidate: Candidate) -> None:
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
    expect(state["release"] != candidate.release,
           "a revoked machine installed the release it was no longer authorized for")
    journey.observe("unchanged_release", {"release": state["release"], "client": state["client"]})


def off_network(journey, fleet: Fleet, candidate: Candidate) -> None:
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

        # The release has to be visible from out here, not just installable.
        row = fleet.hub.row(machine)
        expect(row["desired"] == candidate.release,
               f"the hub offers {row['desired']}, not {candidate.release}")
        inventory = guest.inventory()
        expect(inventory.get("release") == candidate.release,
               f"off the LAN the Mac sees release {inventory.get('release')}, "
               f"not {candidate.release}")
        journey.observe("release_notice", {"release": inventory.get("release"),
                                           "hub_state": row["state"],
                                           "seen_over": hub_mesh})

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
            "interruption": interruption, "rollback": rollback, "off_network": off_network,
            "revocation": revocation}

# The guests each journey drives; everyone else may be parked on a
# slot-limited host. The fleet journey swaps its own leaves mid-flight.
CAST = {"fresh_install": lambda f: (f.hub, f.fresh),
        "upgrade": lambda f: (f.hub, f.leaves[0] if f.leaves else None),
        "fleet": lambda f: (f.hub, f.leaves[0] if f.leaves else None),
        "offline_catchup": lambda f: (f.hub, f.leaves[-1] if f.leaves else None),
        "session_survival": lambda f: (f.hub, f.leaves[0] if f.leaves else None),
        "interruption": lambda f: (f.hub, f.leaves[0] if f.leaves else None),
        "rollback": lambda f: (f.hub, f.leaves[0] if f.leaves else None),
        "revocation": lambda f: (f.hub, f.leaves[-1] if f.leaves else None),
        "off_network": lambda f: (f.hub, f.leaves[-1] if f.leaves else None)}


# ── Operations the journeys are written in terms of


@contextlib.contextmanager
def candidate_repo(candidate: Candidate):
    """Serve one candidate's release assets the way the repo URL would.

    install.sh resolves the current tag over the repo URL (git's dumb HTTP
    reads info/refs as plain "sha<TAB>ref" lines) and downloads assets from
    releases/download/<tag>/<file>. Answering both from the candidate
    directory makes the shipped installer install the candidate's own sealed
    Hub — the public URL would hand it the latest published release instead.
    """
    refs = f"{candidate.stack_sha}\trefs/tags/stack-release-{candidate.release}\n".encode()
    directory = candidate.dir

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path.endswith("/info/refs"):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(refs)))
                self.end_headers()
                self.wfile.write(refs)
                return
            asset = directory / path.rsplit("/", 1)[-1]
            if "/releases/download/" not in path or not asset.is_file():
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(asset.stat().st_size))
            self.end_headers()
            with asset.open("rb") as handle:
                shutil.copyfileobj(handle, self.wfile)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()


def install_candidate(guest: Guest, candidate: Candidate, *, fresh: bool = False) -> None:
    """Install exact candidate bytes through the shipped installer, not a copy of it."""
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
    for name in ("stack", "menubar", "client"):
        guest.copy(candidate.file(name), f"{remote}/{candidate.file(name).name}")
    # An untarred snapshot with a release identity and no .git is what the
    # installer treats as a publisher snapshot: exact local bytes, no fetch.
    guest.sh(f"/bin/mkdir ~/jStack && "
             f"/usr/bin/tar -xzf {remote}/{candidate.file('stack').name} -C ~/jStack", timeout=900)
    gateway = guest.sh("/sbin/route -n get default | /usr/bin/awk '/gateway/{print $2}'").strip()
    expect(gateway, f"{guest.name}: no default gateway — cannot reach the host's candidate server")
    with candidate_repo(candidate) as port:
        guest.sh(f"cd ~/jStack && JSTACK_CHECKOUT=$HOME/jStack "
                 f"JSTACK_REPO_URL=http://{gateway}:{port}/jstack.git "
                 "/bin/bash install.sh --yes --no-claude --no-app", timeout=2400)
    guest.sh(f"/usr/bin/ditto -x -k {remote}/{candidate.file('client').name} /Applications",
             timeout=600)
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
    """Put a guest back on the previous release so a journey starts where it must."""
    state = guest.installed()
    if fleet.prior is None:
        raise AcceptanceFailure("the plan names no previous release to stage from")
    prior = json.loads((fleet.prior / "candidate.json").read_text())["manifest"]["release"]
    if state["release"] == prior:
        return state
    if fleet.plan.get("stage_prior_command"):
        guest.sh(fleet.plan["stage_prior_command"], timeout=2400)
    else:
        target = fleet.candidate
        expect(target is not None, "offer the candidate before staging a previous release")
        previous = Candidate(fleet.prior, target.public_key)
        machine = fleet.machine(guest)
        try:
            fleet.offer(previous)
            fleet.hub.queue(machine, request_id("stage-prior"))
            fleet.hub.wait_for("current", machine)
        finally:
            fleet.offer(target)
    time.sleep(SETTLE)
    state = guest.installed()
    expect(state["release"] == prior,
           f"{guest.name} is on {state['release']} after staging, not {prior}")
    return state


def update_to_candidate(fleet: Fleet, guest: Guest, machine: str, candidate: Candidate) -> dict:
    state = guest.installed()
    if state["release"] != candidate.release:
        job = fleet.hub.queue(machine, request_id("to-candidate"))["jobs"][0]
        fleet.hub.wait_for("current", machine)
        state = guest.installed()
        expect(state["release"] == candidate.release,
               f"{machine} reported current on {state['release']}")
        return {"machine": machine, "job": job["id"], "release": state["release"]}
    return {"machine": machine, "release": state["release"], "job": "already current"}


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


def refused_release(fleet: Fleet, guest: Guest, machine: str) -> dict:
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
            expect(all(after[key] == before[key] for key in ("release", "sha", "client", "menubar")),
                   "the rejected artifact changed the running installation")
            return {"refused_by": "hub", "status": 503, "detail": detail,
                    "unchanged_release": after["release"]}
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
            or terminal.get("state") != "rolled_back"):
        raise AcceptanceFailure(f"the fault injector did not prove injection and recovery: {output[-600:]}")
    return terminal


# ── The run


def unsupported(fleet: Fleet, name: str) -> str | None:
    """Why this plan cannot run a journey — never a reason to pass it quietly."""
    if name == "off_network" and not fleet.leaves:
        return "the plan names no managed Mac to take off the LAN"
    if name == "fresh_install" and fleet.fresh is None:
        return "the plan names no pristine guest"
    if name == "fleet" and len(fleet.leaves) < 2:
        return "the plan names fewer than two managed Macs"
    if name != "fresh_install" and not fleet.leaves:
        return "the plan names no managed Mac"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--only", nargs="+", choices=sorted(JOURNEYS),
                        help="run these journeys, retaining previous receipts for the others")
    args = parser.parse_args()
    from jstack_host import acceptance
    trust = Path(__file__).resolve().parents[1] / "jstack_host/release-trust.json"
    plan = json.loads(args.plan.read_text())
    if plan.get("production") or not plan.get("disposable"):
        parser.error("acceptance runs only against a plan marked disposable")
    candidate = Candidate(args.candidate, json.loads(trust.read_text())["public_key"])
    fleet = Fleet(plan, run=subprocess.run)
    run = acceptance.Run(args.receipts, candidate.manifest)
    print(f"Acceptance for {candidate.release} over {plan['hub']} "
          f"and {len(fleet.leaves)} managed Macs", flush=True)
    fleet.hub.start()
    if not fleet.slots:
        for leaf in fleet.leaves:
            leaf.start()
    fleet.offer(candidate)
    for name in JOURNEYS:
        if args.only and name not in args.only:
            continue
        reason = unsupported(fleet, name)
        if reason:
            run.skip(name, reason)
            continue
        with run.journey(name) as journey:
            fleet.cast(*CAST[name](fleet))
            JOURNEYS[name](journey, fleet, candidate)
    state = acceptance.inspect(args.receipts, candidate.manifest)
    summary = {"release": candidate.release,
               "results": {name: row["state"] for name, row in state.items()}}
    print(json.dumps(summary, indent=2), flush=True)
    return 0 if all(row["state"] == "passed" for row in state.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
