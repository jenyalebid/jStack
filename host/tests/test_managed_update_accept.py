"""The acceptance runner's journeys, driven against a scripted fleet.

A runner that reports what it hoped for is worse than no runner, so what is
pinned here is the refusal: a guest that claims `current` while still built
from the old commit, a menu credential that is allowed to queue somebody
else's update, a fleet with one leaf standing in for two. Each of those must
end as `failed` or `incomplete`, never as a receipt that would open the gate.

The fleet is scripted rather than booted — booting is what the live run does.
What these tests own is the logic between the observations.
"""
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from jstack_host import acceptance, release_manifest as releases

TOOLS = Path(__file__).resolve().parents[1] / "tools/managed_update_accept.py"
#: The subject is a commit, so the scripted fleet moves a sha. The build ids
#: ride along because the hub still reports one and the menu bar's
#: CFBundleVersion is read back out of it.
PRIOR_SHA, HEAD_SHA = "a" * 40, "1" * 40
PRIOR = "2026-09-15-aaaaaaaa-0000000000000000"
CANDIDATE = "2026-09-16-11111111-1111111111111111"
REPO = "https://github.com/jenyalebid/jStack.git"


def dated(build_id: str, version: str = "0.69.3") -> str:
    """The menu bar's CFBundleVersion: `YYYYMMDD.N.<int(sha8, 16)>` — the
    build's own date, the month's release out of a `YY.M.N` version (0 for
    any other), and its commit as a number."""
    parts = build_id.split("-")
    release = re.fullmatch(r"\d{2}\.\d{1,2}\.(\d+)", version)
    return f"{''.join(parts[:3])}.{int(release[1]) if release else 0}.{int(parts[3], 16)}"


def test_the_menu_bar_version_is_read_back_by_the_one_formula(runner):
    from jstack_host import build_hub
    for version in ("0.69.3", "26.9.4"):
        expected = build_hub.bundle_version({"date": "2026-09-16", "sha": "1" * 40}, version)
        assert runner.menubar_version(CANDIDATE, version) == expected == dated(CANDIDATE, version)
    assert runner.menubar_version("not-a-build", "26.9.4") == ""


def test_off_network_requires_a_working_lan_before_isolation(runner, monkeypatch):
    commands = []
    def shell(command):
        commands.append(command)
        return "000"
    guest = SimpleNamespace(name="leaf", sh=shell)
    fleet = SimpleNamespace(leaves=[guest], hub=object(), off_lan="192.168.2.250",
                            machine=lambda _: "leaf-id")
    monkeypatch.setattr(runner, "mesh_address", lambda _: "10.66.0.1")
    monkeypatch.setattr(runner, "lan_address", lambda _: "192.168.2.250")
    with pytest.raises(runner.AcceptanceFailure, match="not usable before isolation"):
        runner.off_network(SimpleNamespace(observe=lambda *args: None), fleet, None)
    assert len(commands) == 1
    assert "http://192.168.2.250:9090" in commands[0]
    assert not any("route -n add" in command for command in commands)


def test_off_network_failure_releases_only_its_firewall_reference(runner, monkeypatch):
    commands = []
    responses = iter(["200", "200"])
    def shell(command):
        commands.append(command)
        if "curl" in command:
            return next(responses)
        if "pfctl -E" in command:
            return "pf enabled\nToken : 12345\n"
        return ""
    guest = SimpleNamespace(name="leaf", sh=shell)
    fleet = SimpleNamespace(leaves=[guest], hub=object(), machine=lambda _: "leaf-id")
    monkeypatch.setattr(runner, "mesh_address", lambda _: "10.66.0.1")
    monkeypatch.setattr(runner, "lan_address", lambda _: "192.168.2.36")
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    with pytest.raises(runner.AcceptanceFailure, match="still open"):
        runner.off_network(SimpleNamespace(observe=lambda *args: None), fleet, None)
    assert commands[-1] == "sudo /sbin/pfctl -X 12345"
    assert "-F rules" in commands[-2]
    assert any("proto tcp" in c and "port 9090" in c for c in commands)
    assert not any("route -n" in c or "pfctl -d" in c for c in commands)


def test_the_permanent_journeys_close_the_run(runner):
    # Revocation is permanent for leaves[-1]'s authority and detach removes its
    # leaf from the fleet entirely: everything that needs a live pair runs
    # before revocation, and detach — on a leaf revocation never touched — is
    # the very last thing a run does.
    assert list(runner.JOURNEYS)[-2:] == ["revocation", "shell_detach"]
    fleet = SimpleNamespace(hub="hub", leaves=["a", "b"], fresh=None)
    assert runner.CAST["shell_detach"](fleet) == ("hub", "a")
    assert runner.CAST["revocation"](fleet) == ("hub", "b")


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location("managed_update_accept", TOOLS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def a_build(runner, monkeypatch, tmp_path, ref, sha, *, version="0.69.3", client="70"):
    """A `Build` without the network: what the ref resolves to is scripted."""
    app = tmp_path / (ref + "-jRemote.app")
    app.mkdir(exist_ok=True)
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, sha + "\trefs/heads/" + ref, ""))
    monkeypatch.setattr(runner, "git", lambda checkout, *argv, **kwargs:
                        json.dumps({"version": version}) if argv[0] == "show" else "")
    monkeypatch.setattr(runner, "bundle_build", lambda _: client)
    return runner.Build(REPO, ref, checkout=tmp_path, client=app)


@pytest.fixture
def subject(tmp_path, runner, monkeypatch):
    return a_build(runner, monkeypatch, tmp_path, "dev", HEAD_SHA)


@pytest.fixture
def earlier(tmp_path, runner, monkeypatch):
    return a_build(runner, monkeypatch, tmp_path, "main", PRIOR_SHA)


#: What a hub answers when it has built the commit under test.
IDENTITY = {"build": CANDIDATE, "sha": HEAD_SHA}


class ScriptedFleet:
    """One disposable fleet's answers, in the shape vm.sh and the tools give them."""

    def __init__(self, *, release=PRIOR, sha=PRIOR_SHA, state="current", client="70",
                 menubar=None, plugin="0.69.3", denied_status=403, address="192.168.2.10",
                 served_client="70"):
        self.release, self.sha, self.state = release, sha, state
        #: The client the hub's feed carries in the build it last offered.
        self.served_client = served_client
        #: Rows the hub still holds for Macs of earlier runs, and what it was
        #: asked to forget.
        self.ghosts: list[str] = []
        self.forgotten: list[str] = []
        #: What `ifconfig` answers on every guest of this fleet.
        self.address = address
        #: What the lab-flag probe answers: 4 just turned on (what an install
        #: leaves behind), 0 already on, 3 no host.
        self.lab_state = "4"
        self.flagged: list[str] = []
        self.kicked: list[str] = []
        self.client, self.menubar, self.plugin = client, menubar, plugin
        self.denied_status = denied_status
        self.calls: list[list[str]] = []
        self.queued: list[str] = []

    def probe(self, name):
        return {"host_id": "machine-" + name,
                "observed": {"release": self.release,
                             "host_source": {"sha": self.sha, "release": self.release,
                                             "dirty": False},
                             "updater_source": {"sha": self.sha}, "verified": True,
                             "components": {"client": {"installed": self.client, "running_pids": [11]},
                                            "menubar": {"installed": self.menubar or dated(self.release),
                                                        "running_pids": [12]},
                                            "plugins": {"claude": {"version": self.plugin}}}},
                "job": {}, "adopted": True, "managed": True}

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        _, action, name, *rest = argv
        command = rest[0] if rest else ""
        answer: dict = {}
        if action in {"gui", "stop", "cp", "reset"}:
            answer = {"ok": True}
        elif "ifconfig" in command:
            return subprocess.CompletedProcess(argv, 0, f"127.0.0.1\n{self.address}\n", "")
        elif "latest.json" in command:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"build": CANDIDATE, "client": self.served_client}), "")
        elif "candidate_test" in command:
            self.flagged.append(name)
            return subprocess.CompletedProcess(argv, 0, f"lab={self.lab_state}\n", "")
        elif "launchctl kickstart" in command:
            self.kicked.append(name)
            return subprocess.CompletedProcess(argv, 0, "", "")
        elif "parent.json" in command:
            # No credential recorded: nothing for the hub to have revoked.
            return subprocess.CompletedProcess(argv, 0, "\n", "")
        elif "--path /devices" in command:
            answer = {"status": 200, "body": json.dumps({"devices": []})}
        elif "probe" in command:
            answer = self.probe(name)
        elif "--path /updates/queue" in command:
            answer = {"status": self.denied_status, "body": "refused"}
        elif "/forget" in command:
            machine = command.split("--path /hosts/")[1].split("/forget")[0]
            self.forgotten.append(machine)
            self.ghosts = [ghost for ghost in self.ghosts if ghost != machine]
            answer = {"status": 200, "body": json.dumps({"forgotten": machine})}
        elif "--path" in command:
            answer = {"status": 200, "body": "accepted"}
        elif "queue" in command:
            target = command.split("--target ")[1].split()[0]
            request = command.split("--request ")[1].split()[0]
            targets = (["machine-hub", "machine-leaf-a", "machine-leaf-b"] if target == "all"
                       else [f"machine-{name}" if target == "self" else target])
            self.queued.extend(targets)
            # The commit the fleet actually moves to when a job is queued.
            self.release, self.sha = CANDIDATE, HEAD_SHA
            # One request ID names one job per machine, however often it arrives.
            answer = {"jobs": [{"id": f"job-{request}-{machine}", "machine": machine}
                               for machine in targets], "errors": []}
        elif "inventory" in command:
            answer = {"release": CANDIDATE, "machines": [
                {"machine": machine, "name": machine, "state": self.state, "supervisor": True,
                 "last_contact": 1.0, "observed": {"release": self.release}, "job": {"id": "job-1"}}
                for machine in {"machine-hub", "machine-leaf-a", "machine-leaf-b", *self.queued,
                                *self.ghosts}]}
        elif "--path /updates/queue" in command:
            answer = {"status": self.denied_status, "body": "refused"}
        elif "--path" in command:
            answer = {"status": 200, "body": "accepted"}
        return subprocess.CompletedProcess(argv, 0, json.dumps(answer), "")


def build(runner, fleet, *, prior=None, **plan):
    made = runner.Fleet({"vm_tool": "/bin/vm.sh", "hub": "hub", "leaves": ["leaf-a", "leaf-b"],
                         **plan}, run=fleet)
    made.prior = prior if prior is not None else SimpleNamespace(
        sha=PRIOR_SHA, ref="main", slug="main@" + PRIOR_SHA[:8])
    # What the hub came back with when it built the ref, without building,
    # and the client its feed carries in that build. `offer` also leaves the
    # fleet holding the build it last offered, which `reach` reads.
    made.build = SimpleNamespace(sha=HEAD_SHA, ref="dev", slug="dev@" + HEAD_SHA[:8])
    made.offered["dev"] = CANDIDATE
    made.served["dev"] = fleet.served_client if isinstance(fleet, ScriptedFleet) else "70"
    return made


def journey_result(runner, subject, name, fleet, tmp_path, **plan):
    run = acceptance.Run(tmp_path / "receipts", IDENTITY)
    with run.journey(name) as journey:
        runner.JOURNEYS[name](journey, build(runner, fleet, **plan), subject)
    return run.results[name], json.loads((tmp_path / "receipts" / (name + ".json")).read_text())


def test_an_upgrade_that_really_lands_records_every_required_fact(runner, subject, tmp_path):
    result, receipt = journey_result(runner, subject, "upgrade", ScriptedFleet(), tmp_path)
    assert result == "passed"
    assert receipt["observed"] == sorted(acceptance.REQUIRED["upgrade"])


def test_a_machine_that_claims_current_on_the_old_commit_fails(runner, subject, tmp_path):
    class Stuck(ScriptedFleet):
        def __call__(self, argv, **kwargs):
            done = super().__call__(argv, **kwargs)
            # Reports current, never actually builds the new commit.
            self.release, self.sha = PRIOR, PRIOR_SHA
            return done

    result, receipt = journey_result(runner, subject, "upgrade", Stuck(), tmp_path)
    assert result == "failed"
    assert "built " + PRIOR_SHA in receipt["detail"]
    assert "installed_build" in receipt["missing"]


def test_a_stale_app_bundle_under_a_new_host_fails(runner, subject, tmp_path):
    result, receipt = journey_result(runner, subject, "upgrade",
                                     ScriptedFleet(client="69"), tmp_path)
    assert result == "failed" and "client is 69" in receipt["detail"]
    assert "carries 70" in receipt["detail"]


def test_a_served_mac_is_held_to_the_client_the_hubs_build_carries(runner, subject, tmp_path):
    """The hub builds no client; its build carries the one its feed holds
    (`build_source.inherited`). A leaf the hub serves lands on that client,
    and the run's own --client, laid down only on Macs it installs by hand,
    says nothing about it."""
    assert subject.version("client") == "70"
    result, receipt = journey_result(runner, subject, "upgrade",
                                     ScriptedFleet(client="69", served_client="69"), tmp_path)
    assert result == "passed", receipt["detail"]


def test_a_fresh_mac_is_held_to_the_client_this_run_laid_down(runner, subject, tmp_path,
                                                                monkeypatch):
    """Built on the Mac itself out of the client the run carried in, so the
    hub's client is beside the point: 70 was installed, 70 must run."""
    monkeypatch.setattr(runner, "install_build", lambda *args, **kwargs: None)
    fleet = ScriptedFleet(release=CANDIDATE, sha=HEAD_SHA, client="69", served_client="69")
    result, receipt = journey_result(runner, subject, "fresh_install", fleet, tmp_path,
                                     fresh="fresh", adopt_command="adopt")
    assert result == "failed" and "client is 69, the build this Mac took carries 70" in receipt["detail"]


def test_a_plugin_left_on_the_old_version_fails(runner, subject, tmp_path):
    result, receipt = journey_result(runner, subject, "upgrade",
                                     ScriptedFleet(plugin="0.69.2"), tmp_path)
    assert result == "failed" and "plugin claude is 0.69.2" in receipt["detail"]


