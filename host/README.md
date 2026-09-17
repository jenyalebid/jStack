# jStack Hub

Current product contract (2026-09-16): [jStack terminology, single installation and release rules](../product.md#core-contract).
Hub is the server/menu on each Mac; Parent Hub manages leaves; host is a
technical machine term. The Plugin and jRemote client are independently usable.
One Hub installation per OS; unique build number and source commit per build;
complete verified changes on main; publication only on explicit request using
the exact tested artifacts. Historical paths and API identifiers below remain
implementation names, not alternative products or installation policies.

The API a phone, an iPad or another Mac reaches this machine through. It serves
your terminal sessions — what is running, what each one said, and a live PTY you
can type into from somewhere else.

## Install

```bash
./install.sh                # from a clone of this repo
./install.sh --dry-run      # print the plan, change nothing
```

Clones nothing you have not already cloned, builds a virtualenv beside the
package, installs a **user** LaunchAgent, and prints a pairing code for the app.
No `sudo`, nothing written outside your home directory.

## Menu bar

The host is a terminal program, so it has no way to tell you it is up. The menu
bar app is that — status, the sessions running right now, and start / stop /
restart. `install.sh` builds it unless you pass `--no-menubar`; it needs a Swift
compiler (`xcode-select --install`) and the host installs fine without it.

```bash
menubar/install.sh              # build and install it on its own
menubar/install.sh --uninstall  # just the icon; the host stays
```

The source installer builds the menu from `menubar/` and runs it under its own
user LaunchAgent. Managed release candidates package a Developer ID signed,
notarized menu. Both are components of the same Hub installation. The menu
runs under its own agent rather than inside any client app,
because a status item dies with the process that made it: an indicator that
only survives while some app is open is one that goes dark while the host it
reports on is still serving.

## Commands

```
jstack-host install          install as a user LaunchAgent
jstack-host pair "iPhone"    a code to type into the app
jstack-host status           installed? loaded? answering?
jstack-host doctor           grade every dependency, with the fix beside each gap
jstack-host serve            run in this terminal instead
jstack-host where            every path this host resolves
jstack-host uninstall        remove the LaunchAgent (state and token stay)
```

## Security

Authenticated routes use per-device credentials and revocation. Managed-leaf
access derives from parent authority; local administration uses a separate
internal credential. `/api/health` is the unauthenticated readiness probe.
Keep credentials private and use the pairing flow to enroll clients.

Binds `0.0.0.0` by default, because a host is reached over a tunnel or across a
LAN and one bound to `127.0.0.1` is a host only this Mac can see. Nothing is
exposed to the internet unless you put it there.

Managed updates stage and verify signed artifacts, replace the existing Hub,
and independently verify the running source after restart. Production fleet
qualification is unfinished; see [managed updates](../docs/managed-updates.md).
A source checkout update is not a published or installed product release.

Markdown reads are fenced to `~/.claude`, the agent root and `~/Systems`, and
the fence is checked after symlinks resolve.

## Paths

| | |
|---|---|
| state | `~/.local/state/jremote` |
| credentials | `~/.local/share/jremote/credentials` |
| LaunchAgent | `~/Library/LaunchAgents/com.jremote.host.plist` |

`jstack-host where` prints them for the host you actually have. All of them move
with `--state-dir`, `JREMOTE_STATE_DIR` or `JREMOTE_CREDENTIALS_DIR`.

## Profiles

Almost everything here is generic: it reads transcripts, scans processes, drives
tmux. What it cannot work out alone is *whose* session it is looking at — which
agents exist, where their workspaces are, how this machine starts an engine.

Those questions go through `hostenv.py` and nowhere else. By default they are
answered from the filesystem: every directory under the agent root is an agent,
every directory inside one is a sub-mode. A machine with its own plumbing
supplies a `jremote_host_profile` module with a `make_profile()` function; the
host takes its answers, never its modules.

## Requirements

macOS, Python 3.11+. `tmux` and an agent CLI on `PATH` for the session features —
`jstack-host doctor` grades both, and the host runs without them.

## Development

Use feature/release branches. Installation and update tests belong in a
separate GUI macOS VM; do not run a competing Hub on an OS that already has
one installed. A complete, verified change may merge to main; publication
remains a separate explicitly requested action over tested artifacts.

```bash
.venv/bin/python3 -m pip install -e '.[dev]'
.venv/bin/python3 -m pytest tests/
```

`tests/test_jremote_isolation.py` pins that the package never imports from
whatever tree it sits inside, and never resolves a path by counting parent
directories.

MIT.
