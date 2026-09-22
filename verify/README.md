# verify — the product acceptance suite

Real machines, real UI. Every scenario boots a GUI macOS guest, runs the
product the way a user would — the README installer from the CDN, the guest's
own Terminal, the published client app — and ends with a verdict read from
what actually happened on that screen. No scenario proves a UI surface
through an API.

```
verify/run.sh list                    the catalog
verify/run.sh run hub/full-reset      one scenario
verify/run.sh run plugin              a category
```

Each run leaves receipts — terminal log, screenshots, verdict — under
`$JSTACK_VERIFY_RECEIPTS` (default `~/.local/state/jstack-verify`). The guest
stays booted and on screen afterward.

## Categories — the viable configurations

| Category | Configuration | Scenarios |
| --- | --- | --- |
| `plugin/` | jStack Plugin alone — Claude Code, no Hub, no client | `install`, `commands`, `scheduler` |
| `hub/` | jStack Hub alone (the Plugin ships with it) | `install`, `uninstall`, `full-reset`, `update` |
| `full/` | Hub + jRemote on one Mac — the full product | `client-install`, `pair` |
| `device/` | The client under its own XCUITest suite | `uitests-ios`, `uitests-mac` |

## What a scenario needs

- `vm.sh` (`$VM_SH`, default `~/Operations/Infrastructure/scripts/vm.sh`) —
  tart guests, always GUI, `term`/`shot` for visible runs.
- Pristine scenarios clone the stock base image; nothing to prepare.
- `plugin/*` derive from `$JSTACK_VERIFY_AUTHED_BASE` (default
  `jstack-base-authed`) — a stopped working image with Claude Code signed in.
- `device/*` derive from `$JSTACK_VERIFY_XCODE_BASE` (default `jr-xcode-base`)
  — a stopped working image with Xcode; the client source ships in from
  `$JSTACK_VERIFY_CLIENT_SRC` because the app repo is private.
- `hub/update` delegates to the existing lab
  (`host/tools/managed_update_accept.py`, nine receipts) and needs
  `JSTACK_VERIFY_CANDIDATE` and `JSTACK_VERIFY_UPDATE_PLAN`.

## Rules

- One scenario, one guest, re-cloned every run — a scenario never inherits a
  previous run's dirt, and running one costs only that one's time.
- A payload prints `FAIL …` for anything wrong and its `DONE-…` marker last;
  a run that never reaches the marker fails. Silence is never a pass.
- The one interactive install question is answered with the installer's own
  `--agent` flag, noted in the payload — never by stripping the prompt.
- UI flows (pairing) read and type through the guest's real windows; on a
  miss they dump the UI element tree into the receipts so the fix starts
  from a map, not a guess.
