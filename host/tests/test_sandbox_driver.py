"""The sandbox driver's refusals and its exact command sequence.

What is pinned: the fleet it drives is the plan's and only the plan's, the
release rig's acc-* guests are never touchable, the derived release
configuration builds from the tree the driver lives in, and a slot-limited
host parks each finished leaf before the next one boots. The commands are
scripted, never run — booting is what the live rig does.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

DRIVER = Path(__file__).resolve().parents[1] / "tools/sandbox.py"
spec = importlib.util.spec_from_file_location("sandbox", DRIVER)
sandbox = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sandbox)


def plan_file(tmp_path, **overrides) -> Path:
    plan = {"disposable": True, "vm_tool": "/lab/vm.sh", "hub": "dev-hub",
            "leaves": ["dev-leaf1", "dev-leaf2"],
            "release_config": str(tmp_path / "release-config.json"),
            "provision_hub": "provision {guest} {candidate}",
            "provision_leaf": "provision {guest} {candidate} --adopt-to {hub}"}
    plan.update(overrides)
    path = tmp_path / "dev-plan.json"
    path.write_text(json.dumps(plan))
    return path


def candidate_dir(tmp_path) -> Path:
    directory = tmp_path / "candidate"
    directory.mkdir()
    (directory / "candidate.json").write_text("{}")
    return directory


def test_only_a_disposable_plan_is_driven(tmp_path):
    with pytest.raises(sandbox.SandboxError, match="disposable"):
        sandbox.load_plan(plan_file(tmp_path, disposable=False))
    with pytest.raises(sandbox.SandboxError, match="disposable"):
        sandbox.load_plan(plan_file(tmp_path, production=True))


def test_the_release_rigs_guests_are_never_touchable(tmp_path):
    with pytest.raises(sandbox.SandboxError, match="release rig"):
        sandbox.load_plan(plan_file(tmp_path, leaves=["dev-leaf1", "acc-leaf1"]))
    with pytest.raises(sandbox.SandboxError, match="release rig"):
        sandbox.load_plan(plan_file(tmp_path, hub="acc-hub"))


def test_a_malformed_fleet_is_refused_with_its_reason(tmp_path):
    with pytest.raises(sandbox.SandboxError, match="provision_leaf"):
        sandbox.load_plan(plan_file(tmp_path, provision_leaf=None))
    with pytest.raises(sandbox.SandboxError, match="twice"):
        sandbox.load_plan(plan_file(tmp_path, leaves=["dev-hub"]))
    with pytest.raises(sandbox.SandboxError, match="vm_tool"):
        sandbox.load_plan(plan_file(tmp_path, vm_tool=""))


def test_the_derived_configuration_builds_from_this_tree(tmp_path):
    (tmp_path / "release-config.json").write_text(json.dumps(
        {"stack_repo": "/Users/production/jStack", "local_catalog": "/private/catalog.json",
         "private_key": "/keys/ed25519", "candidates_dir": "/builds"}))
    plan_path = plan_file(tmp_path)
    derived = sandbox.release_config(sandbox.load_plan(plan_path), plan_path)
    assert derived["stack_repo"] == str(sandbox.tree())
    assert "local_catalog" not in derived
    assert derived["private_key"] == "/keys/ed25519"
    runner = " ".join(derived["acceptance"])
    assert str(sandbox.tree() / "host/tools/managed_update_accept.py") in runner
    assert str(plan_path.resolve()) in runner
    # The runner must import this tree's packages, not an installed checkout's.
    assert f"PYTHONPATH={sandbox.tree() / 'host'}" in derived["acceptance"]


def test_up_parks_each_finished_leaf_before_the_next_boots(tmp_path):
    plan = sandbox.load_plan(plan_file(tmp_path))
    candidate = candidate_dir(tmp_path)
    calls = []
    def runner(argv, *, check=True, env=None):
        calls.append((tuple(argv), check))
        return 0
    summary = sandbox.fleet_up(plan, candidate, runner=runner)
    flat = [" ".join(argv) for argv, _ in calls]
    assert flat[:3] == ["/lab/vm.sh stop dev-hub", "/lab/vm.sh stop dev-leaf1",
                        "/lab/vm.sh stop dev-leaf2"]
    assert all(not check for _, check in calls[:3])
    assert flat[3] == "/lab/vm.sh reset dev-hub"
    assert flat[4] == f"/bin/bash -c provision dev-hub {candidate}"
    assert flat[5:8] == ["/lab/vm.sh reset dev-leaf1",
                         f"/bin/bash -c provision dev-leaf1 {candidate} --adopt-to dev-hub",
                         "/lab/vm.sh stop dev-leaf1"]
    assert flat[8:] == ["/lab/vm.sh reset dev-leaf2",
                        f"/bin/bash -c provision dev-leaf2 {candidate} --adopt-to dev-hub",
                        "/lab/vm.sh stop dev-leaf2"]
    assert summary == {"hub": "dev-hub", "leaves": ["dev-leaf1", "dev-leaf2"],
                       "candidate": str(candidate)}


def test_a_directory_without_a_signed_candidate_provisions_nothing(tmp_path):
    plan = sandbox.load_plan(plan_file(tmp_path))
    calls = []
    with pytest.raises(sandbox.SandboxError, match="no signed candidate"):
        sandbox.fleet_up(plan, tmp_path / "empty", runner=lambda *a, **k: calls.append(a))
    assert not calls


def test_reset_without_a_recorded_candidate_says_to_build(tmp_path):
    with pytest.raises(sandbox.SandboxError, match="run build"):
        sandbox.recorded_candidate(plan_file(tmp_path))


def test_build_records_the_candidate_for_reset(tmp_path, monkeypatch):
    (tmp_path / "release-config.json").write_text(json.dumps(
        {"stack_repo": "/Users/production/jStack", "candidates_dir": "/builds"}))
    plan_path = plan_file(tmp_path)
    plan = sandbox.load_plan(plan_path)
    candidate = candidate_dir(tmp_path)
    seen = {}
    def stream(argv, *, env=None):
        seen["argv"], seen["env"] = argv, env
        return str(candidate)
    result = sandbox.build(plan, plan_path, notes="unit build", stream=stream)
    assert result == candidate
    assert seen["argv"][1] == str(sandbox.tree() / "release.sh")
    assert "unit build" in seen["argv"]
    derived = json.loads(Path(seen["env"]["JSTACK_RELEASE_CONFIG"]).read_text())
    assert derived["stack_repo"] == str(sandbox.tree())
    assert sandbox.recorded_candidate(plan_path) == candidate


def test_down_parks_every_guest_in_the_plan(tmp_path):
    plan = sandbox.load_plan(plan_file(tmp_path))
    calls = []
    sandbox.fleet_down(plan, runner=lambda argv, *, check=True, env=None: calls.append(tuple(argv)))
    assert calls == [("/lab/vm.sh", "stop", "dev-hub"), ("/lab/vm.sh", "stop", "dev-leaf1"),
                     ("/lab/vm.sh", "stop", "dev-leaf2")]
