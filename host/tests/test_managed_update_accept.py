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
from pathlib import Path

import pytest

from jstack_host import acceptance, release_manifest as releases

TOOLS = Path(__file__).resolve().parents[1] / "tools/managed_update_accept.py"
PRIOR, CANDIDATE = "20260915T000000Z-aaaaaaaa", "20260916T192622Z-1737a381"


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
    assert runner.unsupported(fleet, "cellular").startswith("no test phone")
    assert runner.unsupported(fleet, "fresh_install") == "the plan names no pristine guest"
    assert runner.unsupported(fleet, "upgrade") is None


def test_a_plan_that_is_not_marked_disposable_is_refused(runner, tmp_path, candidate, monkeypatch):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"vm_tool": "/bin/vm.sh", "hub": "production-hub"}))
    monkeypatch.setattr("sys.argv", ["accept", "--candidate", str(candidate.dir),
                                     "--receipts", str(tmp_path / "r"), "--plan", str(plan)])
    with pytest.raises(SystemExit) as exit_code:
        runner.main()
    assert exit_code.value.code == 2
