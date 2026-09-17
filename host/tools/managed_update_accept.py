"""One unattended acceptance run over disposable Macs, writing nine receipts.

Every journey here drives the shipped code: the real installer, the real
supervisor under launchd, the real authenticated routes, real bundle
replacement and the real fault injector. Nothing is simulated, and nothing is
asserted about a machine this run did not ask.

The runner cannot mark a journey passed. It checks an expectation and then
records what it observed; `jstack_host.acceptance` writes the receipt from
those observations and refuses to call an unobserved journey anything but
incomplete. A journey the plan cannot support — two leaves that do not exist,
a test phone that is not wired — is recorded as skipped with its reason, and a
skip keeps promotion closed exactly like a failure.

Disposable guests only. The plan names the VMs; every guest is checked for the
fixture account and candidate-test trust before it is touched.
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

GUEST_HOME = "/Users/admin"
GUEST_TOOL = GUEST_HOME + "/update-vm.py"
GUEST_FAULT = GUEST_HOME + "/update-fault.py"
GUEST_PYTHON = GUEST_HOME + "/jStack/host/.venv/bin/python3"
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
        return self.vm("gui", self.name, timeout=600).strip()

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
        last = {}
        while time.monotonic() < deadline:
            last = self.row(machine)
            if last["state"] == state:
                return last
            if last["state"] in {"failed", "rolled_back", "cancelled"} and last["state"] != state:
                raise AcceptanceFailure(
                    f"{machine} settled on {last['state']} waiting for {state}: "
                    + str((last.get("job") or {}).get("detail", "")))
            time.sleep(poll)
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
        self.cellular = plan.get("cellular")
        self._ids: dict[str, str] = {}
        self.candidate: Candidate | None = None

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
    other = fleet.machine(second)
    journey.observe("leaf_remote_update", update_to_candidate(fleet, second, other, candidate))
    request = request_id("update-all")
    everything = fleet.hub.queue("all", request)
    machines = {job["machine"]: job["id"] for job in everything["jobs"]}
    expect(set(machines) >= {hub_machine, leaf_machine, other},
           f"Update All reached {sorted(machines)}, not the whole fleet")
    for machine in machines:
        fleet.hub.wait_for("current", machine)
    journey.observe("update_all", {"request": request, "jobs": machines})
    repeated = fleet.hub.queue("all", request)
    again = {job["machine"]: job["id"] for job in repeated["jobs"]}
    expect(again == machines, f"repeating one request changed its jobs: {again} vs {machines}")
    journey.observe("duplicate_request", again)
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
    session = new_session(guest)
    journey.observe("session_pid", {"session": session["session"], "pid": session["pid"]})
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
    expect(state["client_pids"], "the client app did not relaunch after the update")
    journey.observe("app_relaunch", {"menubar": state["menubar_pids"], "client": state["client_pids"]})


def interruption(journey, fleet: Fleet, candidate: Candidate) -> None:
    guest = fleet.leaves[0]
    machine = fleet.machine(guest)
    stage_prior(fleet, guest)
    fault = arm_fault(guest, "interruption")
    job = fleet.hub.queue(machine, request_id("interrupt"))["jobs"][0]
    result = read_fault(fault)
    expect(result.get("state") == "rolled_back",
           f"killing the updater mid-apply left {result.get('state')}")
    journey.observe("interrupted_job", {"job": job["id"], "injected": result.get("job")})
    journey.observe("recovered_state", {"state": result["state"], "detail": result.get("detail")})
    retry = fleet.hub.queue(machine, request_id("interrupt-retry"))["jobs"][0]
    fleet.hub.wait_for("current", machine)
    journey.observe("retry_current", {"job": retry["id"], "release": guest.installed()["release"]})
    # A reboot must not resume onto a half-replaced installation.
    stage_prior(fleet, guest)
    resuming = fleet.hub.queue(machine, request_id("reboot"))["jobs"][0]
    guest.stop()
    guest.start()
    settled = fleet.hub.wait_for("current", machine)
    expect((settled.get("job") or {}).get("id") == resuming["id"],
           "the job did not resume across the reboot")
    journey.observe("reboot_resume", {"job": resuming["id"], "state": settled["state"]})


def rollback(journey, fleet: Fleet, candidate: Candidate) -> None:
    guest = fleet.leaves[0]
    machine = fleet.machine(guest)
    before = stage_prior(fleet, guest)
    fault = arm_fault(guest, "rollback")
    job = fleet.hub.queue(machine, request_id("rollback"))["jobs"][0]
    result = read_fault(fault)
    expect(result.get("state") == "rolled_back",
           f"a failed apply left {result.get('state')} instead of rolling back")
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
    guest.sh("/bin/launchctl bootout gui/$(id -u)/com.jremote.updater || true")
    job = fleet.hub.queue(machine, request_id("revoked"))["jobs"][0]
    journey.observe("revoked_device", fleet.hub.tool_call("revoke", "--machine", machine))
    row = fleet.hub.wait_for("cancelled", machine, timeout=300, poll=5)
    journey.observe("cancelled_job", {"job": job["id"], "state": row["state"]})
    guest.sh("/bin/launchctl bootstrap gui/$(id -u) "
             "~/Library/LaunchAgents/com.jremote.updater.plist || true")
    time.sleep(SETTLE * 4)
    log = guest.sh("/usr/bin/tail -n 40 ~/.local/state/jremote/updates/logs/supervisor.err "
                   "~/.local/state/jremote/updates/logs/supervisor.out 2>/dev/null || true")
    expect("401" in log or "not authoriz" in log or "cancelled" in log,
           "the restarted updater did not record a rejected request")
    journey.observe("rejected_request", log.strip().splitlines()[-3:])
    state = guest.installed()
    expect(state["release"] != candidate.release,
           "a revoked machine installed the release it was no longer authorized for")
    journey.observe("unchanged_release", {"release": state["release"], "client": state["client"]})


def cellular(journey, fleet: Fleet, candidate: Candidate) -> None:
    raise AcceptanceFailure("the physical cellular journey is not automated by this runner")


JOURNEYS = {"fresh_install": fresh_install, "upgrade": upgrade, "fleet": fleet_journey,
            "offline_catchup": offline_catchup, "session_survival": session_survival,
            "interruption": interruption, "rollback": rollback, "revocation": revocation,
            "cellular": cellular}


# ── Operations the journeys are written in terms of


def install_candidate(guest: Guest, candidate: Candidate, *, fresh: bool = False) -> None:
    """Install exact candidate bytes through the shipped installer, not a copy of it."""
    if fresh:
        existing = guest.sh("for p in ~/.local/state/jremote ~/jStack "
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
    guest.sh(f"/bin/mkdir ~/jStack && "
             f"/usr/bin/tar -xzf {remote}/{candidate.file('stack').name} -C ~/jStack", timeout=900)
    guest.sh("cd ~/jStack && JSTACK_CHECKOUT=$HOME/jStack /bin/bash install.sh --yes "
             "--no-claude --no-app --no-menubar", timeout=2400)
    guest.sh("/bin/bash ~/jStack/host/menubar/install.sh", timeout=600)
    # Resolve the actual installed bundle from launchd, never make a second
    # registration under Applications. Keep the old bundle outside the .app
    # namespace for recovery, then relaunch the exact signed candidate.
    replace_menu = (
        "import pathlib,plistlib,subprocess; "
        "p=pathlib.Path.home()/'Library/LaunchAgents/com.jremote.menubar.plist'; "
        "job=plistlib.loads(p.read_bytes()); "
        "app=next(x for x in pathlib.Path(job['ProgramArguments'][0]).parents if x.suffix=='.app'); "
        "domain='gui/'+str(__import__('os').getuid()); "
        "subprocess.run(['/bin/launchctl','bootout',domain+'/'+job['Label']],check=True); "
        "app.rename(app.with_suffix('.source-backup')); "
        f"subprocess.run(['/usr/bin/ditto','-x','-k',{str(remote + '/' + candidate.file('menubar').name)!r},str(app.parent)],check=True); "
        "subprocess.run(['/bin/launchctl','bootstrap',domain,str(p)],check=True)"
    )
    guest.sh(f"{shlex.quote(GUEST_PYTHON)} -c {shlex.quote(replace_menu)}", timeout=600)
    guest.sh(f"/usr/bin/ditto -x -k {remote}/{candidate.file('client').name} /Applications",
             timeout=600)
    guest.sh("/usr/bin/open -a /Applications/jRemote.app")
    guest.sh(f"{shlex.quote(GUEST_PYTHON)} -m jstack_host.install_updater --candidate-test "
             "", timeout=600)
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


def new_session(guest: Guest) -> dict:
    """A real session opened through the product's own API, executing a tool."""
    answer = guest.tool_call("spawn", "--agent", "update-proof-chat", timeout=900)
    session = answer.get("session_id") or answer.get("session") or answer.get("id")
    expect(session, f"opening a session returned no identity: {answer}")
    deadline = time.monotonic() + 240
    proof = {}
    while time.monotonic() < deadline:
        time.sleep(SETTLE)
        proof = guest.tool_call("session-proof", "--session", session)
        if proof.get("session") == session and len(proof.get("holders", [])) == 1:
            break
    expect(proof.get("session") == session and len(proof.get("holders", [])) == 1,
           "the new session never acquired exactly one identified provider")
    reply = send_to_session(guest, session)
    return {"session": session, "pid": proof["holders"][0]["pid"],
            "holders": proof["holders"], "reply": reply}


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
    if fleet.plan.get("tamper_command"):
        expect(fleet.plan.get("restore_command"), "a custom artifact fault requires restoration")
        fleet.hub.sh(fleet.plan["tamper_command"], timeout=300)
    else:
        fleet.hub.tool_call("tamper")
    try:
        job = fleet.hub.queue(machine, request_id("tampered"))["jobs"][0]
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
    return guest.background(" ".join(shlex.quote(part)
                                     for part in [GUEST_PYTHON, GUEST_FAULT, fault]))


def read_fault(process: subprocess.Popen, *, timeout: int = 1800) -> dict:
    output, _ = process.communicate(timeout=timeout)
    records = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    if not records:
        raise AcceptanceFailure(f"the fault injector reported nothing: {output[-400:]}")
    return records[-1]


# ── The run


def unsupported(fleet: Fleet, name: str) -> str | None:
    """Why this plan cannot run a journey — never a reason to pass it quietly."""
    if name == "cellular" and not fleet.cellular:
        return "no test phone is wired to this run; the physical cellular journey is unrun"
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
            JOURNEYS[name](journey, fleet, candidate)
    state = acceptance.inspect(args.receipts, candidate.manifest)
    summary = {"release": candidate.release,
               "results": {name: row["state"] for name, row in state.items()}}
    print(json.dumps(summary, indent=2), flush=True)
    return 0 if all(row["state"] == "passed" for row in state.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