def test_a_leaf_allowed_to_queue_another_machine_fails_the_fleet_journey(runner, subject, tmp_path):
    result, receipt = journey_result(runner, subject, "fleet",
                                     ScriptedFleet(denied_status=200), tmp_path)
    assert result == "failed"
    assert "queue another machine" in receipt["detail"]
    assert receipt["missing"] == ["denied_authority"]


def test_a_settled_failed_machine_never_reads_as_reaching_the_release(runner, subject, tmp_path):
    result, receipt = journey_result(runner, subject, "upgrade",
                                     ScriptedFleet(state="failed"), tmp_path)
    assert result == "failed" and "failed" in receipt["detail"]


def test_one_leaf_cannot_stand_in_for_the_contract_s_two(runner, subject, tmp_path):
    fleet = build(runner, ScriptedFleet(), leaves=["leaf-a"])
    assert runner.unsupported(fleet, "fleet") == "the plan names fewer than two managed Macs"
    assert runner.unsupported(fleet, "off_network") is None, "one leaf is enough to go off the LAN"
    assert runner.unsupported(build(runner, ScriptedFleet(), leaves=[]), "off_network") \
        == "the plan names no managed Mac to take off the LAN"
    assert runner.unsupported(fleet, "fresh_install") == "the plan names no pristine guest"
    assert runner.unsupported(fleet, "upgrade") is None


class SlotCountingFleet(ScriptedFleet):
    """Tracks which guests are booted, and the most that ever ran at once."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.booted: set[str] = set()
        self.peak = 0

    def __call__(self, argv, **kwargs):
        _, action, name, *_ = argv
        if action == "gui":
            self.booted.add(name)
            self.peak = max(self.peak, len(self.booted))
        elif action == "stop":
            self.booted.discard(name)
        return super().__call__(argv, **kwargs)


def test_a_two_slot_host_never_boots_a_third_guest(runner, subject, tmp_path):
    scripted = SlotCountingFleet()
    fleet = build(runner, scripted, vm_slots=2)
    run = acceptance.Run(tmp_path / "receipts", IDENTITY)
    with run.journey("fleet") as journey:
        fleet.cast(*runner.CAST["fleet"](fleet))
        runner.JOURNEYS["fleet"](journey, fleet, subject)
    assert run.results["fleet"] == "passed"
    assert scripted.peak <= 2, "the fleet journey booted more guests than the host has slots"
    assert any(call[1] == "stop" for call in scripted.calls), \
        "a two-slot plan must park a leaf to make room for the other"


def test_a_cast_larger_than_the_slots_is_refused(runner):
    scripted = SlotCountingFleet()
    fleet = build(runner, scripted, vm_slots=2)
    with pytest.raises(runner.AcceptanceFailure):
        fleet.cast(fleet.hub, fleet.leaves[0], fleet.leaves[1])


def test_update_all_wakes_the_mac_enrolled_by_fresh_install(runner, subject, tmp_path):
    class WithFresh(SlotCountingFleet):
        def __call__(self, argv, **kwargs):
            result = super().__call__(argv, **kwargs)
            command = argv[3] if len(argv) > 3 else ""
            if "--target all" in command:
                body = json.loads(result.stdout)
                request = command.split("--request ")[1].split()[0]
                body["jobs"].append({"id": f"job-{request}-machine-fresh", "machine": "machine-fresh"})
                self.queued.append("machine-fresh")
                result.stdout = json.dumps(body)
            return result
    scripted = WithFresh()
    fleet = build(runner, scripted, vm_slots=2, fresh="fresh")
    # What fresh_install leaves behind: a Mac this run installed and adopted.
    fleet.installed.add("fresh")
    fleet._ids["fresh"] = "machine-fresh"
    wait = fleet.hub.wait_for
    def checked_wait(state, machine, **kwargs):
        if machine == "machine-fresh":
            assert "fresh" in scripted.booted, "waiting for a parked fresh Mac cannot finish"
            assert not [c for c in scripted.calls if c[1:3] == ["reset", "fresh"]], \
                "the adopted fresh Mac was reset on its way back: no Mac answers for its row"
        return wait(state, machine, **kwargs)
    fleet.hub.wait_for = checked_wait
    run = acceptance.Run(tmp_path / "receipts", IDENTITY)
    with run.journey("fleet") as journey:
        fleet.cast(*runner.CAST["fleet"](fleet))
        runner.fleet_journey(journey, fleet, subject)
    assert run.results["fleet"] == "passed"
    assert scripted.peak <= 2
    assert scripted.forgotten == [], "a fixture with no strangers forgets nothing"


def test_a_fresh_mac_no_journey_installed_on_is_not_waited_for(runner, subject, tmp_path):
    """`--only fleet`: the fresh guest is reset at its cast and pristine. A hub
    that still queues a job for the row it left last run has a stranger in
    its fleet, and the journey says so instead of waiting on a Mac that is
    not there."""
    class WithGhost(SlotCountingFleet):
        def __call__(self, argv, **kwargs):
            result = super().__call__(argv, **kwargs)
            command = argv[3] if len(argv) > 3 else ""
            if "--target all" in command:
                body = json.loads(result.stdout)
                body["jobs"].append({"id": "job-stale", "machine": "machine-fresh-of-last-run"})
                result.stdout = json.dumps(body)
            return result
    scripted = WithGhost()
    fleet = build(runner, scripted, vm_slots=2, fresh="fresh")
    result, receipt = journey_result(runner, subject, "fleet", scripted, tmp_path,
                                     vm_slots=2, fresh="fresh")
    assert result == "failed" and "outside this fixture" in receipt["detail"]
    assert "machine-fresh-of-last-run" in receipt["detail"]


def test_rows_no_guest_answers_for_are_forgotten_before_update_all(runner, subject, tmp_path):
    """A hub kept across runs holds a row per past run for the reset fresh
    Mac. Each is a job Update All would queue forever; they leave by the
    product's forget before the fleet is asked, and the receipt names them."""
    scripted = ScriptedFleet()
    scripted.ghosts = ["machine-fresh-run-7", "machine-fresh-run-8"]
    result, receipt = journey_result(runner, subject, "fleet", scripted, tmp_path)
    assert result == "passed", receipt["detail"]
    assert scripted.forgotten == ["machine-fresh-run-7", "machine-fresh-run-8"]
    assert "machine-hub" not in scripted.forgotten
    log = (tmp_path / "receipts" / "fleet.log").read_text()
    assert "forgot 2 machine(s)" in log and "machine-fresh-run-7" in log


def test_a_plan_without_slots_keeps_every_guest_running(runner, subject, tmp_path):
    scripted = SlotCountingFleet()
    fleet = build(runner, scripted)
    run = acceptance.Run(tmp_path / "receipts", IDENTITY)
    with run.journey("fleet") as journey:
        fleet.cast(*runner.CAST["fleet"](fleet))
        runner.JOURNEYS["fleet"](journey, fleet, subject)
    assert run.results["fleet"] == "passed"
    assert not any(call[1] == "stop" for call in scripted.calls), \
        "without vm_slots nothing may be parked"


def test_a_plan_that_is_not_marked_disposable_is_refused(runner, tmp_path, monkeypatch):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"vm_tool": "/bin/vm.sh", "hub": "production-hub"}))
    monkeypatch.setattr("sys.argv", ["accept", "--ref", "dev",
                                     "--receipts", str(tmp_path / "r"), "--plan", str(plan)])
    with pytest.raises(SystemExit) as exit_code:
        runner.main()
    assert exit_code.value.code == 2


@pytest.mark.parametrize("role,text,holders,passes", [
    ("assistant", "accepted abcdef12", [{"pid": 11, "started": 10}], True),
    ("user", "accepted abcdef12", [{"pid": 11, "started": 10}], False),
    ("assistant", "old output", [{"pid": 11, "started": 10}], False),
    ("assistant", "accepted abcdef12", [{"pid": 11, "started": 99}], False),
])
def test_new_input_requires_fresh_assistant_output_from_same_process(
        runner, monkeypatch, role, text, holders, passes):
    from types import SimpleNamespace
    monkeypatch.setattr(runner.uuid, "uuid4", lambda: SimpleNamespace(hex="abcdef12"))
    ticks = iter([0, 1, 301])
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)

    class Guest:
        def tool_call(self, *args):
            if "--after" not in args:
                return {"session": "sid", "cursor": 4, "holders": [{"pid": 11, "started": 10}]}
            assert args[-1] == "4"
            return {"session": "sid", "cursor": 5, "holders": holders,
                    "messages": [{"role": role, "text": text}]}

        def call(self, path, body, **kwargs):
            assert path == "/sessions/sid/input"
            return {"status": 200, "body": '{"ok":true}'}

    if passes:
        assert runner.send_to_session(Guest(), "sid")["marker"] == "abcdef12"
    else:
        with pytest.raises(runner.AcceptanceFailure):
            runner.send_to_session(Guest(), "sid")


def test_unrelated_process_cannot_prove_a_new_session(runner, monkeypatch):
    ticks = iter([0, 1, 241])
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)

    class Guest:
        def tool_call(self, action, *args, **kwargs):
            if action == "spawn":
                return {"session": "wanted"}
            return {"session": "other", "holders": [{"pid": 1, "started": 1}]}

    with pytest.raises(runner.AcceptanceFailure, match="identified provider"):
        runner.new_session(Guest())


def test_fresh_install_refuses_existing_menu_before_any_write(runner, subject):
    class Guest:
        name = "fresh"
        def sh(self, command):
            assert "for p in" in command
            return "/Applications/JStack Host.app"

    with pytest.raises(runner.AcceptanceFailure, match="not pristine"):
        runner.install_build(Guest(), subject, fresh=True)


def test_the_installer_is_fetched_from_the_ref_and_clones_it(runner, subject, monkeypatch):
    commands = []
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    class Guest:
        name = "fresh"
        def sh(self, command, **kwargs):
            commands.append(command)
            # What a just-installed host answers the lab-flag probe: turned on now.
            return "lab=4" if "candidate_test" in command else ""
        def copy(self, *args):
            pass

    runner.install_build(Guest(), subject, fresh=True)
    # Nothing of the product is carried in: no stack tarball, no untar, no
    # sealed Hub. The guest fetches the installer from the commit and clones.
    assert not any("tar -xzf" in command for command in commands)
    fetch = next(command for command in commands if "curl" in command)
    assert subject.sha in fetch and "raw.githubusercontent.com" in fetch
    install = next(command for command in commands if "install.sh --yes" in command)
    assert "--no-claude --no-app" in install and "--ref dev" in install
    assert "JSTACK_REPO_URL=" + REPO in install
    assert not any("menubar/install.sh" in command for command in commands)
    # The Mac app is the one exception and it lands *before* the installer,
    # which names an already-installed jRemote.app as the client of its build.
    app = next(command for command in commands if "ditto -x -k" in command)
    assert "/Applications" in app and commands.index(app) < commands.index(install)
    # The sealed provisioner writes candidate_test false; the lab flips it
    # and restarts the updater so the run verifies unpromoted envelopes.
    flip = next(command for command in commands if "candidate_test" in command)
    assert "True" in flip
    assert commands.index(flip) > commands.index(install)
    assert any("kickstart" in command and "live.jstack.hub.updater" in command
               for command in commands)


def test_a_build_reads_what_the_commit_declares_not_the_working_tree(runner, tmp_path, monkeypatch):
    made = a_build(runner, monkeypatch, tmp_path, "dev", HEAD_SHA, version="9.9.9")
    assert made.sha == HEAD_SHA and made.version("stack") == "9.9.9"
    assert made.slug == "dev@" + HEAD_SHA[:8]
    assert made.raw_url == (
        "https://raw.githubusercontent.com/jenyalebid/jStack/" + HEAD_SHA + "/install.sh")


def test_a_ref_the_repo_does_not_carry_is_named_not_guessed(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    with pytest.raises(runner.AcceptanceFailure, match="has no branch nope"):
        runner.Build(REPO, "nope", checkout=tmp_path)


def test_new_session_with_a_provider_but_no_reply_fails(runner, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)

    class Guest:
        def tool_call(self, action, *args, **kwargs):
            if action == "spawn":
                return {"session": "wanted"}
            return {"session": "wanted", "holders": [{"pid": 1, "started": 1}],
                    "messages": [{"role": "assistant", "text": "/Users/admin/Agents/update-proof"}]}

    def no_reply(*args):
        raise runner.AcceptanceFailure("provider login expired")

    monkeypatch.setattr(runner, "send_to_session", no_reply)
    with pytest.raises(runner.AcceptanceFailure, match="login expired"):
        runner.new_session(Guest())


def test_selected_journey_retains_existing_exact_artifact_receipts(
        runner, subject, tmp_path, monkeypatch):
    receipts = tmp_path / "receipts"
    run = acceptance.Run(receipts, IDENTITY)
    with run.journey("upgrade") as journey:
        for check in acceptance.REQUIRED["upgrade"]:
            # One value under every check is what a pasted receipt looks like,
            # and the gate now refuses it. A fixture standing in for a run has
            # to answer each check with that check's own answer.
            journey.observe(check, "observed " + check)
    original = (receipts / "upgrade.json").read_bytes()
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"disposable": True, "vm_tool": "/bin/vm.sh", "hub": "hub",
                                "leaves": ["leaf-a", "leaf-b"]}))
    monkeypatch.setattr(runner, "Build", lambda *args, **kwargs: subject)
    fleet = build(runner, ScriptedFleet())
    monkeypatch.setattr(fleet, "offer", lambda _: CANDIDATE)
    monkeypatch.setattr(runner, "Fleet", lambda *args, **kwargs: fleet)
    monkeypatch.setattr("sys.argv", ["accept", "--ref", "dev",
                                     "--receipts", str(receipts), "--plan", str(plan),
                                     "--only", "fleet"])
    assert runner.main() == 1  # Other required journeys still missing.
    assert (receipts / "upgrade.json").read_bytes() == original
    assert acceptance.inspect(receipts, IDENTITY)["upgrade"]["state"] == "passed"


