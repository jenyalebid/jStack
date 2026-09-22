# Managed releases and updates

## Governing contract

The [core product contract](../product.md#core-contract), approved 2026-09-16,
is authoritative. Hub means the server and menu bar on each Mac. Parent Hub
names an independent Hub managing leaves; host is a technical machine term.
The Plugin and jRemote client are independently usable products.

There is one installed Hub per OS. Develop on feature/release branches and
test installations in separate macOS VMs. Main receives complete, verified
functionality.

A release is identified by its source: the date it was cut and the first eight
characters of its stack commit, e.g. `2026-09-21-235fc996`. There is no build
number and no version counter — a rebuild of the same source is the same
source, and the hash says so. The Claude plugin manifest and the macOS bundle
still carry a semver, because the marketplace and CFBundleVersion require one;
that number belongs to those obligations and never identifies a release.

Because a hash does not order and two releases can share a date, each manifest
also carries a `sequence` — the commit count behind its own source, never
displayed. It exists only so a hub can refuse an offer older than what it
runs, and it is compared only between releases on the same channel, where the
two counts are measured from the same root.

Each release names the line it came from. A hub follows one line, `stable` by
default, set with `jstack-host updates channel <branch>`; branch releases are
published as prereleases, so a hub that never asked cannot be handed one. The
name is read from the signed manifest rather than the release tag. Moving a
hub between lines is not a downgrade: the old count means nothing on the new
history, so the sequence guard does not apply across a switch.

Info displays version/build/source and
observed running status in a native macOS form, with the jRemote icon/version,
Open or Download, and actionable update state.

Build, merge, publish and install are separate operations. Publish only when
explicitly requested, promoting the exact tested artifacts without rebuilding.
One manifest pins compatible Hub and client builds. One menu action updates
the Hub and its hub-managed client; App Store and independently managed clients
retain their channels. The parent can update itself and its leaves. All local
entry points must resolve to the same active Hub installation.

## Current implementation status

The candidate implements unique build allocation, unified installed identity,
client distribution ownership, public release discovery and the native Info
form. Build 74 has exact-artifact receipts for the eight Mac journeys below.
The complete unattended release configuration remains
unfinished under #72. A branch or passing unit suite does not establish
production readiness.

Status: implemented candidate under real-Mac acceptance (2026-09-16).
Not promoted to the production feed. The product contract below remains the
acceptance target; implemented code and observed proof are separate facts.

## What exists today

- `release.sh` in either repository invokes the same publisher. It snapshots
  committed stack, client and shared-package revisions, builds a signed and
  notarized menu bar and client, and signs the complete artifact manifest.
  `--reuse-client` accepts an unchanged client's already signed artifact only
  after checking its source inputs and bytes. `seal` resumes completed builds.
- `fleet_updates.py` and `update_routes.py` extend the existing authenticated
  host feed with durable jobs, desired/observed inventory and parent-scoped
  update authority. Local admin requires loopback plus the internal credential;
  ordinary paired device tokens cannot queue fleet work.
- `update_supervisor.py`, `update_macos.py` and `update_plugins.py` stage,
  verify, replace and recover the host, both Mac apps and installed Claude/
  native Codex plugins. The separate launchd updater survives host replacement.
  A stable dispatcher advances its own runtime only after hub confirmation.
- The menu bar's Info window offers local Update, per-leaf Update and Update
  All, with component versions, progress and errors. An offered release for
  this Mac also exposes Update Available directly in the menu, whether the
  release changes the client app, jStack or both. The client opens the same window;
  older installations without the menu handler retain their legacy updater.
- Two disposable GUI Macs have performed signed candidate upgrades with the
  real supervisor. Testing exposed and fixed signature requirement syntax,
  launchd removal races, stale menu replacement, wrong administrative token
  selection and updater self-advancement. These observations do not substitute
  for a complete acceptance run on the final artifact set.
- `acceptance.py` writes each journey's receipt from what the run observed.
  A journey names the facts it must record; anything it did not observe leaves
  the receipt incomplete, and `gate` refuses promotion naming every journey
  that is not a genuine pass over this candidate's exact artifact set.
- `host/tools/managed_update_accept.py` is the unattended runner: one command
  drives the journeys over disposable Macs and writes those receipts. Selected
  reruns retain existing receipts; their artifact bindings are checked again.
  Prior releases are staged through the real updater, and artifact-corruption
  checks restore the original bytes even when the probe fails. New-session
  checks wait for the initial response before testing subsequent input.
  Build 74's eight Mac receipts combine retained observations and additional
  live runs. A complete unattended run has not yet passed the promotion gate.
- Production promotion rejects absent/skipped/stale receipts. No production
  acceptance receipt has been issued by the disposable fixture tools.

## Operator commands

Configure the publishing machine once with `JSTACK_RELEASE_CONFIG` pointing
to its private release configuration. Signing keys stay outside the repository.
From either repository:

```sh
bash release.sh build --notes 'Release description'
# Reuse only when client source and shared-package inputs have not changed:
bash release.sh build --reuse-client /path/to/prior/candidate --notes '...'
bash release.sh qualify /path/to/candidate --receipts /path/to/receipts
bash release.sh acceptance /path/to/candidate --receipts /path/to/receipts
bash release.sh promote /path/to/candidate --receipts /path/to/exact-artifact-receipts
# Qualify, promote and then update this hub and its eligible leaves:
bash release.sh ship /path/to/candidate --receipts /path/to/receipts --deploy
```

`qualify` runs the acceptance runner named by the release configuration's
`acceptance` command line. Its exit status is a hint; the receipts it wrote are
the evidence, and `acceptance` prints what they prove today. `ship` is the one
release action: it qualifies, promotes through the same gate, and — with
`--deploy` — asks this hub to update itself and every leaf it can reach, then
waits for the hub's own confirmation of each. A machine that is offline or has
no update supervisor is reported unreached, never counted as deployed.

Build never installs locally or publishes. Promotion is an explicit second
step; it validates all nine receipts and atomically changes the fleet feed.
The candidate includes `mobile.status=not_distributed`: an iOS upload or silent
phone update is **not** implemented by this command.

Existing installations need `jstack-update-bootstrap` once, using their
existing host/app/menu paths and the packaged public trust key. The installer
calls this when those components exist. An old leaf without that supervisor
cannot be updated remotely until bootstrap has run on that leaf.

### Remaining acceptance/integration work

- Run the acceptance runner to completion against the fixtures and retain all
  nine final-artifact receipts: fresh_install, upgrade, fleet, offline_catchup,
  session_survival, interruption, rollback, revocation, off_network. Unit passes
  are not these receipts, and the runner existing is not a run.
- Build 74 passed fresh installation, upgrade, fleet, offline catch-up,
  session survival, interruption/reboot, rollback/artifact refusal and queued
  authority revocation. Fleet evidence covers one parent and two leaves,
  with one leaf offline because the qualification host permits only two
  concurrent macOS guests. The native Info Update All control was exercised.
  Fresh-install evidence retains the original installation observations and
  a subsequent successful Codex session after provider authentication setup.
- The revocation fixture uses the shipped device store to revoke its credential
  and verifies cancellation plus authenticated-request rejection. It does not
  claim mesh enrollment or the native device-removal UI: the HTTP-relay lab is
  not a mesh-owning Hub.
- The off_network journey runs unattended: it blackholes a leaf's LAN route to
  the hub, proves the LAN path is dead and the mesh path carries health, the
  release notice and a real session. No phone, no human.
- Mobile distribution and release notification in the phone UI remain open.
  Discovery currently reconciles by polling, not a release stream event.
- Staged Python dependencies are resolved by pip, not yet a locked, bundled
  offline dependency set. The source archive and Mac apps are exact artifacts;
  do not claim fully hermetic installation.

Remaining qualification is tracked in [#72](https://github.com/jenyalebid/jStack/issues/72).

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

## Release contract

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
- A Mac with no LAN route to the hub discovers the release and completes an
  authenticated session journey against the updated hub and leaf.

Use disposable GUI VMs and the designated test phone. NAT hairpin and local
HTTP contract tests remain useful evidence, but cannot substitute for the
off_network journey. Missing, skipped or stale required receipts prevent
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

Implementation exists for the Mac update path. The complete acceptance and
platform-distribution contract above is not yet shipped; production promotion
must remain closed until its required evidence exists.
