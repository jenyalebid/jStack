"""A disposable fleet built from this very tree, and never from anything else.

`up` builds a candidate out of the checkout this file lives in — the release
channel is that tree's branch (#115), so a production hub is structurally
never offered it — then provisions the plan's guests from those exact bytes.
`reset` re-provisions from the recorded candidate; `down` parks every guest.
The machine running this only edits, builds and drives VMs: nothing here
installs on it, restarts a service on it, or writes its host state.
Plan schema and worked commands: docs/sandbox-fleet.md.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


class SandboxError(RuntimeError):
    """A refusal or a failed step; either ends the run with its reason."""


def tree() -> Path:
    return Path(__file__).resolve().parents[2]


def step(message: str) -> None:
    print(f"== {time.strftime('%H:%M:%S')} {message}", flush=True)


def load_plan(path: Path) -> dict:
    plan = json.loads(path.read_text())
    if plan.get("production") or not plan.get("disposable"):
        raise SandboxError("the sandbox drives only a plan marked disposable")
    for field in ("vm_tool", "hub", "provision_hub"):
        if not plan.get(field):
            raise SandboxError(f"the plan names no {field}")
    if plan.get("leaves") and not plan.get("provision_leaf"):
        raise SandboxError("a plan with leaves names no provision_leaf")
    guests = [plan["hub"], *plan.get("leaves", [])]
    if len(set(guests)) != len(guests):
        raise SandboxError(f"the plan names a guest twice: {guests}")
    for name in guests:
        # The release rig owns the acc-* guests; a sandbox that resets one
        # destroys a qualification run in flight.
        if name.startswith("acc-"):
            raise SandboxError(f"{name}: the acc-* guests belong to the release rig")
    return plan


def state_path(plan_path: Path) -> Path:
    return plan_path.with_suffix(".state.json")


def release_config(plan: dict, plan_path: Path) -> dict:
    """The publisher's own configuration, retargeted at this tree.

    `local_catalog` is dropped: that variant embeds the production hub's
    private capabilities and doubles the hub build for a fleet that never
    receives it. `acceptance` runs this tree's runner over this plan, so
    `release.sh qualify` against the sandbox needs nothing further.
    """
    base = json.loads(Path(plan["release_config"]).expanduser().read_text())
    derived = dict(base)
    derived["stack_repo"] = str(tree())
    derived.pop("local_catalog", None)
    derived["acceptance"] = [
        "/usr/bin/env", "PYTHONPATH=" + str(tree() / "host"), "VM_NET=softnet",
        sys.executable, str(tree() / "host/tools/managed_update_accept.py"),
        "--plan", str(plan_path.resolve())]
    return derived


def _run(argv: list[str], *, check: bool = True, env: dict | None = None) -> int:
    done = subprocess.run(argv, env=env)
    if check and done.returncode:
        raise SandboxError(f"exited {done.returncode}: {' '.join(argv)}")
    return done.returncode


def _stream(argv: list[str], *, env: dict | None = None) -> str:
    """Echo a long command's output live and hand back its last non-empty line."""
    process = subprocess.Popen(argv, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, env=env)
    last = ""
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
        if line.strip():
            last = line.strip()
    if process.wait():
        raise SandboxError(f"exited {process.returncode}: {' '.join(argv)}")
    return last


def build(plan: dict, plan_path: Path, *, notes: str, reuse_client: Path | None = None,
          stream=_stream) -> Path:
    import os
    branch = subprocess.run(["git", "-C", str(tree()), "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    config = release_config(plan, plan_path)
    config_path = plan_path.with_suffix(".release.json")
    config_path.write_text(json.dumps(config, indent=2))
    step(f"building a candidate from {tree()} (channel {branch or 'unknown'})")
    argv = ["/bin/bash", str(tree() / "release.sh"), "build", "--notes", notes]
    if reuse_client is not None:
        argv += ["--reuse-client", str(reuse_client)]
    env = {**os.environ, "JSTACK_RELEASE_CONFIG": str(config_path)}
    # A branch worktree carries no release venv of its own; the plan names the
    # machine's audited 3.12 interpreter and release.sh's PYTHONPATH still
    # resolves this tree's packages ahead of whatever that venv has installed.
    if plan.get("release_python"):
        env["JSTACK_RELEASE_PYTHON"] = str(Path(plan["release_python"]).expanduser())
    candidate = Path(stream(argv, env=env))
    if not (candidate / "candidate.json").is_file():
        raise SandboxError(f"the build reported no candidate: {candidate}")
    state_path(plan_path).write_text(json.dumps(
        {"candidate": str(candidate), "branch": branch,
         "built": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=2))
    step(f"candidate {candidate.name}")
    return candidate


def recorded_candidate(plan_path: Path) -> Path:
    record = state_path(plan_path)
    if not record.is_file():
        raise SandboxError("no candidate recorded for this plan — run build (or up) first")
    return Path(json.loads(record.read_text())["candidate"])


def _provision(plan: dict, template: str, *, guest: str, candidate: Path, runner) -> None:
    command = template.format(guest=guest, candidate=str(candidate),
                              hub=plan["hub"], tree=str(tree()))
    runner(["/bin/bash", "-c", command])


def fleet_up(plan: dict, candidate: Path, *, runner=_run) -> dict:
    if not (candidate / "candidate.json").is_file():
        raise SandboxError(f"{candidate} holds no signed candidate")
    vm = str(Path(plan["vm_tool"]).expanduser())
    hub, leaves = plan["hub"], plan.get("leaves", [])
    for name in (hub, *leaves):
        runner([vm, "stop", name], check=False)
    step(f"{hub}: reset + install the candidate")
    runner([vm, "reset", hub])
    _provision(plan, plan["provision_hub"], guest=hub, candidate=candidate, runner=runner)
    for leaf in leaves:
        # Two Virtualization.framework slots: the hub stays up through each
        # adoption, and a finished leaf is parked before the next one boots.
        step(f"{leaf}: reset + install + adopt to {hub}")
        runner([vm, "reset", leaf])
        _provision(plan, plan["provision_leaf"], guest=leaf, candidate=candidate, runner=runner)
        runner([vm, "stop", leaf])
    step(f"fleet ready: {hub} running, {len(leaves)} leaves provisioned and parked")
    return {"hub": hub, "leaves": leaves, "candidate": str(candidate)}


def fleet_down(plan: dict, *, runner=_run) -> None:
    vm = str(Path(plan["vm_tool"]).expanduser())
    for name in (plan["hub"], *plan.get("leaves", [])):
        runner([vm, "stop", name], check=False)
    step("fleet parked")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("up", "build"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--notes", default=None)
        cmd.add_argument("--reuse-client", type=Path, default=None)
        if name == "up":
            cmd.add_argument("--candidate", type=Path, default=None,
                             help="provision from an already built candidate instead")
    commands.add_parser("reset").add_argument("--candidate", type=Path, default=None)
    commands.add_parser("down")
    args = parser.parse_args()
    plan = load_plan(args.plan)
    if args.action == "down":
        fleet_down(plan)
        return 0
    if args.action in ("build", "up") and getattr(args, "candidate", None) is None:
        notes = args.notes or f"sandbox build ({args.plan.stem})"
        candidate = build(plan, args.plan, notes=notes, reuse_client=args.reuse_client)
        if args.action == "build":
            print(candidate)
            return 0
    else:
        candidate = args.candidate or recorded_candidate(args.plan)
    print(json.dumps(fleet_up(plan, candidate), indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SandboxError as error:
        print(f"sandbox: {error}", file=sys.stderr)
        sys.exit(2)