def test_staging_prior_puts_the_ref_under_test_back_on_update_failure(
        runner, subject, tmp_path, monkeypatch):
    fleet = build(runner, ScriptedFleet(release=CANDIDATE, sha=HEAD_SHA))
    fleet.build = subject
    offered = []
    monkeypatch.setattr(fleet, "offer", lambda b: offered.append(b.sha))

    def failed(*args, **kwargs):
        raise runner.AcceptanceFailure("update failed")

    monkeypatch.setattr(fleet.hub, "wait_for", failed)
    with pytest.raises(runner.AcceptanceFailure, match="update failed"):
        runner.stage_prior(fleet, fleet.leaves[0])
    assert offered == [PRIOR_SHA, HEAD_SHA]


class KeyedFleet(ScriptedFleet):
    """A hub that signs with its own key, over leaves that may or may not hold it.

    `keys` is what each guest's updater trusts. An install lands the prior and
    mints that machine its own key, as building from a checkout does; adoption
    hands the leaf the hub's key when `learns` — which is what a leaf on code
    from a9fe663 on does on its next heartbeat, and what no earlier leaf can.
    """

    def __init__(self, *, keys, learns=True, revoked=(), **kwargs):
        super().__init__(**kwargs)
        self.keys, self.learns = dict(keys), learns
        self.installed: list[str] = []
        self.adopted: list[str] = []
        #: Guests whose credential the hub has revoked; adoption mints a new one.
        self.revoked: set[str] = set(revoked)

    def __call__(self, argv, **kwargs):
        _, action, name, *rest = argv
        command = rest[0] if rest else ""
        if action == "ssh" and "public_key" in command:
            self.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, self.keys.get(name, "") + "\n", "")
        if action == "ssh" and "parent.json" in command:
            self.calls.append(list(argv))
            device = "" if name == "hub" or not self.keys.get(name) else "device-" + name
            return subprocess.CompletedProcess(argv, 0, device + "\n", "")
        if action == "ssh" and "--path /devices" in command:
            self.calls.append(list(argv))
            rows = [{"id": "device-" + guest, "revoked": guest in self.revoked}
                    for guest in self.keys if guest != "hub"]
            answer = {"status": 200, "body": json.dumps({"devices": rows})}
            return subprocess.CompletedProcess(argv, 0, json.dumps(answer), "")
        if action == "ssh" and "install.sh --yes" in command:
            self.installed.append(name)
            self.keys[name] = "own-" + name
            self.release, self.sha = PRIOR, PRIOR_SHA
        if action == "ssh" and "adopt-to-hub" in command:
            self.adopted.append(name)
            self.revoked.discard(name)
            if self.learns:
                self.keys[name] = self.keys["hub"]
        done = super().__call__(argv, **kwargs)
        if action == "ssh" and "queue" in command and "--path" not in command:
            self.release, self.sha = PRIOR, PRIOR_SHA  # a staging job lands the prior
        return done


class LinedFleet(ScriptedFleet):
    """A hub with lines: every inventory row names the line its Mac's own
    heartbeat carries, main when the heartbeat names none.

    `updates channel` on a leaf writes its config; the kicked updater then
    says the new line on its heartbeat — unless `says_line` is off, which is
    every updater from before lines: its config takes the name, its heartbeat
    carries nothing, and the hub keeps its row on main.
    """

    def __init__(self, *, lines=None, says_line=True, **kwargs):
        super().__init__(**kwargs)
        self.lines: dict[str, str] = dict(lines or {})
        self.says_line = says_line
        self.channels: list[tuple[str, str]] = []

    def __call__(self, argv, **kwargs):
        _, action, name, *rest = argv
        command = rest[0] if rest else ""
        if action == "ssh" and "updates channel " in command:
            self.calls.append(list(argv))
            line = command.split("updates channel ")[1].split()[0]
            self.channels.append((name, line))
            if self.says_line:
                self.lines["machine-" + name] = line
            return subprocess.CompletedProcess(argv, 0, line + "\n", "")
        done = super().__call__(argv, **kwargs)
        if action == "ssh" and "inventory" in command:
            answer = json.loads(done.stdout)
            for row in answer["machines"]:
                row["line"] = self.lines.get(row["machine"], "main")
            return subprocess.CompletedProcess(argv, 0, json.dumps(answer), "")
        return done


def test_a_build_s_offer_is_its_own_line_s_or_main_s(runner):
    assert runner.line_of("main") == "main" and runner.line_of("dev") == "dev"
    # A debug build of any other ref lands in main's feed.
    assert runner.line_of("release/26.9.1") == "main"
    assert runner.line_of("feature/x") == "main"


def test_a_leaf_on_main_is_moved_onto_the_candidate_s_line_before_its_upgrade(
        runner, subject, tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    scripted = LinedFleet()
    result, receipt = journey_result(runner, subject, "upgrade", scripted, tmp_path)
    assert result == "passed"
    assert scripted.channels == [("leaf-a", "dev")] and "leaf-a" in scripted.kicked
    commands = [c[3] for c in scripted.calls if len(c) > 3]
    channel = next(i for i, c in enumerate(commands) if "updates channel dev" in c)
    queue = next(i for i, c in enumerate(commands) if "queue" in c)
    assert channel < queue, "the line is chosen before the hub is asked to update the leaf"


def test_a_leaf_already_on_the_line_is_left_alone(runner, subject, tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    scripted = LinedFleet(lines={"machine-leaf-a": "dev"})
    result, _ = journey_result(runner, subject, "upgrade", scripted, tmp_path)
    assert result == "passed"
    assert scripted.channels == [] and scripted.kicked == []


def test_a_leaf_whose_heartbeat_names_no_line_fails_by_name(
        runner, subject, tmp_path, monkeypatch):
    """An updater from before lines writes the channel it is told and reports
    none, so the hub keeps it on main and can only ever offer it main's
    build. The failure names that, instead of a job that settles current on
    the commit the leaf already ran."""
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    monkeypatch.setattr(runner, "LINE_PATIENCE", 0)
    scripted = LinedFleet(says_line=False)
    result, receipt = journey_result(runner, subject, "upgrade", scripted, tmp_path)
    assert result == "failed"
    assert "reports no line" in receipt["detail"]
    assert "dev@" + HEAD_SHA[:8] + " is not main's" in receipt["detail"]
    assert scripted.channels == [("leaf-a", "dev")]
    assert scripted.queued == [], "nothing is queued for a leaf that cannot take the offer"


def test_a_hub_before_lines_lists_no_line_and_tells_no_leaf_one(runner, subject, tmp_path):
    scripted = ScriptedFleet()
    result, _ = journey_result(runner, subject, "upgrade", scripted, tmp_path)
    assert result == "passed"
    assert not any("updates channel" in c[3] for c in scripted.calls if len(c) > 3)


def test_the_hub_itself_is_never_told_a_line(runner, subject, tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    scripted = LinedFleet()
    result, _ = journey_result(runner, subject, "fleet", scripted, tmp_path)
    assert result == "passed"
    assert "machine-hub" in scripted.queued, "the hub updates itself by its own queue"
    assert scripted.channels == [("leaf-a", "dev")], "a leaf follows; the hub is never told"


class LinedKeyedFleet(LinedFleet, KeyedFleet):
    pass


def test_staging_the_prior_puts_a_leaf_on_dev_back_on_the_prior_s_line(
        runner, subject, earlier, tmp_path, monkeypatch):
    """The prior's offer is main's (a line, or a debug build filed there), so
    a leaf left on dev by an earlier journey is moved first — or the hub would
    answer its queue with dev's offer, the candidate it already runs."""
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    monkeypatch.setattr(runner, "KEY_PATIENCE", 0)
    scripted = LinedKeyedFleet(release=CANDIDATE, sha=HEAD_SHA,
                               keys={"hub": "hub-key", "leaf-a": "hub-key"},
                               lines={"machine-leaf-a": "dev"})
    fleet = build(runner, scripted, prior=earlier,
                  adopt_command="/bin/bash ~/adopt-to-hub.sh hub.local")
    fleet.build = subject
    offered = []
    monkeypatch.setattr(fleet, "offer", lambda b: offered.append(b.sha))
    state = runner.stage_prior(fleet, fleet.leaves[0])
    assert state["sha"] == PRIOR_SHA
    assert offered == [PRIOR_SHA, HEAD_SHA]
    assert scripted.channels == [("leaf-a", "main")]


def _keyed(runner, monkeypatch, tmp_path, earlier, subject, **kwargs):
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    monkeypatch.setattr(runner, "KEY_PATIENCE", 0)
    scripted = KeyedFleet(release=CANDIDATE, sha=HEAD_SHA, **kwargs)
    fleet = build(runner, scripted, prior=earlier,
                  adopt_command="/bin/bash ~/adopt-to-hub.sh hub.local")
    fleet.build = subject
    offered = []
    monkeypatch.setattr(fleet, "offer", lambda b: offered.append(b.sha))
    return scripted, fleet, offered


def test_a_leaf_that_trusts_the_hub_is_staged_by_the_hub(runner, subject, earlier, tmp_path, monkeypatch):
    scripted, fleet, offered = _keyed(runner, monkeypatch, tmp_path, earlier, subject,
                                      keys={"hub": "hub-key", "leaf-a": "hub-key"})
    runner.stage_prior(fleet, fleet.leaves[0])
    assert offered == [PRIOR_SHA, HEAD_SHA]
    assert scripted.installed == [] and scripted.adopted == []


def test_a_leaf_that_cannot_follow_the_hub_takes_the_prior_by_the_one_file_install(
        runner, subject, earlier, tmp_path, monkeypatch):
    """A Mac installed from a published release trusts that release's key and
    nothing the hub signs (#144): the hub can serve it nothing, so it is
    moved as such a Mac is moved — installed onto the prior, then adopted."""
    scripted, fleet, offered = _keyed(runner, monkeypatch, tmp_path, earlier, subject,
                                      keys={"hub": "hub-key", "leaf-a": "published-key"})
    notes = SimpleNamespace(lines=[], note=lambda text: notes.lines.append(text))
    state = runner.stage_prior(fleet, fleet.leaves[0], journey=notes)
    assert state["sha"] == PRIOR_SHA
    assert offered == [], "nothing the hub serves can reach an untrusting leaf"
    assert scripted.installed == ["leaf-a"] and scripted.adopted == ["leaf-a"]
    install = next(c[3] for c in scripted.calls if "install.sh --yes" in (c[3] if len(c) > 3 else ""))
    assert "--ref main" in install
    assert any("#144" in line and HEAD_SHA in line for line in notes.lines)
    assert scripted.keys["leaf-a"] == "hub-key"


def test_a_leaf_that_never_takes_the_hubs_key_fails_by_name(runner, subject, earlier, tmp_path, monkeypatch):
    scripted, fleet, offered = _keyed(runner, monkeypatch, tmp_path, earlier, subject,
                                      keys={"hub": "hub-key", "leaf-a": "published-key"},
                                      learns=False)
    with pytest.raises(runner.AcceptanceFailure, match="never took the hub's key.*#144"):
        runner.stage_prior(fleet, fleet.leaves[0])
    assert scripted.installed == ["leaf-a"] and scripted.adopted == ["leaf-a"]
    assert offered == []


def test_a_cast_leaf_the_hub_cannot_reach_is_moved_before_its_journey(
        runner, subject, earlier, tmp_path, monkeypatch):
    scripted, fleet, offered = _keyed(runner, monkeypatch, tmp_path, earlier, subject,
                                      keys={"hub": "hub-key", "leaf-a": "published-key",
                                            "leaf-b": "hub-key", "fresh": ""})
    fleet.fresh = runner.Guest("fresh", Path("/bin/vm.sh"), run=scripted)
    fleet.cast(fleet.hub, fleet.leaves[0])
    assert scripted.installed == ["leaf-a"] and scripted.adopted == ["leaf-a"]
    assert scripted.keys["leaf-a"] == "hub-key" and offered == []
    # A leaf that already follows, and a Mac with no host at all, are left alone.
    fleet.cast(fleet.hub, fleet.leaves[1])
    fleet.cast(fleet.hub, fleet.fresh)
    assert scripted.installed == ["leaf-a"] and scripted.adopted == ["leaf-a"]


def test_a_cast_leaf_the_hub_revoked_is_adopted_again_before_its_journey(
        runner, subject, earlier, tmp_path, monkeypatch):
    """The revocation journey leaves its leaf revoked on the hub, and a run
    that casts that leaf next would queue it a job the hub refuses (409,
    `machine credential is revoked`). It is adopted again, as a revoked Mac
    is brought back for real: no reinstall, since the Mac runs fine."""
    scripted, fleet, offered = _keyed(runner, monkeypatch, tmp_path, earlier, subject,
                                      keys={"hub": "hub-key", "leaf-a": "hub-key",
                                            "leaf-b": "hub-key"}, revoked={"leaf-b"})
    fleet.cast(fleet.hub, fleet.leaves[1])
    assert scripted.adopted == ["leaf-b"] and scripted.installed == []
    assert scripted.revoked == set() and offered == []
    # A leaf the hub still honours is left alone, and so is the hub itself.
    fleet.cast(fleet.hub, fleet.leaves[0])
    assert scripted.adopted == ["leaf-b"]


def test_a_cast_leaf_that_detached_is_adopted_again_before_its_journey(
        runner, subject, earlier, tmp_path, monkeypatch):
    """Run 20260925-051215: shell_detach had taken acc-leaf1 apart the run
    before (detach drops the parent record and the machine identity; the host
    stays), and the rerun's shell_flip read `cat id_jremote.pub` on it — no
    such file. A leaf with a host and no parent record is adopted again
    before its journey, as a detached Mac is brought back: no reinstall."""
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    monkeypatch.setattr(runner, "KEY_PATIENCE", 0)

    class Detached(KeyedFleet):
        def probe(self, name):
            answer = super().probe(name)
            answer["adopted"] = name != "leaf-a" or name in self.adopted
            return answer

    scripted = Detached(release=CANDIDATE, sha=HEAD_SHA,
                        keys={"hub": "hub-key", "leaf-a": "hub-key", "leaf-b": "hub-key"})
    fleet = build(runner, scripted, prior=earlier,
                  adopt_command="/bin/bash ~/adopt-to-hub.sh hub.local")
    fleet.build = subject
    offered = []
    monkeypatch.setattr(fleet, "offer", lambda b: offered.append(b.sha))
    fleet.cast(fleet.hub, fleet.leaves[0])
    assert scripted.adopted == ["leaf-a"] and scripted.installed == []
    assert offered == [], "the leaf already runs the ref under test; nothing is staged"
    # Adopted, it is a fleet member again and left alone; so is a leaf that never detached.
    fleet.cast(fleet.hub, fleet.leaves[0])
    fleet.cast(fleet.hub, fleet.leaves[1])
    assert scripted.adopted == ["leaf-a"]


def test_a_cast_leaf_on_a_build_this_run_never_named_is_staged_onto_the_prior(
        runner, subject, earlier, tmp_path, monkeypatch):
    """Run 14, acc-leaf2: the leaf followed the hub and still ran the prior of
    an earlier run, so reach() left it there, and the fleet journey measured
    an update from a commit this run never named — which failed on that
    commit's own defect. A following leaf on neither the earlier ref nor the
    ref under test is staged onto the earlier ref before its journey."""
    scripted, fleet, offered = _keyed(runner, monkeypatch, tmp_path, earlier, subject,
                                      keys={"hub": "hub-key", "leaf-a": "hub-key",
                                            "leaf-b": "hub-key"})
    scripted.release, scripted.sha = "2026-09-14-33333333-3333333333333333", "3" * 40
    fleet.cast(fleet.hub, fleet.leaves[1])
    assert offered == [PRIOR_SHA, HEAD_SHA], "staged by the hub, which then holds the ref under test again"
    assert scripted.installed == [] and scripted.adopted == []
    assert scripted.sha == PRIOR_SHA
    # Now on the prior: cast again and it is left alone.
    fleet.cast(fleet.hub, fleet.leaves[1])
    assert offered == [PRIOR_SHA, HEAD_SHA]


def test_an_adoption_that_leaves_the_leaf_revoked_fails_by_name(
        runner, subject, earlier, tmp_path, monkeypatch):
    scripted, fleet, _ = _keyed(runner, monkeypatch, tmp_path, earlier, subject,
                                keys={"hub": "hub-key", "leaf-a": "hub-key"}, revoked={"leaf-a"})
    adopted = []
    monkeypatch.setattr(runner, "adopt", lambda fleet, guest: adopted.append(guest.name))
    with pytest.raises(runner.AcceptanceFailure, match="still revoked"):
        fleet.cast(fleet.hub, fleet.leaves[0])
    assert adopted == ["leaf-a"]


def test_the_hub_runs_what_it_built_before_any_journey(runner, subject, tmp_path, monkeypatch):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"disposable": True, "vm_tool": "/bin/vm.sh", "hub": "hub",
                                "leaves": ["leaf-a", "leaf-b"]}))
    monkeypatch.setattr(runner, "Build", lambda *args, **kwargs: subject)
    scripted = ScriptedFleet()
    fleet = build(runner, scripted)
    monkeypatch.setattr(fleet, "offer", lambda _: CANDIDATE)
    monkeypatch.setattr(runner, "Fleet", lambda *args, **kwargs: fleet)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    monkeypatch.setattr("sys.argv", ["accept", "--ref", "dev", "--receipts", str(tmp_path / "r"),
                                     "--plan", str(plan), "--only", "fresh_install"])
    runner.main()
    assert scripted.queued[0] == "machine-hub", "the hub takes its own build before any leaf is cast"


