# The sandbox fleet

`host/tools/sandbox.py` stands up a disposable VM fleet built from the tree it
lives in, so a feature branch gets end-to-end testing without a bespoke rig and
without touching anything production runs. The candidate it builds carries the
tree's branch as its release channel, and a hub declines offers from a channel
that is not its own — a production hub is structurally never offered a sandbox
build.

## Commands

```sh
python3 host/tools/sandbox.py --plan dev-plan.json up      # build + provision
python3 host/tools/sandbox.py --plan dev-plan.json build   # candidate only
python3 host/tools/sandbox.py --plan dev-plan.json reset   # re-provision, no rebuild
python3 host/tools/sandbox.py --plan dev-plan.json down    # park every guest
```

`build` records its candidate next to the plan (`<plan>.state.json`), which is
what `reset` re-provisions from; `up --candidate DIR` and `reset --candidate
DIR` take an explicit one instead. `build --reuse-client DIR` passes through
to the release builder for a branch that leaves the client untouched. A full
`up` takes on the order of an hour — run it in a detached session.

## The plan

A JSON file the operator keeps outside the repository, since it names
machine-local paths:

```json
{
  "disposable": true,
  "vm_tool": "path to the vm driver (vm.sh or a wrapper)",
  "hub": "dev-hub",
  "leaves": ["dev-leaf1", "dev-leaf2"],
  "release_config": "path to the machine's private release configuration",
  "release_python": "the machine's audited 3.12 release interpreter (a branch worktree carries no venv of its own)",
  "provision_hub": "command run for the hub; {guest} {candidate} {hub} {tree}",
  "provision_leaf": "command run per leaf, same placeholders"
}
```

The leaf list is a list: zero, one or ten leaves are all the same plan. The
provision commands are the operator's own (typically the acceptance
provisioner), formatted with the guest name, the candidate directory, the
hub's guest name and the sandbox tree.

## What it refuses

- A plan not marked `disposable`, or marked `production`.
- Any guest named `acc-*` — those belong to the release qualification rig.
- A candidate directory without a signed `candidate.json`.

The machine running the driver only builds and drives VMs. The driver never
installs on it, never restarts a service on it, and never writes its host
state; the derived release configuration retargets `stack_repo` at the
sandbox tree, drops the production hub's private `local_catalog` variant, and
points `acceptance` at this tree's runner over this plan — so
`release.sh qualify <candidate> --receipts <dir>` runs the acceptance
journeys against the sandbox fleet with nothing further configured.
