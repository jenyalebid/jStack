"""The acceptance runner's journeys, driven against a scripted fleet.

A runner that reports what it hoped for is worse than no runner, so what is
pinned here is the refusal: a guest that claims `current` while still running
the old release, a menu credential that is allowed to queue somebody else's
update, a fleet with one leaf standing in for two. Each of those must end as
`failed` or `incomplete`, never as a receipt that would open the promotion gate.

The fleet is scripted rather than booted — booting is what the live run does.
What these tests own is the logic between the observations.
"""
import hashlib
import importlib.util
import json
import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest

from jstack_host import acceptance, release_manifest as releases

TOOLS = Path(__file__).resolve().parents[1] / "tools/managed_update_accept.py"
PRIOR, CANDIDATE = "20260915T000000Z-aaaaaaaa", "20260916T192622Z-1737a381"


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


def test_credential_revocation_is_the_last_journey(runner):
    # Revocation is permanent; later journeys cannot reuse that leaf's authority.
    assert list(runner.JOURNEYS)[-1] == "revocation"


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location("managed_update_accept", TOOLS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def candidate(tmp_path, runner):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    directory = tmp_path / "candidate"
    directory.mkdir()
    components = {}
    for name in sorted(releases.COMPONENTS):
        body = (name + " bytes").encode()
        (directory / (name + ".zip")).write_bytes(body)
        components[name] = {"file": name + ".zip", "version": "70" if name == "client" else "0.69.3",
                            "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
    manifest = {"schema": 1, "release": CANDIDATE, "components": components,
                "sources": {"stack": "1" * 40, "client": "2" * 40},
                "compatibility": {"protocol": 1, "rollback": True, "platform": "macos",
                                  "architecture": "arm64", "minimum_os": "26.0"},
                "receipts": {}}
    key = Ed25519PrivateKey.generate()
    import base64
    (directory / "candidate.json").write_text(json.dumps(
        releases.sign(manifest, key.private_bytes_raw(), promoted=False)))
    public = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    return runner.Candidate(directory, public)


class ScriptedFleet:
    """One disposable fleet's answers, in the shape vm.sh and the tools give them."""

    def __init__(self, *, release=PRIOR, state="current", client="70", menubar="0.69.3",
                 plugin="0.69.3", denied_status=403):
        self.release, self.state = release, state
        self.client, self.menubar, self.plugin = client, menubar, plugin
        self.denied_status = denied_status
        self.calls: list[list[str]] = []
        self.queued: list[str] = []

    def probe(self, name):
        return {"host_id": "machine-" + name,
                "observed": {"release": self.release,
                             "host_source": {"sha": "1" * 40, "release": self.release, "dirty": False},
                             "updater_source": {"sha": "1" * 40}, "verified": True,
                             "components": {"client": {"installed": self.client, "running_pids": [11]},
                                            "menubar": {"installed": self.menubar, "running_pids": [12]},
                                            "plugins": {"claude": {"version": self.plugin}}}},
                "job": {}, "adopted": True, "managed": True}

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        _, action, name, *rest = argv
        command = rest[0] if rest else ""
        answer: dict = {}
        if action in {"gui", "stop", "cp"}:
            answer = {"ok": True}
        elif "probe" in command:
            answer = self.probe(name)
        elif "--path /updates/queue" in command:
            answer = {"status": self.denied_status, "body": "refused"}
        elif "--path" in command:
            answer = {"status": 200, "body": "accepted"}
        elif "queue" in command:
            target = command.split("--target ")[1].split()[0]
            request = command.split("--request ")[1].split()[0]
            targets = (["machine-hub", "machine-leaf-a", "machine-leaf-b"] if target == "all"
                       else [f"machine-{name}" if target == "self" else target])
            self.queued.extend(targets)
            # The release the fleet actually moves to when a job is queued.
            self.release = CANDIDATE
            # One request ID names one job per machine, however often it arrives.
            answer = {"jobs": [{"id": f"job-{request}-{machine}", "machine": machine}
                               for machine in targets], "errors": []}
        elif "inventory" in command:
            answer = {"release": CANDIDATE, "machines": [
                {"machine": machine, "name": machine, "state": self.state, "supervisor": True,
                 "last_contact": 1.0, "observed": {"release": self.release}, "job": {"id": "job-1"}}
                for machine in {"machine-hub", "machine-leaf-a", "machine-leaf-b", *self.queued}]}
        elif "--path /updates/queue" in command:
            answer = {"status": self.denied_status, "body": "refused"}
        elif "--path" in command:
            answer = {"status": 200, "body": "accepted"}
        return subprocess.CompletedProcess(argv, 0, json.dumps(answer), "")


def build(runner, fleet, **plan):
    return runner.Fleet({"vm_tool": "/bin/vm.sh", "hub": "hub", "leaves": ["leaf-a", "leaf-b"],
                         "prior_candidate": "/tmp/prior", **plan}, run=fleet)


def journey_result(runner, candidate, name, fleet, tmp_path, **plan):
    run = acceptance.Run(tmp_path / "receipts", candidate.manifest)
    with run.journey(name) as journey:
        runner.JOURNEYS[name](journey, build(runner, fleet, **plan), candidate)
    return run.results[name], json.loads((tmp_path / "receipts" / (name + ".json")).read_text())


def test_an_upgrade_that_really_lands_records_every_required_fact(runner, candidate, tmp_path):
    result, receipt = journey_result(runner, candidate, "upgrade", ScriptedFleet(), tmp_path)
    assert result == "passed"
    assert receipt["observed"] == sorted(acceptance.REQUIRED["upgrade"])


def test_a_machine_that_claims_current_on_the_old_release_fails(runner, candidate, tmp_path):
    class Stuck(ScriptedFleet):
        def __call__(self, argv, **kwargs):
            done = super().__call__(argv, **kwargs)
            self.release = PRIOR  # reports current, never actually moves
            return done

    result, receipt = journey_result(runner, candidate, "upgrade", Stuck(), tmp_path)
    assert result == "failed"
    assert "reports release" in receipt["detail"]
    assert "installed_release" in receipt["missing"]


def test_a_stale_app_bundle_under_a_new_host_fails(runner, candidate, tmp_path):
    result, receipt = journey_result(runner, candidate, "upgrade",
                                     ScriptedFleet(client="69"), tmp_path)
    assert result == "failed" and "client is 69" in receipt["detail"]


def test_a_plugin_left_on_the_old_version_fails(runner, candidate, tmp_path):
    result, receipt = journey_result(runner, candidate, "upgrade",
                                     ScriptedFleet(plugin="0.69.2"), tmp_path)
    assert result == "failed" and "plugin claude is 0.69.2" in receipt["detail"]


def test_a_leaf_allowed_to_queue_another_machine_fails_the_fleet_journey(runner, candidate, tmp_path):
    result, receipt = journey_result(runner, candidate, "fleet",
                                     ScriptedFleet(denied_status=200), tmp_path)
    assert result == "failed"
    assert "queue another machine" in receipt["detail"]
    assert receipt["missing"] == ["denied_authority"]


def test_a_rolled_back_machine_never_reads_as_reaching_the_release(runner, candidate, tmp_path):
    result, receipt = journey_result(runner, candidate, "upgrade",
                                     ScriptedFleet(state="rolled_back"), tmp_path)
    assert result == "failed" and "rolled_back" in receipt["detail"]


def test_one_leaf_cannot_stand_in_for_the_contract_s_two(runner, candidate, tmp_path):
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


def test_a_two_slot_host_never_boots_a_third_guest(runner, candidate, tmp_path):
    scripted = SlotCountingFleet()
    fleet = build(runner, scripted, vm_slots=2)
    run = acceptance.Run(tmp_path / "receipts", candidate.manifest)
    with run.journey("fleet") as journey:
        fleet.cast(*runner.CAST["fleet"](fleet))
        runner.JOURNEYS["fleet"](journey, fleet, candidate)
    assert run.results["fleet"] == "passed"
    assert scripted.peak <= 2, "the fleet journey booted more guests than the host has slots"
    assert any(call[1] == "stop" for call in scripted.calls), \
        "a two-slot plan must park a leaf to make room for the other"


def test_a_cast_larger_than_the_slots_is_refused(runner):
    scripted = SlotCountingFleet()
    fleet = build(runner, scripted, vm_slots=2)
    with pytest.raises(runner.AcceptanceFailure):
        fleet.cast(fleet.hub, fleet.leaves[0], fleet.leaves[1])


def test_update_all_wakes_the_mac_enrolled_by_fresh_install(runner, candidate, tmp_path):
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
    wait = fleet.hub.wait_for
    def checked_wait(state, machine, **kwargs):
        if machine == "machine-fresh":
            assert "fresh" in scripted.booted, "waiting for a parked fresh Mac cannot finish"
        return wait(state, machine, **kwargs)
    fleet.hub.wait_for = checked_wait
    run = acceptance.Run(tmp_path / "receipts", candidate.manifest)
    with run.journey("fleet") as journey:
        fleet.cast(*runner.CAST["fleet"](fleet))
        runner.fleet_journey(journey, fleet, candidate)
    assert run.results["fleet"] == "passed"
    assert scripted.peak <= 2


def test_a_plan_without_slots_keeps_every_guest_running(runner, candidate, tmp_path):
    scripted = SlotCountingFleet()
    fleet = build(runner, scripted)
    run = acceptance.Run(tmp_path / "receipts", candidate.manifest)
    with run.journey("fleet") as journey:
        fleet.cast(*runner.CAST["fleet"](fleet))
        runner.JOURNEYS["fleet"](journey, fleet, candidate)
    assert run.results["fleet"] == "passed"
    assert not any(call[1] == "stop" for call in scripted.calls), \
        "without vm_slots nothing may be parked"


def test_a_plan_that_is_not_marked_disposable_is_refused(runner, tmp_path, candidate, monkeypatch):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"vm_tool": "/bin/vm.sh", "hub": "production-hub"}))
    monkeypatch.setattr("sys.argv", ["accept", "--candidate", str(candidate.dir),
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


def test_fresh_install_refuses_existing_menu_before_any_write(runner, candidate):
    class Guest:
        name = "fresh"
        def sh(self, command):
            assert "for p in" in command
            return "/Applications/JStack Host.app"

    with pytest.raises(runner.AcceptanceFailure, match="not pristine"):
        runner.install_candidate(Guest(), candidate, fresh=True)


def test_candidate_installer_installs_the_sealed_hub_from_candidate_bytes(runner, candidate, monkeypatch):
    commands = []
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    class Guest:
        name = "fresh"
        def sh(self, command, **kwargs):
            commands.append(command)
            if "route -n get default" in command:
                return "192.168.64.1\n"
            return ""
        def copy(self, *args):
            pass

    runner.install_candidate(Guest(), candidate, fresh=True)
    assert not any("| /usr/bin/tail" in command for command in commands)
    assert not any("ditto -x -k" in command and "~/Applications" in command for command in commands)
    # The one installer does the menu bar too; its own install.sh is an
    # internal step that refuses a direct call.
    install = next(command for command in commands if "install.sh --yes" in command)
    assert "--no-claude --no-app" in install
    assert not any("menubar/install.sh" in command for command in commands)
    # The Hub zip must come from the candidate, not the published release.
    assert "JSTACK_REPO_URL=http://192.168.64.1:" in install
    # The sealed provisioner writes candidate_test false; the lab flips it
    # and restarts the updater so the run verifies unpromoted envelopes.
    flip = next(command for command in commands if "candidate_test" in command)
    assert "True" in flip
    assert commands.index(flip) > commands.index(install)
    assert any("kickstart" in command and "live.jstack.hub.updater" in command
               for command in commands)


def test_candidate_repo_serves_the_tag_and_the_candidate_assets(runner, candidate):
    from urllib.error import HTTPError
    from urllib.request import urlopen
    with runner.candidate_repo(candidate) as port:
        base = f"http://127.0.0.1:{port}/jstack.git"
        refs = urlopen(base + "/info/refs?service=git-upload-pack").read().decode()
        assert refs == f"{candidate.stack_sha}\trefs/tags/stack-release-{candidate.release}\n"
        asset = urlopen(base + f"/releases/download/any-tag/{candidate.file('menubar').name}").read()
        assert asset == candidate.file("menubar").read_bytes()
        with pytest.raises(HTTPError):
            urlopen(base + "/releases/download/any-tag/absent.zip")
        with pytest.raises(HTTPError):
            urlopen(base + f"/{candidate.file('menubar').name}")


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
        runner, candidate, tmp_path, monkeypatch):
    receipts = tmp_path / "receipts"
    run = acceptance.Run(receipts, candidate.manifest)
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
    monkeypatch.setattr(runner, "Candidate", lambda *args: candidate)
    fleet = build(runner, ScriptedFleet())
    monkeypatch.setattr(fleet, "offer", lambda _: None)
    monkeypatch.setattr(runner, "Fleet", lambda *args, **kwargs: fleet)
    monkeypatch.setattr("sys.argv", ["accept", "--candidate", str(candidate.dir),
                                     "--receipts", str(receipts), "--plan", str(plan),
                                     "--only", "fleet"])
    assert runner.main() == 1  # Other required journeys still missing.
    assert (receipts / "upgrade.json").read_bytes() == original
    assert acceptance.inspect(receipts, candidate.manifest)["upgrade"]["state"] == "passed"


def test_staging_prior_restores_candidate_offer_on_update_failure(
        runner, candidate, tmp_path, monkeypatch):
    from types import SimpleNamespace
    prior = tmp_path / "prior"
    prior.mkdir()
    (prior / "candidate.json").write_text(json.dumps({"manifest": {"release": PRIOR}}))
    fleet = build(runner, ScriptedFleet(release=CANDIDATE), prior_candidate=str(prior))
    fleet.candidate = candidate
    offered = []
    monkeypatch.setattr(runner, "Candidate", lambda *args: SimpleNamespace(release=PRIOR))
    monkeypatch.setattr(fleet, "offer", lambda c: offered.append(c.release))

    def failed(*args, **kwargs):
        raise runner.AcceptanceFailure("update failed")

    monkeypatch.setattr(fleet.hub, "wait_for", failed)
    with pytest.raises(runner.AcceptanceFailure, match="update failed"):
        runner.stage_prior(fleet, fleet.leaves[0])
    assert offered == [PRIOR, candidate.release]


def test_artifact_fault_is_restored_when_refusal_probe_fails(runner):
    from types import SimpleNamespace
    calls = []
    class Hub:
        def tool_call(self, action):
            calls.append(action)
        def call(self, *args):
            raise runner.AcceptanceFailure("hub disconnected")

    with pytest.raises(runner.AcceptanceFailure, match="disconnected"):
        runner.refused_release(SimpleNamespace(plan={}, hub=Hub()),
                               SimpleNamespace(installed=lambda: {"release": PRIOR}), "leaf")
    assert calls == ["tamper", "restore-artifact"]


@pytest.mark.parametrize("detail,valid", [("artifact digest mismatch", True),
                                         ("host unavailable", False)])
def test_hub_refusal_requires_artifact_error_and_unchanged_installation(runner, detail, valid):
    from types import SimpleNamespace
    state = dict(release=PRIOR, sha="a" * 40, client="69", menubar="69")
    guest = SimpleNamespace(installed=lambda: state)
    hub = SimpleNamespace(tool_call=lambda _: None,
                          call=lambda *args: {"status": 503, "body": detail})
    fleet = SimpleNamespace(plan={}, hub=hub)
    if valid:
        assert runner.refused_release(fleet, guest, "leaf")["unchanged_release"] == PRIOR
    else:
        with pytest.raises(runner.AcceptanceFailure, match="unrelated"):
            runner.refused_release(fleet, guest, "leaf")


def test_revocation_failure_restores_the_fixture_supervisor(runner, monkeypatch):
    from types import SimpleNamespace
    commands = []
    guest = SimpleNamespace(sh=lambda command: commands.append(command))
    monkeypatch.setattr(runner, "stage_prior", lambda *args: {})

    def fail(*args):
        raise runner.AcceptanceFailure("credential revocation failed")

    hub = SimpleNamespace(queue=lambda *args: {"jobs": [{"id": "queued"}]}, tool_call=fail)
    fleet = SimpleNamespace(leaves=[guest], machine=lambda _: "leaf", hub=hub)
    with pytest.raises(runner.AcceptanceFailure, match="revocation failed"):
        runner.revocation(SimpleNamespace(observe=lambda *args: None), fleet, None)
    assert commands[0].endswith("unregister updater")
    assert commands[-2].endswith("register updater")
    assert "launchctl print" in commands[-1]


def test_a_hub_restarting_into_the_candidate_survives_refused_polls(runner):
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


def test_a_candidate_session_left_on_the_bypass_warning_fails_by_name(runner, monkeypatch):
    ticks = iter([0, 1, 10, 31, 241, 242])
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    guest, state = _bypass_guest(runner, answers_after_nudge=True)

    with pytest.raises(runner.AcceptanceFailure, match="startup watcher never answered"):
        runner.new_session(guest)
    assert state["nudged"] == [], "the runner must never press keys into a candidate's session"
    assert state["reprompted"] == [], "the runner must never feed a candidate's session either"


def test_a_prior_is_not_nudged_before_the_grace_period(runner, monkeypatch):
    ticks = iter([0, 1, 5, 10, 20, 241, 242])
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    guest, state = _bypass_guest(runner, answers_after_nudge=True)

    with pytest.raises(runner.AcceptanceFailure, match="initial pwd request"):
        runner.new_session(guest, prior=True)
    assert state["nudged"] == []


def test_session_survival_requires_an_unaided_session_on_the_candidate(runner):
    assert "candidate_new_session" in acceptance.REQUIRED["session_survival"]


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
    assert "seq 1 180" in calls[1][3]


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


def _fault_fleet(runner, monkeypatch, *, fault_result, prior_release=PRIOR):
    queued = []
    hub = SimpleNamespace(queue=lambda machine, request: queued.append(request) or
                          {"jobs": [{"id": "job-" + request.split("-")[1]}]},
                          wait_for=lambda state, machine: {"state": state, "job": {}})
    guest = SimpleNamespace(name="leaf", installed=lambda: {"release": prior_release,
                                                             "client": "91", "menubar": "20260922"},
                            sh=lambda command, **kwargs: "")
    fleet = SimpleNamespace(leaves=[guest], hub=hub, machine=lambda _: "leaf-id", plan={})
    monkeypatch.setattr(runner, "stage_prior", lambda fleet, guest: guest.installed())
    monkeypatch.setattr(runner, "arm_fault", lambda guest, fault: fault)
    monkeypatch.setattr(runner, "read_fault", lambda process: fault_result)
    return fleet, guest, queued


def test_a_kill_that_rolls_back_for_a_leftover_copy_is_not_recovery(runner, monkeypatch):
    fleet, guest, queued = _fault_fleet(runner, monkeypatch, fault_result={
        "state": "rolled_back", "job": "j1", "detail": "unfinished incoming app requires recovery"})
    journey = acceptance.Journey("interruption")
    with pytest.raises(runner.AcceptanceFailure, match="not from recovery"):
        runner.interruption(journey, fleet, SimpleNamespace(release=CANDIDATE))
    assert journey.observed == {}


def test_a_rollback_caused_by_anything_but_the_withheld_client_fails(runner, monkeypatch):
    fleet, guest, queued = _fault_fleet(runner, monkeypatch, fault_result={
        "state": "rolled_back", "job": "j1", "detail": "unfinished incoming app requires recovery"})
    journey = acceptance.Journey("rollback")
    with pytest.raises(runner.AcceptanceFailure, match="not for the withheld client bundle"):
        runner.rollback(journey, fleet, SimpleNamespace(release=CANDIDATE))
    assert journey.observed == {}


def test_the_prior_s_leftover_copy_is_recorded_and_removed_before_the_retry(runner, monkeypatch):
    fleet, guest, queued = _fault_fleet(runner, monkeypatch, fault_result={
        "state": "rolled_back", "job": "j1", "detail": "updated components failed verification"})
    commands = []
    listing = {"paths": "/Applications/jRemote.app.incoming-" + CANDIDATE + "\n"}
    def shell(command, **kwargs):
        commands.append(command)
        if command == runner.RESIDUE_LISTING:
            found, listing["paths"] = listing["paths"], ""
            return found
        return ""
    guest.sh = shell
    releases = iter([PRIOR, CANDIDATE])
    guest.installed = lambda: {"release": next(releases)}
    monkeypatch.setattr(runner, "reboot_mid_apply",
                        lambda fleet, guest, machine, candidate: {"job": "j3", "state": "rolled_back"})
    journey = acceptance.Journey("interruption")

    runner.interruption(journey, fleet, SimpleNamespace(release=CANDIDATE))

    import shlex
    assert "/bin/rm -rf -- " + shlex.quote("/Applications/jRemote.app.incoming-" + CANDIDATE) in commands
    assert not any("/Applications/*" in command for command in commands), "no shell globs reach the guest's zsh"
    assert any("jRemote.app.incoming-" in line and "#117" in line for line in journey.lines)
    assert commands.index(next(c for c in commands if c.startswith("/bin/rm"))) > 0
    assert journey.observed["retry_current"]["release"] == CANDIDATE
    assert journey.missing == ()


def _reboot_fleet(runner, monkeypatch, tmp_path, *, settled_detail, leftovers=""):
    prior = tmp_path / "prior"
    prior.mkdir()
    offered, queued, power = [], [], []
    monkeypatch.setattr(runner, "Candidate", lambda *args: SimpleNamespace(release=PRIOR))
    monkeypatch.setattr(runner, "arm_fault", lambda guest, fault: fault)
    monkeypatch.setattr(runner, "read_freeze", lambda process: {
        "injected": "freeze", "job": "job-reboot", "pid": 41, "copies": ["/Applications/x.incoming-j"]})
    hub = SimpleNamespace(
        queue=lambda machine, request: queued.append(request) or {"jobs": [{"id": "job-" + request.split("-")[1]}]},
        wait_for=lambda state, machine: {"state": state, "job": {"id": "job-reboot", "detail": settled_detail}})
    installed = iter([{"release": CANDIDATE}, {"release": CANDIDATE}, {"release": PRIOR}])
    guest = SimpleNamespace(name="leaf", installed=lambda: next(installed),
                            stop=lambda: power.append("stop"), start=lambda: power.append("start"),
                            sh=lambda command, **kwargs: leftovers if command == runner.RESIDUE_LISTING else "")
    fleet = SimpleNamespace(prior=prior, hub=hub, offer=lambda c: offered.append(c.release))
    return fleet, guest, offered, queued, power


def test_a_reboot_mid_copy_rolls_back_keeps_the_release_and_the_next_request_lands(
        runner, monkeypatch, tmp_path):
    fleet, guest, offered, queued, power = _reboot_fleet(
        runner, monkeypatch, tmp_path, settled_detail="recovered interrupted application")
    candidate = SimpleNamespace(release=CANDIDATE, public_key="k")

    result = runner.reboot_mid_apply(fleet, guest, "leaf-id", candidate)

    assert power == ["stop", "start"]
    assert result["state"] == "rolled_back" and result["release_kept"] == CANDIDATE
    assert result["retry_release"] == PRIOR and result["frozen_copies"]
    assert offered == [PRIOR, CANDIDATE], "the candidate offer comes back whatever happened"
    assert [request.split("-")[1] for request in queued] == ["reboot", "reboot"]


def test_a_reboot_that_settles_any_other_way_fails_by_its_detail(runner, monkeypatch, tmp_path):
    fleet, guest, offered, queued, power = _reboot_fleet(
        runner, monkeypatch, tmp_path, settled_detail="unfinished incoming app requires recovery")
    with pytest.raises(runner.AcceptanceFailure, match="settled the job as 'unfinished incoming"):
        runner.reboot_mid_apply(fleet, guest, "leaf-id", SimpleNamespace(release=CANDIDATE, public_key="k"))
    assert offered == [PRIOR, CANDIDATE]


def test_a_recovered_updater_that_leaves_a_copy_behind_fails(runner, monkeypatch, tmp_path):
    fleet, guest, offered, queued, power = _reboot_fleet(
        runner, monkeypatch, tmp_path, settled_detail="recovered interrupted application",
        leftovers="/Applications/jStack Hub.app.incoming-job-reboot\n")
    with pytest.raises(runner.AcceptanceFailure, match="left \\['/Applications/jStack Hub.app.incoming"):
        runner.reboot_mid_apply(fleet, guest, "leaf-id", SimpleNamespace(release=CANDIDATE, public_key="k"))


def test_the_reboot_leg_refuses_a_guest_not_on_the_candidate(runner, monkeypatch, tmp_path):
    fleet, guest, offered, queued, power = _reboot_fleet(
        runner, monkeypatch, tmp_path, settled_detail="recovered interrupted application")
    guest.installed = lambda: {"release": PRIOR}
    with pytest.raises(runner.AcceptanceFailure, match="needs the candidate's updater"):
        runner.reboot_mid_apply(fleet, guest, "leaf-id", SimpleNamespace(release=CANDIDATE, public_key="k"))
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