def feed(tmp_path, channel, files):
    """A hub's updates config and feed: `files` maps feed name -> release."""
    feed_dir = tmp_path / "feed"; feed_dir.mkdir(exist_ok=True)
    for name, release in files.items():
        (feed_dir / name).write_text(json.dumps({"manifest": {
            "release": release, "components": {"client": {"version": 70}}}}))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"channel": channel, "feed_dir": str(feed_dir)}))
    return config


def served(runner, config):
    out = subprocess.run([sys.executable, "-c", runner.SERVED_PY, str(config)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_the_served_probe_reads_the_one_feed_a_hub_before_lines_keeps(runner, tmp_path):
    """The run's first probe lands on the prior hub, told to follow dev and
    to build it. A prior that predates lines has no `latest-dev.json`: what it
    built is in `latest.json`, and the probe reads it there — 26.9.1's proof
    died on the missing file before any receipt (2026-09-26)."""
    config = feed(tmp_path, "dev", {"latest.json": PRIOR})
    assert served(runner, config) == {"build": PRIOR, "client": "70"}


def test_the_served_probe_prefers_the_lines_own_feed_where_the_hub_has_one(runner, tmp_path):
    config = feed(tmp_path, "dev", {"latest.json": PRIOR, "latest-dev.json": CANDIDATE})
    assert served(runner, config)["build"] == CANDIDATE
    config = feed(tmp_path, "main", {"latest.json": PRIOR, "latest-dev.json": CANDIDATE})
    assert served(runner, config)["build"] == PRIOR


def test_the_hub_is_driven_through_the_refusal_older_code_raises(runner, subject):
    """A hub on code before a9fe663 refuses to build over adopted machines;
    its own hatch is the variable, which a hub past the fix ignores."""
    class Building(ScriptedFleet):
        def __call__(self, argv, **kwargs):
            command = argv[3] if len(argv) > 3 else ""
            if "updates build" in command:
                self.calls.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, json.dumps({"release": CANDIDATE}), "")
            return super().__call__(argv, **kwargs)

    scripted = Building(served_client="68")
    fleet = build(runner, scripted)
    assert fleet.offer(subject) == CANDIDATE
    built = next(c[3] for c in scripted.calls if "updates build" in c[3])
    assert built.startswith("JSTACK_BUILD_DESPITE_LEAVES=1 ") and "updates build --ref dev" in built
    # The build's answer names no parts; the client it carries is read off
    # the feed the hub now serves, and it is the hub's, not this run's.
    assert fleet.served["dev"] == "68" and subject.version("client") == "70"


@pytest.mark.parametrize("help_text,flag", [("  --debug  a debug build", " --debug"),
                                            ("  --ref REF", "")])
def test_a_feature_branch_is_built_as_a_debug_build_where_the_hub_knows_one(
        runner, subject, help_text, flag):
    """A hub builds a release only off main or dev; the branch under test is
    a debug build. A hub whose CLI predates the flag builds any ref without it."""
    class Building(ScriptedFleet):
        def __call__(self, argv, **kwargs):
            command = argv[3] if len(argv) > 3 else ""
            if "updates build --help" in command:
                return subprocess.CompletedProcess(argv, 0, help_text, "")
            if "updates build" in command:
                self.calls.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, json.dumps({"release": CANDIDATE}), "")
            return super().__call__(argv, **kwargs)

    scripted = Building()
    subject.ref = "feature/x"
    fleet = build(runner, scripted)
    fleet.offer(subject)
    built = next(c[3] for c in scripted.calls if "updates build --ref" in c[3])
    assert built.endswith("updates build --ref feature/x" + flag)


def test_a_hub_whose_feed_does_not_serve_what_it_built_fails_the_offer(runner, subject):
    class Elsewhere(ScriptedFleet):
        def __call__(self, argv, **kwargs):
            command = argv[3] if len(argv) > 3 else ""
            if "updates build" in command:
                return subprocess.CompletedProcess(argv, 0, json.dumps({"release": "other"}), "")
            return super().__call__(argv, **kwargs)

    fleet = build(runner, Elsewhere())
    with pytest.raises(runner.AcceptanceFailure, match="built but serves"):
        fleet.offer(subject)


def test_artifact_fault_is_restored_when_refusal_probe_fails(runner):
    from types import SimpleNamespace
    calls = []
    class Hub:
        def tool_call(self, action):
            calls.append(action)
        def call(self, *args):
            raise runner.AcceptanceFailure("hub disconnected")

    with pytest.raises(runner.AcceptanceFailure, match="disconnected"):
        runner.refused_build(SimpleNamespace(plan={}, hub=Hub()),
                             SimpleNamespace(installed=lambda: {"build": PRIOR}), "leaf")
    assert calls == ["tamper", "restore-artifact"]


@pytest.mark.parametrize("detail,valid", [("artifact digest mismatch", True),
                                         ("host unavailable", False)])
def test_hub_refusal_requires_artifact_error_and_unchanged_installation(runner, detail, valid):
    from types import SimpleNamespace
    state = dict(build=PRIOR, sha=PRIOR_SHA, client="69", menubar="69")
    guest = SimpleNamespace(installed=lambda: state)
    hub = SimpleNamespace(tool_call=lambda _: None,
                          call=lambda *args: {"status": 503, "body": detail})
    fleet = SimpleNamespace(plan={}, hub=hub)
    if valid:
        assert runner.refused_build(fleet, guest, "leaf")["unchanged_build"] == PRIOR
    else:
        with pytest.raises(runner.AcceptanceFailure, match="unrelated"):
            runner.refused_build(fleet, guest, "leaf")


def test_revocation_failure_restores_the_fixture_supervisor(runner, monkeypatch):
    from types import SimpleNamespace
    commands = []
    guest = SimpleNamespace(sh=lambda command: commands.append(command))
    monkeypatch.setattr(runner, "stage_prior", lambda *args, **kwargs: {})

    def fail(*args):
        raise runner.AcceptanceFailure("credential revocation failed")

    hub = SimpleNamespace(queue=lambda *args: {"jobs": [{"id": "queued"}]}, tool_call=fail,
                          row=lambda machine: {"machine": machine, "state": "current"})
    fleet = SimpleNamespace(leaves=[guest], machine=lambda _: "leaf", hub=hub)
    with pytest.raises(runner.AcceptanceFailure, match="revocation failed"):
        runner.revocation(SimpleNamespace(observe=lambda *args: None), fleet, None)
    assert commands[0].endswith("unregister updater")
    assert commands[-2].endswith("register updater")
    assert "launchctl print" in commands[-1]


def test_a_hub_restarting_into_the_new_build_survives_refused_polls(runner):
    fleet = ScriptedFleet(state="current")
    polls = {"refused": 2}

    def run(argv, **kwargs):
        if "inventory" in (argv[3] if len(argv) > 3 else "") and polls["refused"]:
            polls["refused"] -= 1
            return subprocess.CompletedProcess(argv, 1, "", "[Errno 61] Connection refused")
        return fleet(argv, **kwargs)

    hub = runner.Guest("hub", Path("/bin/vm.sh"), run=run)
    row = hub.wait_for("current", "machine-hub", timeout=5, poll=0)
    assert row["state"] == "current"
    assert polls["refused"] == 0


def test_a_hub_that_never_answers_again_fails_at_the_deadline(runner):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "[Errno 61] Connection refused")

    hub = runner.Guest("hub", Path("/bin/vm.sh"), run=run)
    with pytest.raises(runner.AcceptanceFailure, match="stopped answering"):
        hub.wait_for("current", "machine-hub", timeout=0.2, poll=0)


def _bypass_guest(runner, *, answers_after_nudge: bool, prompt_up: bool = True):
    """A guest whose spawned session sits on Claude's bypass warning until
    something presses Down+Enter in its pane, and then at an empty prompt
    until something delivers the request — the published ffe85dc9 prior,
    whose startup helpers never run on the sealed bundle."""
    state = {"nudged": [], "reprompted": [], "answered": False, "accepted": False}

    class Guest:
        def tool_call(self, action, *args, **kwargs):
            if action == "spawn":
                return {"session": "65e5d97c-1733"}
            messages = ([{"role": "assistant", "text": "/Users/admin/Agents/update-proof"}]
                        if state["answered"] else [])
            return {"session": "65e5d97c-1733", "holders": [{"pid": 1676, "started": 1}],
                    "messages": messages}

        def sh(self, command, **kwargs):
            assert "-L jremote" in command and "jr-65e5d97c" in command
            if "capture-pane" in command:
                return ("  ❯ No, exit\n    Yes, I accept\n"
                        if prompt_up and not state["accepted"] else "bypass permissions on\n")
            assert "send-keys" in command and "Down" in command and "Enter" in command
            state["nudged"].append(command)
            state["accepted"] = True
            return ""

        def call(self, path, body=None, **kwargs):
            assert path == "/sessions/65e5d97c-1733/input" and "run pwd once" in body["text"]
            assert state["accepted"] or not prompt_up, "a request typed under the warning is lost"
            state["reprompted"].append(body["text"])
            state["answered"] = answers_after_nudge
            return {"status": 200, "body": "{}"}

    return Guest(), state


def test_a_prior_stuck_on_the_bypass_warning_is_answered_and_recorded(runner, monkeypatch):
    ticks = iter([0, 1, 10, 31, 32, 40, 41])
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    monkeypatch.setattr(runner, "send_to_session", lambda guest, session: {"session": session})
    guest, state = _bypass_guest(runner, answers_after_nudge=True)

    session = runner.new_session(guest, prior=True)

    assert session["nudged"] is True and len(state["nudged"]) == 1
    assert session["reprompted"] is True and len(state["reprompted"]) == 1
    assert session["pid"] == 1676


def test_a_new_build_session_left_on_the_bypass_warning_fails_by_name(runner, monkeypatch):
    ticks = iter([0, 1, 10, 31, 241, 242])
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    guest, state = _bypass_guest(runner, answers_after_nudge=True)

    with pytest.raises(runner.AcceptanceFailure, match="startup watcher never answered"):
        runner.new_session(guest)
    assert state["nudged"] == [], "the runner must never press keys into a new build's session"
    assert state["reprompted"] == [], "the runner must never feed a new build's session either"


def test_a_prior_is_not_nudged_before_the_grace_period(runner, monkeypatch):
    ticks = iter([0, 1, 5, 10, 20, 241, 242])
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    guest, state = _bypass_guest(runner, answers_after_nudge=True)

    with pytest.raises(runner.AcceptanceFailure, match="initial pwd request"):
        runner.new_session(guest, prior=True)
    assert state["nudged"] == []


def test_session_survival_requires_an_unaided_session_on_the_new_build(runner):
    assert "built_new_session" in acceptance.REQUIRED["session_survival"]


