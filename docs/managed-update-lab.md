# Disposable update lab

These tools are diagnostic fixtures, **not production acceptance receipts**.
They exercise signed candidates, real HTTP APIs, launchd, bundle replacement
and the actual supervisor. They do not exercise real enrollment or cellular.

Use disposable macOS GUI guests only. Install the previous stack, menu and
Developer ID client first. Keep the base image stopped. Do not run against a
personal Mac, a real fleet's state directory or a production feed.

`host/tools/managed_update_lab.py` requires `--root` whose basename contains
`updates-lab`; it refuses an existing directory without its own marker. Its
hub uses a separate state, agent root, feed, tmux socket and nonproduction port.

```sh
PYTHONPATH=host python host/tools/managed_update_lab.py --root /private/lab/updates-lab serve
PYTHONPATH=host python host/tools/managed_update_lab.py --root /private/lab/updates-lab \
  register updates-leaf --vm-tool /path/to/vm.sh --bootstrap host/jstack_host
PYTHONPATH=host python host/tools/managed_update_lab.py --root /private/lab/updates-lab \
  offer /path/to/signed/candidate
PYTHONPATH=host python host/tools/managed_update_lab.py --root /private/lab/updates-lab \
  queue MACHINE_ID --request unique-test-name
PYTHONPATH=host python host/tools/managed_update_lab.py --root /private/lab/updates-lab inventory
PYTHONPATH=host python host/tools/managed_update_lab.py --root /private/lab/updates-lab record /path/to/evidence.json
```

Use the interpreter containing the host's dependencies. `register` requires
the disposable guest account `/Users/admin`. It refuses to replace an existing
parent. It provisions fixture credentials privately, installs updater bootstrap
in explicit candidate-test mode, and never prints tokens. If host-to-guest
network access needs a pre-existing SSH relay, pass its local `--relay-port`;
record that limitation in the evidence. Do not call this a mesh proof.

## Real failure injection

Copy `host/tools/managed_update_fault.py` into the guest. Run it with that
guest's host Python, before queueing a new update:

```sh
python /path/to/managed_update_fault.py interruption
python /path/to/managed_update_fault.py rollback
```

The first waits for the real applying journal and a moved bundle, then kills
only the test updater. Launchd must restart it and restore the previous release.
The second pauses the updater during apply, temporarily withholds the staged
client bundle, resumes it, and expects rollback. It restores the withheld
staging path afterward. Neither changes the journal or substitutes a backend.
Both refuse a nonfixture account or a configuration without candidate-test.
Queue a normal retry afterward and independently verify versions and requests.

For offline catch-up, stop the guest, wait until its report is stale (90s),
queue its update and save the `pending/offline` inventory. Boot it in GUI mode
and require the **same job ID** to become current with fresh observations.

For revocation, stop only the fixture updater, queue a different offered
release, run `revoke MACHINE_ID`, then restart the updater. Record cancellation,
the rejected requests and unchanged installed release. Revocation is not undone;
any later adoption must mint a new credential, never resurrect the old one.

## Evidence boundaries

- A menu test clicks the actual Update action, not just `/updates/queue`.
- A session test records the original provider PID, updates, sends input
  through the reconnected app and observes a new reply. Capturing old terminal
  scrollback alone fails this check.
- Inventory must name the host **and updater** loaded source, plugin versions,
  both Mac app versions and fresh contact. Source checkout HEAD is not proof.
- The isolated hub fixture lacks its own managed supervisor. It must report
  unknown/offline, and cannot satisfy hub self-update or complete Update All.
- Baseline installation followed by upgrade is not exact-candidate fresh install.
- `record` writes fixture-only observations and job history, never the nine
  publisher receipts. Keep private machine details out of public repositories.
