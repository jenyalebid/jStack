# Managed releases and updates

Status: product direction agreed on 2026-09-16; implementation design proposed.
The product documents describe the intended contract. This document separates
that contract from the implementation and evidence available today.

## What exists today

- `5cd12c4` adds the running host's source stamp and doctor comparisons for
  checkout, plugin cache, running host and installed app. These are local
  observations, not a fleet inventory or an updater.
- `host/jstack_host/releases.py` serves the authenticated Mac client feed.
  The client repository's `release-mac.sh` builds, signs, notarizes and
  publishes the app. Its managed-access gate does not cover a complete
  installation or cellular journey.
- The client's `MacUpdater.swift` checks registered hosts at launch and every
  six hours, stages a verified app, and offers a relaunch. Its helper checks
  the archive again before replacement. Update ownership currently lives in
  the client app, not the menu bar.
- `install.sh` updates the stack and installs the client. The menu bar is
  compiled locally by `host/menubar/install.sh`; it is not yet a separately
  published, signed update artifact.
- Managed enrollment supplies parent identity and credentials. It provides
  a connection to extend, not authorization for arbitrary remote commands.

## Product decisions

- The home hub owns release selection and update management for its fleet.
- Every Mac has one update surface in the jStack menu bar for the host,
  plugin, menu bar app and client app. The client's update entry leads there.
- The hub can update itself, one managed Mac, or all eligible managed Macs.
  A managed Mac can also apply the release its hub offers with a local tap.
- Every host and client learns of releases while connected and checks again
  on launch and reconnect. Unreachable means unknown, never up to date.
- Changes in either repository have one release workflow. Publishing a
  release and deploying it are separate recorded steps.
- General SSH administration remains a possible later capability. Updating
  does not depend on adding an unrestricted remote shell first.

## Proposed workflow

One release action, reachable from either repository, submits exact committed
source revisions to the build/publishing machine. That machine produces one
release manifest covering the stack, menu bar and client platform builds.
No release is assembled from a developer's moving working directory.

The manifest has a release ID, source revisions, component versions and
digests, platform requirements, compatibility constraints, release notes and
test receipts. Unchanged components can retain their existing verified
artifacts; one release action need not rebuild unrelated binaries.

Build and test a candidate privately. Sign the manifest and Mac artifacts;
reuse the client's signing and notarization pipeline and package the menu bar
through it. Validate the complete candidate, then atomically promote the
manifest in the existing host feed. An incomplete candidate is not offered.
Keep immutable artifacts and a previous compatible release for recovery.

The home hub selects the fleet's desired release from that feed. Paired
clients discover their designated publisher through the hub, instead of
electing whichever registered host advertises the largest build number.
Release events use the existing streaming connection; periodic checks and
reconnect reconciliation recover missed events.

## Menu bar and fleet inventory

The menu bar shows the available release and one Update action for this Mac.
On the hub it also shows each managed Mac, with Update and Update All actions.
The update control stays usable when the client app is closed.

For each machine, record desired release, installed component versions,
actually running component versions, last contact, and the latest update
job/result. Show checking, available, downloading, applying, verifying,
current, pending/offline, failed or rolled back. Surface the error and retry
action on the affected row. App and host versions are separate facts.

Extend today's source stamp into this report, including release identity for
packaged installations. Plugin cache version and running sessions' loaded
versions are separate: updating a cache does not hot-reload existing sessions.
Keep current sessions running and show when new sessions will use the update.

## Remote execution and recovery

A small local update supervisor survives host and menu bar restarts. It
maintains an authenticated outbound connection to the parent hub and accepts
durable update jobs. The hub authorizes a target release; the leaf downloads
the named artifacts and executes the same local updater as its menu button.

Jobs identify the target machine, release and unique request. Repeated
delivery is idempotent. Job authority is scoped to enrolled machines and
update operations; a normal paired phone's credential is not administrative
authority. Revocation or detach cancels queued authority. Verify signatures
against pinned release trust, not a signing identity supplied by the download.

Stage and verify everything before changing the running installation. Keep
release directories separate from source checkouts and persistent state.
Serialize updates per machine. Restart only affected components, retain
session processes, credentials and the mesh, and resume interrupted jobs
after reboot. Do not replace the tunnel while using it as the only recovery
path; any tunnel migration needs its own tested recovery sequence.

The supervisor verifies the running release and a real authenticated product
request after application. The hub independently confirms reconnection and
the reported release. A download, installer exit or health response alone
does not complete a job. If verification fails, recover the previous
compatible release and report failure/rollback. State migrations must declare
rollback support; never promise binary rollback across incompatible data.

Mixed versions are expected during rollout. Check compatibility before each
job; update the hub first only when it continues supporting existing leaves
and clients. Older leaves that lack the supervisor need a documented one-time
bootstrap through the existing installer/adoption path. Do not claim they can
already be remotely upgraded.

## Phone and tablet distribution

The same release workflow prepares the iOS/iPadOS build and records its
distribution status. Devices get an update notice and the action for their
supported distribution channel. Mac bundle replacement is not an iOS update
mechanism. Reuse the existing device/TestFlight workflow; distinguish built,
uploaded, available and installed. Do not promise silent phone installation.

## Acceptance and ongoing management

Before promotion, the candidate needs receipts tied to its exact artifacts:

- Fresh Mac installation and upgrade from the previous release, with host,
  plugin, menu bar and client identities observed after restart.
- A hub and two managed Macs: local Update, remote Update and Update All,
  including a leaf offline at request time and catching up after reconnect.
- An active session survives updating; the client reconnects and can send
  input and receive new output. Validate the app and menu bar relaunch too.
- Interrupted download, duplicate job, supervisor/host crash and reboot
  resume correctly. Bad signatures and incompatible releases are refused.
- A failed candidate rolls back without losing pairing or remote access.
  Detached/revoked machines cannot execute queued update jobs.
- A real phone over cellular discovers the release and completes an
  authenticated session journey against the updated hub and leaf.

Use disposable GUI VMs and the designated test phone. NAT hairpin and local
HTTP contract tests remain useful evidence, but cannot substitute for the
physical cellular journey. Missing, skipped or stale required receipts prevent
promotion; a test's existence does not satisfy the gate.

The hub continually reconciles desired versus observed versions and job
progress. Last contact and evidence age are visible. Interrupted updates
retry within bounded policy; persistent failure stays attached to its job
with diagnostics. Routine recoverable failures do not become requests for a
human decision. Escalation names the exhausted recovery and missing capability.

## Implementation order

1. Release manifest and fleet version inventory, extending the existing feed
   and source stamps. This makes every deployment discrepancy observable.
2. One local updater and menu bar action, with signed artifacts and recovery.
3. Parent-authorized remote jobs using that same updater, offline catch-up
   and reporting. Prove it on two disposable managed Macs.
4. One release workflow from either repository, platform distribution and
   candidate promotion enforced by the product acceptance receipts above.

This is a design, not an assertion that these four steps have shipped.