def _booting_guest(runner, answers):
    """vm.sh for a guest whose `gui` returns on sshd; the API poll in the
    guest answers from `answers` (the script's own stdout), or exits 3."""
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "gui":
            return subprocess.CompletedProcess(argv, 0, "acc-leaf1 up at 192.168.2.54\n", "")
        assert argv[1] == "ssh" and "local_url" in argv[3] and "/api/jremote/v1/host" in argv[3]
        answer = answers.pop(0)
        if answer == "down":
            return subprocess.CompletedProcess(argv, 3, "down\n", "")
        return subprocess.CompletedProcess(argv, 0, answer + "\n", "")

    return runner.Guest("acc-leaf1", Path("/bin/vm.sh"), run=run), calls


def test_start_returns_only_once_the_guest_api_answers(runner):
    guest, calls = _booting_guest(runner, ["up 401 27s"])
    assert guest.start() == "acc-leaf1 up at 192.168.2.54"
    assert [argv[1] for argv in calls] == ["gui", "ssh"]
    assert "-lt 180" in calls[1][3], "the wait does not carry the boot budget"


def test_start_names_a_host_api_that_never_comes_up(runner):
    guest, _ = _booting_guest(runner, ["down"])
    with pytest.raises(runner.AcceptanceFailure, match="host API never answered within 180s"):
        guest.start()


def test_start_of_a_guest_without_a_host_does_not_wait(runner):
    guest, calls = _booting_guest(runner, ["no-host"])
    guest.start()
    assert len(calls) == 2 and 'exit 0' in calls[1][3]


def test_a_prior_whose_warning_was_answered_still_gets_its_request_delivered(runner, monkeypatch):
    """The nudge alone leaves the prior at an empty prompt (105 run 4): the
    request has to go in through the product's input route afterwards."""
    ticks = iter([0, 1, 10, 31, 32, 33, 40, 41])
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    monkeypatch.setattr(runner, "send_to_session", lambda guest, session: {"session": session})
    guest, state = _bypass_guest(runner, answers_after_nudge=True)

    runner.new_session(guest, prior=True)

    assert state["nudged"] and state["reprompted"], "both halves of the prior's start are the runner's"


def test_a_prior_that_shows_no_warning_still_gets_its_request_delivered(runner, monkeypatch):
    """A guest that accepted the warning in an earlier journey opens straight
    onto an empty prompt; the prior's typer is just as dead there (#116)."""
    ticks = iter([0, 1, 10, 31, 32, 40, 41])
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    monkeypatch.setattr(runner, "send_to_session", lambda guest, session: {"session": session})
    guest, state = _bypass_guest(runner, answers_after_nudge=True, prompt_up=False)

    session = runner.new_session(guest, prior=True)

    assert session["nudged"] is False and state["nudged"] == []
    assert session["reprompted"] is True and len(state["reprompted"]) == 1


def test_a_request_already_on_screen_is_not_typed_twice(runner, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    calls = []
    guest = SimpleNamespace(
        sh=lambda command, **kwargs: "❯ Use the shell tool to run pwd once\nbypass permissions on\n",
        call=lambda *args, **kwargs: calls.append(args))
    assert runner.reprompt(guest, "65e5d97c-1733") is None
    assert calls == []


TRUE_UPDATER = "/Applications/jStack Hub.app/Contents/MacOS/JStackRuntime\n"
STALE_UPDATER = "/Applications/jStack Hub.app.failed-job-x/Contents/MacOS/JStackRuntime\n"
#: What `update_supervisor.abandon` writes when a reboot cut an apply: the
#: reason, then which release the machine was left running.
SETTLED_DETAIL = ("an interrupted application could not be resumed; "
                  "the release it was running is untouched and still running")


def test_a_leaf_whose_updater_runs_from_a_moved_bundle_is_rebooted_and_recorded(runner):
    answers = iter([STALE_UPDATER, TRUE_UPDATER])
    power = []
    guest = SimpleNamespace(name="leaf", sh=lambda command, **kwargs: next(answers),
                            stop=lambda: power.append("stop"), start=lambda: power.append("start"))
    journey = acceptance.Journey("interruption")
    assert runner.ensure_true_updater(journey, guest) == TRUE_UPDATER.strip()
    assert power == ["stop", "start"]
    assert any("#119" in line and ".failed-job-x" in line for line in journey.lines)


def test_a_leaf_whose_updater_stays_stale_after_a_reboot_fails(runner):
    guest = SimpleNamespace(name="leaf", sh=lambda command, **kwargs: STALE_UPDATER,
                            stop=lambda: None, start=lambda: None)
    with pytest.raises(runner.AcceptanceFailure, match="after a reboot the updater still runs from"):
        runner.ensure_true_updater(acceptance.Journey("interruption"), guest)


def test_a_leaf_with_no_updater_process_fails_before_any_fault(runner):
    guest = SimpleNamespace(name="leaf", sh=lambda command, **kwargs: "", stop=lambda: None, start=lambda: None)
    with pytest.raises(runner.AcceptanceFailure, match="no updater process"):
        runner.ensure_true_updater(acceptance.Journey("interruption"), guest)


def test_the_reboot_leg_reboots_a_stale_updater_on_the_new_build_and_records_it(runner, monkeypatch, tmp_path):
    fleet, guest, offered, queued, power = _reboot_fleet(
        runner, monkeypatch, tmp_path, settled_detail=SETTLED_DETAIL)
    bundles = iter([STALE_UPDATER, TRUE_UPDATER])
    guest.sh = lambda command, **kwargs: next(bundles) if command == runner.UPDATER_BUNDLE else ""
    journey = acceptance.Journey("interruption")
    result = runner.reboot_mid_apply(journey, fleet, guest, "leaf-id",
                                     SimpleNamespace(sha=HEAD_SHA, ref="dev", slug="dev@" + HEAD_SHA[:8]))
    assert power == ["stop", "start", "stop", "start"], "one reboot to shed the stale updater, one mid-copy"
    assert any("#119" in line and ".failed-job-x" in line for line in journey.lines)
    assert result["state"] == "failed" and result["retry_sha"] == PRIOR_SHA


def test_the_reboot_leg_fails_when_the_stale_updater_survives_its_reboot(runner, monkeypatch, tmp_path):
    fleet, guest, offered, queued, power = _reboot_fleet(
        runner, monkeypatch, tmp_path, settled_detail=SETTLED_DETAIL)
    guest.sh = lambda command, **kwargs: STALE_UPDATER if command == runner.UPDATER_BUNDLE else ""
    with pytest.raises(runner.AcceptanceFailure, match="after a reboot the updater still runs from"):
        runner.reboot_mid_apply(acceptance.Journey("interruption"), fleet, guest, "leaf-id",
                                SimpleNamespace(sha=HEAD_SHA, ref="dev", slug="dev@" + HEAD_SHA[:8]))
    assert offered == [], "nothing is offered until this commit's own updater is the live one"


def _fault_fleet(runner, monkeypatch, *, fault_result, prior_release=PRIOR):
    queued = []
    hub = SimpleNamespace(queue=lambda machine, request: queued.append(request) or
                          {"jobs": [{"id": "job-" + request.split("-")[1]}]},
                          row=lambda machine: {"machine": machine, "state": "current"},
                          wait_for=lambda state, machine: {"state": state, "job": {}})
    guest = SimpleNamespace(name="leaf", installed=lambda: {"build": prior_release, "sha": PRIOR_SHA,
                                                             "client": "91", "menubar": "20260922"},
                            sh=lambda command, **kwargs: (
                                TRUE_UPDATER if command == runner.UPDATER_BUNDLE else ""),
                            stop=lambda: None, start=lambda: None)
    fleet = SimpleNamespace(leaves=[guest], hub=hub, machine=lambda _: "leaf-id", plan={})
    monkeypatch.setattr(runner, "stage_prior", lambda fleet, guest, **kwargs: guest.installed())
    monkeypatch.setattr(runner, "arm_fault", lambda guest, fault: fault)
    monkeypatch.setattr(runner, "read_fault", lambda process: fault_result)
    return fleet, guest, queued


def test_a_kill_that_fails_for_a_leftover_copy_is_not_an_interrupted_apply(runner, monkeypatch):
    fleet, guest, queued = _fault_fleet(runner, monkeypatch, fault_result={
        "state": "failed", "job": "j1", "detail": "unfinished incoming app requires recovery"})
    journey = acceptance.Journey("interruption")
    with pytest.raises(runner.AcceptanceFailure, match="not from an interrupted apply"):
        runner.interruption(journey, fleet, SimpleNamespace(sha=HEAD_SHA, ref="dev", slug="dev@" + HEAD_SHA[:8]))
    assert journey.observed == {}


def test_the_prior_s_leftover_copy_is_recorded_and_removed_before_the_retry(runner, monkeypatch):
    fleet, guest, queued = _fault_fleet(runner, monkeypatch, fault_result={
        "state": "failed", "job": "j1",
        "detail": "updated components failed verification; this machine is running " + CANDIDATE})
    commands = []
    listing = {"paths": "/Applications/jRemote.app.incoming-" + CANDIDATE + "\n"}
    def shell(command, **kwargs):
        commands.append(command)
        if command == runner.RESIDUE_LISTING:
            found, listing["paths"] = listing["paths"], ""
            return found
        if command == runner.UPDATER_BUNDLE:
            return TRUE_UPDATER
        return ""
    guest.sh = shell
    moves = iter([(PRIOR, PRIOR_SHA), (CANDIDATE, HEAD_SHA)])
    guest.installed = lambda: dict(zip(("build", "sha"), next(moves)))
    monkeypatch.setattr(runner, "reboot_mid_apply",
                        lambda journey, fleet, guest, machine, build: {"job": "j3", "state": "failed"})
    journey = acceptance.Journey("interruption")

    runner.interruption(journey, fleet, SimpleNamespace(sha=HEAD_SHA, ref="dev", slug="dev@" + HEAD_SHA[:8]))

    import shlex
    assert "/bin/rm -rf -- " + shlex.quote("/Applications/jRemote.app.incoming-" + CANDIDATE) in commands
    assert not any("/Applications/*" in command for command in commands), "no shell globs reach the guest's zsh"
    assert any("jRemote.app.incoming-" in line and "#117" in line for line in journey.lines)
    assert commands.index(next(c for c in commands if c.startswith("/bin/rm"))) > 0
    assert journey.observed["retry_current"]["sha"] == HEAD_SHA
    assert journey.missing == ()


def _reboot_fleet(runner, monkeypatch, tmp_path, *, settled_detail, leftovers=""):
    prior = SimpleNamespace(sha=PRIOR_SHA, ref="main", slug="main@" + PRIOR_SHA[:8])
    offered, queued, power = [], [], []
    monkeypatch.setattr(runner, "arm_fault", lambda guest, fault: fault)
    monkeypatch.setattr(runner, "read_freeze", lambda process: {
        "injected": "freeze", "job": "job-reboot", "pid": 41, "copies": ["/Applications/x.incoming-j"]})
    hub = SimpleNamespace(
        sh=lambda command, **kwargs: "",  # the key it signs with: none, like the leaf's
        row=lambda machine: {"machine": machine, "state": "current"},  # before lines: no line
        queue=lambda machine, request: queued.append(request) or {"jobs": [{"id": "job-" + request.split("-")[1]}]},
        wait_for=lambda state, machine: {"state": state, "job": {"id": "job-reboot", "detail": settled_detail}})
    installed = iter([{"build": CANDIDATE, "sha": HEAD_SHA},
                      {"build": CANDIDATE, "sha": HEAD_SHA},
                      {"build": PRIOR, "sha": PRIOR_SHA}])
    guest = SimpleNamespace(name="leaf", installed=lambda: next(installed),
                            stop=lambda: power.append("stop"), start=lambda: power.append("start"),
                            sh=lambda command, **kwargs: (
                                leftovers if command == runner.RESIDUE_LISTING
                                else TRUE_UPDATER if command == runner.UPDATER_BUNDLE else ""))
    fleet = SimpleNamespace(prior=prior, hub=hub, offer=lambda b: offered.append(b.sha))
    return fleet, guest, offered, queued, power


def test_a_reboot_mid_copy_fails_keeps_the_build_and_the_next_request_lands(
        runner, monkeypatch, tmp_path):
    fleet, guest, offered, queued, power = _reboot_fleet(
        runner, monkeypatch, tmp_path, settled_detail=SETTLED_DETAIL)
    candidate = SimpleNamespace(sha=HEAD_SHA, ref="dev", slug="dev@" + HEAD_SHA[:8])

    result = runner.reboot_mid_apply(acceptance.Journey("interruption"), fleet, guest, "leaf-id", candidate)

    assert power == ["stop", "start"]
    assert result["state"] == "failed" and result["build_kept"] == CANDIDATE
    assert result["retry_sha"] == PRIOR_SHA and result["frozen_copies"]
    assert offered == [PRIOR_SHA, HEAD_SHA], "the ref under test is offered again whatever happened"
    assert [request.split("-")[1] for request in queued] == ["reboot", "reboot"]


def test_a_reboot_that_settles_any_other_way_fails_by_its_detail(runner, monkeypatch, tmp_path):
    fleet, guest, offered, queued, power = _reboot_fleet(
        runner, monkeypatch, tmp_path, settled_detail="unfinished incoming app requires recovery")
    with pytest.raises(runner.AcceptanceFailure, match="settled the job as 'unfinished incoming"):
        runner.reboot_mid_apply(acceptance.Journey("interruption"), fleet, guest, "leaf-id", SimpleNamespace(sha=HEAD_SHA, ref="dev", slug="dev@" + HEAD_SHA[:8]))
    assert offered == [PRIOR_SHA, HEAD_SHA]


def test_a_recovered_updater_that_leaves_a_copy_behind_fails(runner, monkeypatch, tmp_path):
    fleet, guest, offered, queued, power = _reboot_fleet(
        runner, monkeypatch, tmp_path, settled_detail=SETTLED_DETAIL,
        leftovers="/Applications/jStack Hub.app.incoming-job-reboot\n")
    with pytest.raises(runner.AcceptanceFailure, match="left \\['/Applications/jStack Hub.app.incoming"):
        runner.reboot_mid_apply(acceptance.Journey("interruption"), fleet, guest, "leaf-id", SimpleNamespace(sha=HEAD_SHA, ref="dev", slug="dev@" + HEAD_SHA[:8]))


def test_the_reboot_leg_refuses_a_guest_not_on_the_commit_under_test(runner, monkeypatch, tmp_path):
    fleet, guest, offered, queued, power = _reboot_fleet(
        runner, monkeypatch, tmp_path, settled_detail=SETTLED_DETAIL)
    guest.installed = lambda: {"build": PRIOR, "sha": PRIOR_SHA}
    with pytest.raises(runner.AcceptanceFailure, match="needs this commit's updater"):
        runner.reboot_mid_apply(acceptance.Journey("interruption"), fleet, guest, "leaf-id", SimpleNamespace(sha=HEAD_SHA, ref="dev", slug="dev@" + HEAD_SHA[:8]))
    assert offered == [] and power == []


@pytest.mark.parametrize("output", [
    "",
    '{"armed": "freeze"}\n',
    '{"injected": "freeze", "job": "j", "pid": 3, "copies": []}\n',
])
def test_read_freeze_wants_a_stopped_pid_and_a_copy_in_flight(runner, output):
    process = SimpleNamespace(communicate=lambda timeout=None: (output, ""), returncode=0)
    with pytest.raises(runner.AcceptanceFailure, match="did not prove a frozen"):
        runner.read_freeze(process)
    good = '{"injected": "freeze", "job": "j", "pid": 3, "copies": ["/Applications/a.incoming-j"]}\n'
    assert runner.read_freeze(SimpleNamespace(communicate=lambda timeout=None: (good, ""),
                                              returncode=0))["job"] == "j"


class ResetGuestFleet(ScriptedFleet):
    """A fleet whose guests hold only what was put there since their reset."""

    def __init__(self, *, provisions=True, **kwargs):
        super().__init__(**kwargs)
        self.present: dict[str, set[str]] = {}
        self.provisions = provisions

    def __call__(self, argv, **kwargs):
        if argv[0] == "provision":
            self.calls.append(list(argv))
            if self.provisions:
                self.present.setdefault(argv[1], set()).add("/Users/admin/adopt-to-hub.sh")
            return subprocess.CompletedProcess(argv, 0, "", "")
        _, action, name, *rest = argv
        if action == "cp":
            self.present.setdefault(name, set()).add(rest[1])
        if action == "ssh" and ("ifconfig" in rest[0] or "candidate_test" in rest[0]
                                or "launchctl kickstart" in rest[0]):
            return ScriptedFleet.__call__(self, argv, **kwargs)
        if action == "ssh" and rest[0].startswith("for p in"):
            self.calls.append(list(argv))
            wanted = [part.strip("'") for part in rest[0].split(";")[0].split()[3:]]
            absent = [path for path in wanted if path not in self.present.get(name, set())]
            return subprocess.CompletedProcess(argv, 0, "".join(p + "\n" for p in absent), "")
        return super().__call__(argv, **kwargs)


def test_a_reset_guest_gets_the_runner_s_own_tools_back_before_its_journey(runner):
    scripted = ResetGuestFleet()
    fleet = build(runner, scripted, fresh="fresh")
    fleet.cast(fleet.hub, fleet.fresh)
    for name in ("hub", "fresh"):
        assert {runner.GUEST_TOOL, runner.GUEST_FAULT} <= scripted.present[name]


def test_a_missing_plan_fixture_is_provisioned_before_the_journey(runner):
    scripted = ResetGuestFleet()
    fleet = build(runner, scripted, fresh="fresh", fixtures=["/Users/admin/adopt-to-hub.sh"],
                  provision="provision {guest} --tools-only")
    fleet.cast(fleet.hub, fleet.fresh)
    assert ["provision", "fresh", "--tools-only"] in scripted.calls
    assert "/Users/admin/adopt-to-hub.sh" in scripted.present["fresh"]


def test_a_fixture_still_missing_is_a_harness_receipt_not_a_failed_journey(
        runner, subject, tmp_path):
    scripted = ResetGuestFleet(provisions=False)
    fleet = build(runner, scripted, fresh="fresh", fixtures=["/Users/admin/adopt-to-hub.sh"],
                  provision="provision {guest}")
    run = acceptance.Run(tmp_path / "receipts", IDENTITY)
    with run.journey("fresh_install") as journey:
        fleet.cast(*runner.CAST["fresh_install"](fleet))
        runner.fresh_install(journey, fleet, subject)
    receipt = json.loads((tmp_path / "receipts" / "fresh_install.json").read_text())
    assert receipt["result"] == "harness"
    assert receipt["detail"] == "fixture missing: hub:/Users/admin/adopt-to-hub.sh"
    state = acceptance.inspect(tmp_path / "receipts", IDENTITY)["fresh_install"]
    assert state["state"] == "harness"
    with pytest.raises(releases.ReleaseError, match="fresh_install: harness"):
        acceptance.gate(tmp_path / "receipts", IDENTITY)


def test_a_guest_off_the_lab_network_is_a_harness_fault_not_a_hung_journey(runner):
    """A guest booted outside the lab keeps the default NAT and no other guest
    can reach it. The runner says so instead of waiting on an ssh that will
    never connect."""
    scripted = ResetGuestFleet(address="192.168.64.120")
    fleet = build(runner, scripted, fresh="fresh")
    with pytest.raises(runner.HarnessFault) as caught:
        fleet.cast(fleet.hub, fleet.fresh)
    assert "not on the lab network" in str(caught.value)
    assert "192.168.64.120" in str(caught.value)


def test_a_plan_may_name_the_lab_network_it_expects(runner):
    scripted = ResetGuestFleet(address="10.9.9.4")
    fleet = build(runner, scripted, fresh="fresh")
    fleet.network = "10.9.9."
    fleet.cast(fleet.hub, fleet.fresh)
    assert scripted.present["fresh"]


def test_the_fresh_guest_is_reset_before_it_is_booted(runner):
    """A run that died after installing on the fresh guest must not hand the
    next run a Mac that already carries a Hub."""
    scripted = ResetGuestFleet()
    fleet = build(runner, scripted, fresh="fresh")
    fleet.cast(fleet.hub, fleet.fresh)
    actions = [(argv[1], argv[2]) for argv in scripted.calls if argv[1] in {"reset", "gui"}]
    assert actions.index(("reset", "fresh")) < actions.index(("gui", "fresh"))
    assert ("reset", "hub") not in actions


def test_the_fresh_guest_installed_on_this_run_is_kept_across_casts(runner):
    """After fresh_install the fresh Mac is a fleet member the hub adopted;
    parking and re-booting it must hand back that Mac, not a clone."""
    scripted = ResetGuestFleet()
    fleet = build(runner, scripted, fresh="fresh")
    fleet.installed.add("fresh")
    fleet.cast(fleet.hub, fleet.fresh)
    assert not [argv for argv in scripted.calls if argv[1] == "reset"]


def test_only_the_fresh_guest_is_reset(runner):
    scripted = ResetGuestFleet()
    fleet = build(runner, scripted, fresh="fresh")
    fleet.cast(fleet.hub, fleet.leaves[0])
    assert not [argv for argv in scripted.calls if argv[1] == "reset"]


def test_every_cast_guest_with_a_host_gets_its_lab_flag_back(runner):
    """The hub's own install of the build it made rewrites the flag off, and
    the guest tool then refuses every call. The cast puts it back."""
    scripted = ResetGuestFleet()
    fleet = build(runner, scripted, fresh="fresh")
    fleet.cast(fleet.hub, fleet.leaves[0])
    assert scripted.flagged == ["hub", "leaf-a"]
    assert scripted.kicked == ["hub", "leaf-a"]


def test_a_flag_already_on_restarts_nothing(runner):
    scripted = ResetGuestFleet()
    scripted.lab_state = "0"
    fleet = build(runner, scripted, fresh="fresh")
    fleet.cast(fleet.hub, fleet.leaves[0])
    assert scripted.flagged == ["hub", "leaf-a"]
    assert scripted.kicked == []


def test_a_guest_without_a_host_is_left_alone(runner):
    scripted = ResetGuestFleet()
    scripted.lab_state = "3"
    fleet = build(runner, scripted, fresh="fresh")
    assert runner.lab_guest(fleet.fresh) is False
    assert scripted.kicked == []


def test_a_guest_that_stops_answering_names_itself_instead_of_a_traceback(runner):
    """`subprocess.TimeoutExpired` out of `vm.sh` ended a whole acceptance in a
    stack trace with no guest in it — before the first receipt was written."""
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))

    hub = runner.Guest("acc-hub", Path("/bin/vm.sh"), run=run)
    with pytest.raises(runner.AcceptanceFailure, match="acc-hub: vm.sh ssh did not return"):
        hub.sh("true", timeout=7)


def test_the_api_wait_gives_the_guest_the_seconds_it_claims_to(runner):
    """The wait loop spends a curl timeout per turn as well as its sleep, so an
    iteration count is not a second count: counting to 180 ran the guest past
    the ssh budget, which killed a Mac that was still coming up."""
    seen = {}
    def run(argv, **kwargs):
        seen["script"], seen["timeout"] = argv[-1], kwargs["timeout"]
        return subprocess.CompletedProcess(argv, 0, "up 200 31s", "")

    guest = runner.Guest("acc-hub", Path("/bin/vm.sh"), run=run)
    assert guest.await_api(timeout=180) == "up 200 31s"
    assert "seq 1 180" not in seen["script"], "still counting turns instead of seconds"
    assert "-lt 180" in seen["script"], "the deadline is not the timeout it was given"
    assert "$(date +%s) - start" in seen["script"], "the loop does not read the clock"
    # Room for the in-flight curl and the ssh handshake on either side of it.
    assert seen["timeout"] >= 180 + 60


# ── A local hub: this Mac in the hub role, both VM slots for leaves

LOCAL_PLAN = {"disposable": True, "vm_tool": "/bin/vm.sh", "vm_slots": 2,
              "hub": "localhost", "leaves": ["leaf-a", "leaf-b"]}


def _local_main(runner, tmp_path, monkeypatch, *only):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(LOCAL_PLAN))
    argv = ["accept", "--ref", "dev", "--receipts", str(tmp_path / "r"), "--plan", str(plan)]
    monkeypatch.setattr("sys.argv", argv + (["--only", *only] if only else []))


@pytest.mark.parametrize("only,named", [
    (("shell_flip", "off_network"), "off_network"),
    ((), "fleet"),
])
def test_a_local_hub_refuses_a_destructive_journey_before_anything_boots(
        runner, tmp_path, monkeypatch, capsys, only, named):
    """The default run and an explicit one alike: a journey outside
    HOST_HUB_SAFE is a refusal to start, not a skip written after the start."""
    scripted = SlotCountingFleet()
    monkeypatch.setattr(runner.subprocess, "run", scripted)
    monkeypatch.setattr(runner, "Build", lambda *a, **k: pytest.fail("resolved a build first"))
    monkeypatch.setattr(runner.LocalHub, "start", lambda self: pytest.fail("touched the hub"))
    _local_main(runner, tmp_path, monkeypatch, *only)
    with pytest.raises(SystemExit) as exit_code:
        runner.main()
    assert exit_code.value.code == 2
    said = capsys.readouterr().err
    assert named in said and "real machine" in said
    assert "shell_flip would" not in said, "a safe journey was named as the offender"
    assert not scripted.calls, "a guest was touched before the refusal"
    assert not (tmp_path / "r").exists(), "a refusal wrote receipts"


def test_localhost_is_never_a_leaf_or_the_pristine_guest(runner):
    for plan in ({**LOCAL_PLAN, "hub": "acc-hub", "leaves": ["localhost"]},
                 {**LOCAL_PLAN, "hub": "acc-hub", "fresh": "localhost"}):
        with pytest.raises(runner.AcceptanceFailure, match="only be the hub"):
            runner.Fleet(plan, run=SlotCountingFleet())


def test_a_local_hub_takes_no_slot(runner, monkeypatch):
    scripted = SlotCountingFleet()
    fleet = runner.Fleet(LOCAL_PLAN, run=scripted)
    assert isinstance(fleet.hub, runner.LocalHub)
    monkeypatch.setattr(fleet.hub, "start", lambda: "localhost: answering")
    monkeypatch.setattr(fleet, "prepare", lambda *guests: None)
    monkeypatch.setattr(runner, "reach", lambda fleet, guest: None)
    fleet.cast(*runner.CAST["shell_flip"](fleet))
    assert scripted.booted == {"leaf-a", "leaf-b"} and scripted.peak == 2
    assert not any("localhost" in call for call in scripted.calls), \
        "vm.sh was asked to act on this Mac"
    # A third VM still does not fit: the exemption is the local hub's alone.
    with pytest.raises(runner.AcceptanceFailure, match="2 slots"):
        fleet.cast(fleet.hub, *fleet.leaves, runner.Guest("leaf-c", Path("/bin/vm.sh"),
                                                          run=scripted))


def test_resetting_the_local_hub_raises_past_any_journey(runner):
    touched = []
    hub = runner.LocalHub(run=lambda *a, **k: touched.append(a))
    with pytest.raises(runner.RealMachineRefusal, match="refusing to reset localhost"):
        hub.reset()
    # `Run.journey` turns an Exception into a failed receipt and carries on;
    # this must end the run instead.
    assert not issubclass(runner.RealMachineRefusal, Exception)
    assert not touched
    with pytest.raises(runner.AcceptanceFailure, match="not a VM"):
        hub.vm("reset", "localhost")
    with pytest.raises(runner.AcceptanceFailure, match="disposable fixtures only"):
        hub.tool_call("revoke", "--machine", "m")


def test_a_local_hub_off_the_commit_is_refused_and_never_updated(
        runner, subject, tmp_path, monkeypatch, capsys):
    scripted = SlotCountingFleet()
    fleet = runner.Fleet(LOCAL_PLAN, run=scripted)
    touched = []
    hub = fleet.hub
    monkeypatch.setattr(hub, "start", lambda: "localhost: answering")
    monkeypatch.setattr(hub, "installed", lambda: {"sha": PRIOR_SHA, "dirty": False,
                                                   "build": PRIOR})
    monkeypatch.setattr(hub, "served", lambda: {"build": PRIOR, "client": "70",
                                                "sha": PRIOR_SHA})
    for name in ("sh", "queue", "call", "request", "copy"):
        monkeypatch.setattr(hub, name, lambda *a, _name=name, **k: touched.append(_name))
    monkeypatch.setattr(runner, "Fleet", lambda *a, **k: fleet)
    monkeypatch.setattr(runner, "Build", lambda *a, **k: subject)
    _local_main(runner, tmp_path, monkeypatch, "shell_flip")
    with pytest.raises(SystemExit) as exit_code:
        runner.main()
    assert exit_code.value.code == 2
    said = capsys.readouterr().err
    assert PRIOR_SHA in said and "never updated" in said
    assert not touched, f"the runner acted on this Mac: {touched}"
    assert not scripted.calls, "a leaf was booted for a run that cannot start"
    # And the door that would have moved it is shut on its own account.
    with pytest.raises(runner.AcceptanceFailure, match="refusing to build"):
        fleet.offer(subject)
    assert not touched


def test_a_local_hub_on_the_commit_is_held_to_it_as_it_stands(runner, subject, monkeypatch):
    fleet = runner.Fleet(LOCAL_PLAN, run=SlotCountingFleet())
    monkeypatch.setattr(fleet.hub, "installed", lambda: {"sha": HEAD_SHA, "dirty": False,
                                                         "build": CANDIDATE})
    monkeypatch.setattr(fleet.hub, "served", lambda: {"build": CANDIDATE, "client": "71",
                                                      "sha": HEAD_SHA})
    assert runner.local_identity(fleet, subject) == {"build": CANDIDATE, "sha": HEAD_SHA}
    assert fleet.build is subject and fleet.served["dev"] == "71"
    monkeypatch.setattr(fleet.hub, "installed", lambda: {"sha": HEAD_SHA, "dirty": True,
                                                         "build": CANDIDATE})
    with pytest.raises(runner.AcceptanceFailure, match="modified"):
        runner.local_identity(fleet, subject)


def test_the_local_hub_finds_the_state_dir_its_host_declares(runner, tmp_path):
    declared = tmp_path / "dashboard-state"
    (declared / "updates").mkdir(parents=True)
    (declared / "updates/config.json").write_text(json.dumps({"local_url": "http://127.0.0.1:9090"}))
    marker = tmp_path / ".local/state/jremote"
    marker.mkdir(parents=True)
    (marker / "embedded.json").write_text(json.dumps({"state_dir": str(declared)}))
    assert runner.LocalHub(home=tmp_path).config()["local_url"] == "http://127.0.0.1:9090"
    (marker / "embedded.json").unlink()
    with pytest.raises(runner.AcceptanceFailure, match="no readable updater config"):
        runner.LocalHub(home=tmp_path).config()


def test_shell_flip_on_a_local_hub_drives_the_hub_s_own_route(runner, monkeypatch):
    flips = []
    hub = runner.LocalHub(run=lambda *a, **k: None)

    def call(path, body=None, **kwargs):
        flips.append((path, body))
        return {"status": 200, "body": json.dumps(
            {"allowed": body["allowed"], "steps": [{"step": "refresh", "ok": True}]})}

    monkeypatch.setattr(hub, "call", call)
    leaf_a = SimpleNamespace(name="leaf-a")
    leaf_b = SimpleNamespace(name="leaf-b")
    fleet = SimpleNamespace(hub=hub, leaves=[leaf_a, leaf_b],
                            machine=lambda g: "machine-" + g.name)
    driven = {}
    monkeypatch.setattr(runner, "shell_alias", lambda f, m: ({}, "leaf-b-alias"))
    monkeypatch.setattr(runner, "flip_through_lab", lambda *a: pytest.fail("used the lab"))

    def pair(journey, a, b, machine_a, machine_b, alias, flip):
        driven.update(alias=alias, on=flip(True), off=flip(False))

    monkeypatch.setattr(runner, "flip_pair", pair)
    notes = []
    runner.shell_flip(SimpleNamespace(note=notes.append), fleet, None)
    assert flips == [("/hosts/machine-leaf-b/shell", {"src": "machine-leaf-a", "allowed": True}),
                     ("/hosts/machine-leaf-b/shell", {"src": "machine-leaf-a", "allowed": False})]
    assert driven["alias"] == "leaf-b-alias"
    assert any("real Hub on localhost" in note for note in notes), "the receipt names no hub"


class ParkedMacsAnswerNoSsh(SlotCountingFleet):
    """A stopped guest is a stopped Mac: ssh into it times out, as it did on
    the lab host. The shell routes answer what the teardown reads."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.timed_out: list[str] = []

    def __call__(self, argv, **kwargs):
        _, action, name, *rest = argv
        command = rest[0] if rest else ""
        if action == "ssh" and name not in self.booted:
            self.calls.append(list(argv))
            self.timed_out.append(name)
            return subprocess.CompletedProcess(
                argv, 255, "", f"ssh: connect to host {name} port 22: Operation timed out")
        if action == "ssh" and "--path /shell/refresh" in command:
            self.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, json.dumps(
                {"status": 200, "body": json.dumps({"steps": [{"step": "refresh", "ok": True}]})}), "")
        if action == "ssh" and "authorized_keys" in command:
            self.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "ssh-ed25519 AAAA hub\n", "")
        return super().__call__(argv, **kwargs)


def test_the_lab_restore_puts_files_back_on_both_leaves_before_a_cast_parks_one(runner):
    scripted = ParkedMacsAnswerNoSsh()
    fleet = build(runner, scripted, vm_slots=2)
    # The flip's own cast on a two-slot host: both leaves up, the hub parked.
    fleet.cast(*runner.CAST["shell_flip"](fleet))
    assert fleet.hub.name in fleet.parked
    failures = runner.lab_teardown(fleet, None, list(fleet.leaves))
    assert failures == [], failures
    assert scripted.timed_out == [], "the restore shelled into a parked Mac"
    assert scripted.peak <= 2
    refreshed = [call[2] for call in scripted.calls
                 if call[1] == "ssh" and "--path /shell/refresh" in call[3]]
    assert refreshed == ["leaf-a", "leaf-b"], "every leaf re-pulls its hub set beside the hub"


class DelegationFleet:
    """A hub and a leaf answering the delegation journey, each defect switchable."""

    def __init__(self, *, remint="proj-1", admin_status=403, after_withdrawal=403,
                 rows=None, non_leaf_status=403, roster=()):
        self.remint, self.admin_status = remint, admin_status
        self.after_withdrawal, self.non_leaf_status = after_withdrawal, non_leaf_status
        self.rows = rows if rows is not None else ["proj-1"]
        self.roster = list(roster)
        self.minted = 0
        self.revoked: list[str] = []
        self.hub = SimpleNamespace(name="localhost", call=self.hub_call, request=self.hub_request)
        self.leaf = SimpleNamespace(name="leaf-a", request=self.leaf_request, sh=self.leaf_sh)
        self.leaves = [self.leaf]

    def machine(self, guest):
        return "machine-leaf-a"

    @staticmethod
    def answer(status, body):
        return {"status": status, "body": json.dumps(body)}

    def hub_call(self, path, body=None, **kwargs):
        if path == "/devices":
            return self.answer(200, {"device": {"id": "dev-1"}, "token": "jr1.dev-1.secret"})
        if path.endswith("/revoke"):
            self.revoked.append(path.split("/")[2])
            return self.answer(200, {"revoked": path.split("/")[2]})
        raise AssertionError(path)

    def hub_request(self, url, token, body=None, **kwargs):
        if url == "/hosts/machine-leaf-a/grant":
            self.minted += 1
            projection = "proj-1" if self.minted == 1 else self.remint
            return self.answer(200, {"device": {"id": projection}, "token": "jr1." + projection,
                                     "address": "10.66.0.7", "port": 9090})
        if url == "http://10.66.0.7:9090/api/jremote/v1/host":
            if self.revoked:
                return self.answer(self.after_withdrawal, {"detail": "no longer authorized"})
            return self.answer(200, {"host_id": "machine-leaf-a"})
        raise AssertionError(url)

    def leaf_request(self, path, token, body=None, **kwargs):
        if path == "/devices" and body is None:
            return self.answer(200, {"devices": self.roster})
        return self.answer(self.admin_status, {"detail": "hub menu bar"})

    def leaf_sh(self, command, **kwargs):
        if "authority_device" in command:
            return json.dumps(self.rows)
        if "MINT_PATH" in command:
            return json.dumps({"held": True, "parent": "machine-hub", "status": self.non_leaf_status,
                               "url": "http://10.66.0.1:9090/api/jremote/v1/delegate/access",
                               "body": json.dumps({"detail": "only a managed machine"})})
        raise AssertionError(command)


def _delegate(runner, tmp_path, scripted):
    run = acceptance.Run(tmp_path / "receipts", IDENTITY)
    with run.journey("delegate_leaf") as journey:
        runner.delegate_leaf(journey, scripted, None)
    receipt = json.loads((tmp_path / "receipts/delegate_leaf.json").read_text())
    return run.results["delegate_leaf"], receipt


def test_a_projection_that_holds_every_line_passes(runner, tmp_path):
    scripted = DelegationFleet()
    result, receipt = _delegate(runner, tmp_path, scripted)
    assert result == "passed", receipt["detail"]
    assert receipt["observed"] == sorted(acceptance.REQUIRED["delegate_leaf"])
    assert scripted.revoked == ["dev-1"], "the hub device outlived its journey"


@pytest.mark.parametrize("defect,detail", [
    ({"remint": "proj-2"}, "a second device"),
    ({"rows": ["proj-1", "proj-9"]}, "not the one projection"),
    ({"admin_status": 404}, "let mint a device"),
    ({"admin_status": 200}, "let mint a device"),
    ({"roster": [{"id": "legacy"}]}, "read leaf-a's device roster"),
    ({"after_withdrawal": 200}, "still honours the projection"),
    ({"non_leaf_status": 401}, "answered a live grant with 401"),
])
def test_a_projection_that_breaks_a_line_fails_by_name(runner, tmp_path, defect, detail):
    scripted = DelegationFleet(**defect)
    result, receipt = _delegate(runner, tmp_path, scripted)
    assert result == "failed"
    assert detail in receipt["detail"]
    # Withdrawn exactly once however the journey ended: a live test device
    # left on a hub that outlives the run is the thing the finally exists for.
    assert scripted.revoked == ["dev-1"]


def test_delegate_leaf_is_ordered_on_the_provisioned_adoption(runner):
    order = list(runner.JOURNEYS)
    assert order.index("shell_adopt") < order.index("delegate_leaf") < order.index("upgrade_shell")
    assert "delegate_leaf" in runner.HOST_HUB_SAFE
    fleet = SimpleNamespace(hub="hub", leaves=["a", "b"], fresh=None)
    assert runner.CAST["delegate_leaf"](fleet) == ("hub", "a")


@pytest.mark.parametrize("name,stop_at", [("shell_adopt", "Guest.call"),
                                          ("shell_detach", "shell_alias")])
def test_an_ssh_journey_moves_its_leaf_onto_the_candidate_before_reading_it(
        runner, subject, tmp_path, monkeypatch, name, stop_at):
    """Run 20260925-061935: interruption had left acc-leaf1 on the earlier
    ref, as its reboot leg does on purpose, and no journey moved it back once
    upgrade_shell was left out. shell_detach then took apart a leaf on
    release/one-hub, whose detach posts a revoke the hub had already done
    with the forget, and read `parent-revoke 401` off code the run was not
    testing. The ssh journeys that read leaves[0] now move it onto the
    candidate first, by the hub's queue, and record where it stands."""
    def stop(*args, **kwargs):
        raise runner.AcceptanceFailure("stopped after the move")
    monkeypatch.setattr(runner.Guest if stop_at == "Guest.call" else runner,
                        stop_at.split(".")[-1], stop)
    scripted = ScriptedFleet()
    result, receipt = journey_result(runner, subject, name, scripted, tmp_path)
    assert result == "failed" and "stopped after the move" in receipt["detail"]
    assert scripted.queued == ["machine-leaf-a"], "the leaf on the earlier ref was not moved"
    assert receipt["observed"] == ["leaf_on_candidate"]

    already = ScriptedFleet(release=CANDIDATE, sha=HEAD_SHA)
    result, receipt = journey_result(runner, subject, name, already, tmp_path)
    assert result == "failed" and "stopped after the move" in receipt["detail"]
    assert already.queued == [], "a leaf already on the candidate was queued a job"
    assert receipt["observed"] == ["leaf_on_candidate"]


def test_a_local_hub_mints_the_adoption_code_at_its_own_console(runner, monkeypatch):
    """The lab's joiner script ssh's into the hub as admin and runs the CLI
    there. This Mac has no jstack-host launcher and grows no authorized key for
    a disposable VM, so joining it has to go through the console route the
    hub's own menu bar spends — and the guest spends the code it hands back."""
    fleet = runner.Fleet({**LOCAL_PLAN, "hub_address": "192.168.2.1",
                          "adopt_command": "/bin/bash ~/adopt-to-hub.sh acc-hub.local"},
                         run=SlotCountingFleet())
    minted, ran = [], []
    monkeypatch.setattr(fleet.hub, "config", lambda: {"local_url": "http://127.0.0.1:9090"})
    monkeypatch.setattr(fleet.hub, "call", lambda path, body=None, **kw: minted.append((path, body))
                        or {"status": 200, "body": json.dumps({"code": "LAB-4242"})})
    guest = fleet.leaves[0]
    monkeypatch.setattr(guest, "sh", _leaf_shell(ran, attach=ATTACH_NO_APP,
                                                 parent="http://192.168.2.1:9090"))
    runner.join(fleet, guest)
    assert minted == [("/enrolment/codes",
                       {"name": "Update lab leaf-a", "kind": "host"})], \
        "the hub did not mint a host code at its console"
    attach = [c for c in ran if "attach" in c]
    assert len(attach) == 1 and "LAB-4242" in attach[0], f"the guest spent no code: {ran}"
    assert "--parent http://192.168.2.1:9090" in attach[0], \
        f"the guest was not pointed at this Mac's Hub: {attach[0]}"
    assert not any("adopt-to-hub" in c for c in ran), \
        "the lab's ssh-into-the-hub joiner ran against this Mac"


#: What a lab guest's `attach` prints: the redeem lands, the mode reads
#: managed, then the local pair link finds no app and the CLI exits 1.
ATTACH_NO_APP = ("attached acc-leaf1 to http://192.168.2.1:9090 — this Mac is a managed hub now.\n"
                 "\nmode  managed\n      the parent hub drives this Mac\n"
                 "the app on this Mac did not take that link — open it, then type this code into it:\n"
                 "\n    YU5B-EHM8\n\ngood for 10 minutes.\n"
                 "attach-exit=1\n")


def _leaf_shell(ran, *, attach, parent):
    """A guest shell that answers the two things join() asks of it: the
    attach transcript, and what parent.json names afterwards."""
    def sh(command, **kw):
        ran.append(command)
        if " attach " in command:
            return attach
        if "parent.json" in command:
            return parent + "\n"
        return ""
    return sh


def _local_fleet(runner, monkeypatch):
    fleet = runner.Fleet({**LOCAL_PLAN, "hub_address": "192.168.2.1"}, run=SlotCountingFleet())
    monkeypatch.setattr(fleet.hub, "config", lambda: {"local_url": "http://127.0.0.1:9090"})
    monkeypatch.setattr(fleet.hub, "call", lambda path, body=None, **kw:
                        {"status": 200, "body": json.dumps({"code": "LAB-4242"})})
    return fleet


def test_a_leaf_with_no_app_still_counts_as_adopted_when_it_records_this_hub(runner, monkeypatch):
    """`attach` hands the leaf's own app a local pair link last and exits 1
    when nothing spends it — on a lab guest, every time. The redeem it did
    first is what adoption is, and the leaf's parent record is the proof."""
    fleet = _local_fleet(runner, monkeypatch)
    guest, ran = fleet.leaves[0], []
    monkeypatch.setattr(guest, "sh", _leaf_shell(ran, attach=ATTACH_NO_APP,
                                                 parent="http://192.168.2.1:9090"))
    runner.join(fleet, guest)
    attach = [c for c in ran if " attach " in c][0]
    assert "attach-exit=" in attach, \
        "join must capture attach's exit itself; vm.sh ssh raises on the app's refusal"
    assert ran.index(attach) < ran.index([c for c in ran if "parent.json" in c][0])


def test_a_failed_redeem_fails_the_join_by_name(runner, monkeypatch):
    fleet = _local_fleet(runner, monkeypatch)
    guest, ran = fleet.leaves[0], []
    monkeypatch.setattr(guest, "sh", _leaf_shell(
        ran, attach="that code is not one this hub minted\nattach-exit=1\n", parent=""))
    with pytest.raises(runner.AcceptanceFailure, match="leaf-a: attach to localhost exited 1"):
        runner.join(fleet, guest)


def test_an_attach_that_does_not_read_as_managed_fails_even_without_an_app(runner, monkeypatch):
    fleet = _local_fleet(runner, monkeypatch)
    guest, ran = fleet.leaves[0], []
    transcript = ATTACH_NO_APP.replace("attach-exit=1",
        "The leaf installed but this machine is not reading as a managed hub yet\nattach-exit=1")
    monkeypatch.setattr(guest, "sh", _leaf_shell(ran, attach=transcript,
                                                 parent="http://192.168.2.1:9090"))
    with pytest.raises(runner.AcceptanceFailure, match="exited 1"):
        runner.join(fleet, guest)


def test_a_leaf_recording_another_parent_is_not_adopted(runner, monkeypatch):
    """A clean exit is not the verdict either: the leaf has to name this hub."""
    fleet = _local_fleet(runner, monkeypatch)
    guest, ran = fleet.leaves[0], []
    monkeypatch.setattr(guest, "sh", _leaf_shell(
        ran, attach="attached leaf-a to http://10.0.0.5:9090 — this Mac is a managed hub now.\n"
                    "attach-exit=0\n", parent="http://10.0.0.5:9090"))
    with pytest.raises(runner.AcceptanceFailure,
                       match="does not record localhost as its parent.*10.0.0.5"):
        runner.join(fleet, guest)


def test_joining_a_vm_hub_still_runs_the_plan_s_joiner(runner, monkeypatch):
    fleet = runner.Fleet({**LOCAL_PLAN, "hub": "acc-hub",
                          "adopt_command": "/bin/bash ~/adopt-to-hub.sh acc-hub.local"},
                         run=SlotCountingFleet())
    ran = []
    monkeypatch.setattr(fleet.leaves[0], "sh", lambda command, **kw: ran.append(command) or "")
    runner.join(fleet, fleet.leaves[0])
    assert ran == ["/bin/bash ~/adopt-to-hub.sh acc-hub.local"]


def test_a_local_hub_without_an_address_asks_the_guest_for_its_gateway(runner, monkeypatch):
    fleet = runner.Fleet(LOCAL_PLAN, run=SlotCountingFleet())
    monkeypatch.setattr(fleet.hub, "config", lambda: {"local_url": "http://127.0.0.1:9091"})
    guest = fleet.leaves[0]
    monkeypatch.setattr(guest, "sh", lambda command, **kw: "192.168.2.1\n")
    assert runner.hub_parent_url(fleet, guest) == "http://192.168.2.1:9091"


class SourceBuiltFleet(KeyedFleet):
    """A leaf whose Hub it compiled itself: the one-file installer leaves that
    Hub alone, so an install over it moves nothing until the guest is reset."""

    def __init__(self, *, sticky=False, **kwargs):
        super().__init__(**kwargs)
        self.source_built = {"leaf-a"}
        self.sticky = sticky  # the install lands nothing, whatever was tried
        self.reset_names: list[str] = []

    def __call__(self, argv, **kwargs):
        _, action, name, *rest = argv
        command = rest[0] if rest else ""
        if action == "reset":
            self.reset_names.append(name)
            self.source_built.discard(name)
            self.keys[name] = ""
        if action == "ssh" and "release-identity.json" in command:
            return subprocess.CompletedProcess(
                argv, 0, "source-build\n" if name in self.source_built else "\n", "")
        if action == "ssh" and "jremote/host-id" in command:
            return subprocess.CompletedProcess(argv, 0, f"machine-{name}\n", "")
        if action == "ssh" and command.startswith("for p in"):
            return subprocess.CompletedProcess(argv, 0, "\n", "")  # pristine, or nothing listed
        if self.sticky and action == "ssh" and "install.sh --yes" in command:
            self.installed.append(name)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return super().__call__(argv, **kwargs)


def _source_built(runner, monkeypatch, tmp_path, earlier, subject, **kwargs):
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    monkeypatch.setattr(runner, "KEY_PATIENCE", 0)
    scripted = SourceBuiltFleet(release=CANDIDATE, sha="3" * 40, **kwargs)
    fleet = build(runner, scripted, prior=earlier,
                  adopt_command="/bin/bash ~/adopt-to-hub.sh hub.local")
    fleet.build = subject
    monkeypatch.setattr(fleet, "offer", lambda b: pytest.fail("the hub built"))
    return scripted, fleet


def test_a_leaf_whose_hub_it_built_itself_goes_back_to_the_base_image_before_the_move(
        runner, subject, earlier, tmp_path, monkeypatch):
    """Run 22:21 on acc-leaf1: the leaf ran a Hub a VM hub had built and served
    it, main's installer said "Hub already installed and answering" and left
    it, and five journeys measured a leaf that never moved. The installer's
    rule decides: such a guest is reset, installed fresh and adopted, and the
    hub forgets the row the old install answered for first."""
    scripted, fleet = _source_built(runner, monkeypatch, tmp_path, earlier, subject,
                                    keys={"hub": "hub-key", "leaf-a": "published-key"})
    notes = SimpleNamespace(lines=[], note=lambda text: notes.lines.append(text))
    state = runner.stage_prior(fleet, fleet.leaves[0], journey=notes)
    assert state["sha"] == PRIOR_SHA
    assert scripted.reset_names == ["leaf-a"]
    assert scripted.forgotten == ["machine-leaf-a"]
    assert scripted.installed == ["leaf-a"] and scripted.adopted == ["leaf-a"]
    order = [c[1] for c in scripted.calls if c[1] == "reset"
             or (len(c) > 3 and "install.sh --yes" in c[3])]
    assert order == ["reset", "ssh"], "reset first, then the fresh install"
    assert any("built itself" in line and "base image" in line for line in notes.lines)
    assert scripted.keys["leaf-a"] == "hub-key"


def test_a_move_the_installer_declined_fails_by_name_before_adoption(
        runner, subject, earlier, tmp_path, monkeypatch):
    scripted, fleet = _source_built(runner, monkeypatch, tmp_path, earlier, subject,
                                    keys={"hub": "hub-key", "leaf-a": "published-key"},
                                    sticky=True)
    scripted.source_built = set()  # a published Hub, installed over in place
    with pytest.raises(runner.AcceptanceFailure, match="after the move.*left the Hub it found"):
        runner.stage_prior(fleet, fleet.leaves[0])
    assert scripted.installed == ["leaf-a"] and scripted.adopted == []


def test_a_following_leaf_on_an_unnamed_commit_goes_onto_the_ref_under_test_when_a_local_hub_names_no_prior(
        runner, subject, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    scripted = KeyedFleet(keys={"leaf-a": "hub-key"}, release=CANDIDATE, sha="3" * 40)
    fleet = runner.Fleet(LOCAL_PLAN, run=scripted)
    fleet.prior, fleet.build = None, subject
    monkeypatch.setattr(fleet.hub, "trust_key", lambda: "hub-key")
    monkeypatch.setattr(runner, "revoked", lambda f, g: False)
    moved = []
    monkeypatch.setattr(runner, "move", lambda f, g, target, running, journey, **kw:
                        moved.append((g.name, target.sha, running, kw.get("why"))))
    runner.reach(fleet, fleet.leaves[0])
    assert moved == [("leaf-a", HEAD_SHA, "3" * 40, "which this run never named, and the hub is localhost")]
    # On a named commit it is left alone.
    scripted.sha = HEAD_SHA
    runner.reach(fleet, fleet.leaves[0])
    assert len(moved) == 1


def test_a_leaf_with_no_host_at_all_is_installed_onto_the_run_s_ref_and_adopted(
        runner, subject, earlier, tmp_path, monkeypatch):
    scripted, fleet, offered = _keyed(runner, monkeypatch, tmp_path, earlier, subject,
                                      keys={"hub": "hub-key", "leaf-a": "", "fresh": ""})
    fleet.fresh = runner.Guest("fresh", Path("/bin/vm.sh"), run=scripted)
    moved = []
    monkeypatch.setattr(runner, "move", lambda f, g, target, running, journey, **kw:
                        moved.append((g.name, target.sha)))
    runner.reach(fleet, fleet.leaves[0])
    runner.reach(fleet, fleet.fresh)
    assert moved == [("leaf-a", PRIOR_SHA)], "the pristine guest alone stays empty"
    assert offered == []


def test_a_cast_that_parks_the_hub_asks_it_nothing(runner, subject, earlier, tmp_path, monkeypatch):
    """Run 20260925-014140, shell_flip on a VM hub: the flip casts both leaves on
    a two-slot host, which parks the hub, and reach() then asked that parked
    hub for its device list — an ssh into a stopped Mac, timed out, and the
    flip failed before the lab ever stood in for the hub."""
    scripted, fleet, offered = _keyed(runner, monkeypatch, tmp_path, earlier, subject,
                                      keys={"hub": "hub-key", "leaf-a": "hub-key",
                                            "leaf-b": "hub-key"})
    fleet.slots = 2
    fleet.cast(*runner.CAST["shell_flip"](fleet))
    assert [call[2] for call in scripted.calls if call[1] == "stop"] == ["hub"]
    assert fleet.parked == {"hub"}
    assert not any(call[1] == "ssh" and call[2] == "hub" for call in scripted.calls), \
        "the parked hub was asked something"
    assert offered == [] and scripted.installed == [] and scripted.adopted == []
    # Cast again with the hub, and it is no longer parked: the other leaf is.
    fleet.cast(fleet.hub, fleet.leaves[0])
    assert fleet.parked == {"leaf-b"}
    assert any(call[1] == "ssh" and call[2] == "hub" for call in scripted.calls)


def test_a_leaf_with_no_host_cannot_be_cast_beside_a_parked_hub(
        runner, subject, earlier, tmp_path, monkeypatch):
    """Installing and adopting such a leaf needs the hub; with it parked the
    journey is refused by name rather than by an ssh timeout."""
    scripted, fleet, _ = _keyed(runner, monkeypatch, tmp_path, earlier, subject,
                                keys={"hub": "hub-key", "leaf-a": "hub-key", "leaf-b": ""})
    fleet.slots = 2
    with pytest.raises(runner.AcceptanceFailure, match="leaf-b has no host, and the hub is parked"):
        fleet.cast(*runner.CAST["shell_flip"](fleet))
    assert scripted.installed == [] and scripted.adopted == []


def test_an_interpreter_without_the_host_dependencies_is_refused_before_any_journey(
        runner, tmp_path, monkeypatch, capsys):
    """Run 20260925-014140: nine journeys in, shell_adopt imported
    jstack_host.enrolment for a peer name and died on fastapi — the verify
    scenario had started the runner under a bare python3. The interpreter is
    checked before the plan is read or a guest boots."""
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"vm_tool": "/bin/vm.sh", "hub": "hub", "disposable": True}))
    monkeypatch.setitem(sys.modules, "jstack_host.enrolment", None)
    monkeypatch.setattr("sys.argv", ["accept", "--ref", "dev",
                                     "--receipts", str(tmp_path / "r"), "--plan", str(plan)])
    with pytest.raises(SystemExit) as exit_code:
        runner.main()
    assert exit_code.value.code == 2
    err = capsys.readouterr().err
    assert "jstack_host.enrolment" in err and "host/pyproject.toml" in err
