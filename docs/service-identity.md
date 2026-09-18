# Signed service ownership

Product domain: `jstack.live`. New namespace: `live.jstack`. Product name:
**jStack Hub**. This document describes the migration target, not a claim
that existing installations already implement it.

## Inventory before migration

`jstack-host doctor --services --json` inventories filesystem LaunchAgents
and LaunchDaemons without modifying them. It records definition and executable
fingerprints, the executable's observed signature, launchd state, schedule,
and writable privileged code paths. Arguments and environment values are
never dumped. A failed observation remains unknown. This command does not
read privacy databases, reset background-item registrations, establish a
trusted baseline, or certify a machine free of malware. App-data paths are
left unobserved to avoid prompting for another application's data.

An interpreter signature attests that interpreter, not the scripts, imported
modules or child tools it executes. Filesystem inventory also does not cover
all app-owned SMAppService registrations; macOS background-task records must
be checked in the exact-artifact acceptance run.

## Installation target

- One Developer ID signed, notarized **jStack Hub.app**, including the menu,
  bundled runtime, background helpers and service definitions. Use
  SMAppService for app-owned registration. New main bundle ID:
  `live.jstack.hub`.
- Keep user work unprivileged. Register only services needed by enabled
  capabilities. Timed work uses the existing scheduling mechanisms; do not
  add a second scheduler merely to reduce a settings-row count.
- A separately privileged **jStack Network** helper performs only network
  operations. Root-executed code and all replaceable ancestors must be
  protected from ordinary-user writes. It must not run checkout scripts or
  user-installed package-manager executables as root. Authenticate its
  callers and validate requests; never expose arbitrary execution.
- The **jStack Updater** retains independent recovery of an unavailable Hub.
  Consolidated ownership must not couple rollback to the process it replaces.
- Optional local automation belongs to an explicit installed service catalog,
  not hardcoded assumptions about a particular operator's home tree. Each
  entry records purpose, owner, privilege, trigger, code identity, logs,
  dependencies, and supported stop/start behavior.
- Development servers and fixtures are opt-in project capabilities. A
  production install must not inherit a developer's unrelated startup jobs.
- Preserve existing client bundle IDs, pairing identities, credentials
  and state. A new domain alone is not a reason to invalidate them.

macOS determines its settings presentation. Acceptance measures the actual
registered owners and names; do not promise a precise row count based only
on an app name or AssociatedBundleIdentifiers metadata.

### Permission attribution evidence

Notarized Services fixture build 23, source `172136d`, ran an external
`osascript` child through SMAppService on a fresh GUI VM with Gatekeeper and
Developer ID policy enabled. A real Finder operation prompted as **jStack
Hub Services**. Denial returned `-1743`; explicit approval permitted exactly
one folder creation. `tccutil reset All live.jstack.hub.services` required
fresh named consent, and denying again prevented a second creation. Native
unregister removed the fixture registration. No privacy database was edited.

The proposed additional Apple Events entitlement was unnecessary for this
observed child-process path and was reverted after the experiment. Earlier
fixtures that only requested an application's name did not exercise consent
and are not privacy acceptance evidence.

This proves that particular responsibility chain and targeted reset only.
It does not prove every automation runtime or permission category, terminate
detached agent processes, revoke the old generic-runtime grants, or provide
a product-wide emergency switch. Those and production cutover remain #76.

### Fresh candidate installation

After placing a matching signed/notarized Hub and Services pair at their
final paths, the Hub's sealed runtime can provision a fresh standalone host:

```sh
"/Applications/jStack Hub.app/Contents/MacOS/JStackRuntime" install \
  --app "/Applications/jStack Hub.app" \
  --services "/Applications/jStack Hub Services.app" \
  --state-dir "$HOME/.local/state/jstack-hub"
```

This path refuses existing hosts, nonempty state and existing registrations;
it is not the upgrade or embedded-host migration path. The installation
journal records every registration attempt before it occurs. Pending OS
approval remains pending; an interrupted attempt is never silently repeated.
After granting approval in System Settings, rerun the same command to resume.
An observed stopped service requires the explicit Start control. Repeating a
completed installation reports the existing choices without enabling them.
Provisioning preserves credentials on retry. Completion requires observed
running services, the expected authenticated host/source identity, sessions,
an enforced authentication gate and the independent updater's source report.

For release candidates, `build_hub --release-id ... --github-repo ...
--build-number ...` records the publisher's identity and numeric bundle build.
Supply all three together. Omitting them produces a prototype identity that
does not qualify as a managed release. The distribution installer, publisher
integration and full lifecycle qualification remain tracked by #76.

Machine settings may select absolute `automation_settings` and `migration_dir`
paths inside the operator's existing private storage. Full job definitions,
environment values and original plist backups belong there. The sealed app
contains only scheduling metadata and definition digests. Migration records
the chosen location and refuses to follow a changed location during apply or
rollback; moving private configuration is a separate reviewed operation.

### Native release candidates

Independent Services maintenance is available through the Hub's bundled Python:
`-m jstack_host.services_update prepare /path/to/candidate.app`, followed by
`apply JOURNAL` or `rollback JOURNAL`. Run from a user Terminal outside Services.
The transaction keeps both sealed owners and a settings digest in private
migration storage, refuses catalog changes, and records intent before stops
or bundle replacement. Interrupted application requires explicit rollback;
OFF choices are retained. Candidate 20 (`c636642`) passed signed OFF and
enabled update/rollback cycles, a real kill after native updater unregister,
explicit recovery to the original owner, and post-recovery reboot checks in
the GUI VM. Identity and settings hashes were preserved. These are component
receipts, not automatic supervisor handoff or full release qualification.

The release publisher accepts explicit `native_services: true` for candidate
builds. It builds both public owners from the publisher's committed snapshot,
release ID, origin and build number, with no private capability catalog.
Schema 2 includes the independent `services` artifact in the signed component
set and acceptance-receipt digest; schema 1 remains the released legacy format.
Promotion, public publication and updater compatibility currently refuse
schema 2 pending Services self-update implementation and qualification. This
candidate path does not publish a new feed or migrate a released host.

### Removing user-service registrations

`jstack-host uninstall --all-services` (also available through
`python -m jstack_host.install_host uninstall --all-services`) explicitly
unregisters the Hub and Services catalogs, including the updater and optional
capabilities. Run it from an independent user Terminal. Both signatures,
sealed definitions and approval observations are checked before any stop.
The private `uninstall-journal.json` inside the configured migration directory records each
attempt before unregister; repeating the command resumes after interruption
and rechecks actual registration absence. It stores a settings digest, never
the environment values. A changed owner or settings digest requires review.

This command retains application bundles, Network, pairing and private data.
It is the user-registration portion of product removal, not a full product
uninstaller. Candidate 16 passed core registration removal, identity retention,
repeat removal and reboot on a fresh GUI VM with Gatekeeper enforced. The
subsequent candidate 17 (source `2a400e3`) passed fresh installation with
Gatekeeper enforced, four-role removal including an optional worker,
interruption after native updater unregister, journal recovery, repeat removal
and post-reboot absence. Identity and private configuration were preserved;
the journal used selected private storage without copying environment values.
The main CLI forwarding added afterward requires final-artifact qualification.

### Emergency stop

The sealed Hub CLI exposes one explicit switch: `jstack-host emergency-stop`.
It writes an active marker to the configured private state before changing
runtime state. Both the embedded runtime and native SMAppService controller
refuse subsequent service starts while that marker is active.

The transaction removes an installed Network owner through its reviewed,
administrator-approved native transaction, stops the product's dedicated tmux
server, unregisters every Hub and Services role, and resets macOS privacy
consent for the Hub, Services, Network, legacy menu and installed client bundle
identities with `tccutil`. It preserves pairing, state and application bundles.
Every step records durable progress and a repeated invocation uses the same
shutdown identity. An installed Network owner without its exact protected
transaction ID is reported before user services are changed.

Reset means the apps must request permission again if they are deliberately
started later. It does not edit the TCC database directly or affect unrelated
applications. Restoring operation is a separate, explicit recovery action; an
emergency stop never silently turns services or permissions back on.

### Automatic Services update handoff

Schema 2 fleet releases stage the signed Services artifact with Hub and the
client. After Hub replacement, the Services updater writes a durable request
and starts the Hub-owned `services-handoff` SMAppService. That controller can
stop and replace Services without terminating itself. The replacement updater
then resumes the same fleet journal, verifies the exact Services version and
finishes the release. The controller is unregistered only after fleet
confirmation is durable.

If replacement or verification is interrupted, the restarted updater reads
the handoff outcome instead of blindly applying again. Rollback runs in the
opposite ownership order: the Hub controller restores Services first; the
restored updater then restores Hub and the client. Disabled or newly denied
roles remain off throughout both directions.

### Existing host cutover

The sealed runtime's `migrate prepare --request PATH --journal-root PATH`
accepts an explicitly reviewed request containing installation settings,
the exact `host`, `menu` and `updater` job definitions, and their code
provenance. Keep that request and its journal in private storage. Preparation
checks the installed identity, endpoint, release key, matching signed owners,
and disabled/approval state. It does not provision another identity.

`migrate apply JOURNAL` retires all three originals before registering any
replacement. It verifies authenticated host identity, sessions, authentication
enforcement and the recovery process's source. `migrate rollback JOURNAL`
restores exact configuration and eligible originals. A denied replacement
holds the shared transaction; rollback never revives its legacy approval path.
An original that was OFF remains OFF. Ambiguous or externally modified state
requires review instead of an automatic overwrite.

Inventory observes explicit local `python -m` packages without importing
them. Package fingerprints detect changes, but are not a dependency seal.
Unresolved interpreter paths and protected application data remain unknown.
Unknown module provenance cannot authorize migration.

`host/tools/host_migration_accept.py` exercises released standalone upgrade,
actual reboot and exact rollback on a disposable GUI Mac. The separate
`service_migration_accept.py` retains the optional-job migration fixture.
These component journeys do not establish embedded-host, network, privacy,
or production qualification.

## Migration requirements

1. Capture installed definitions, disabled/approval states, schedules and
   live process identity. Match legacy jobs by exact label AND executable
   provenance; a name prefix alone never authorizes adopting or removing one.
2. Build and notarize the complete candidate, including nested executable
   code. Keep code immutable and state separate. Verify signatures and the
   signed release manifest before staging.
3. Prove a fresh installation and an upgrade from the existing released build
   on disposable GUI Macs. The existing installer/updater must migrate as part
   of the same release, so repairs cannot resurrect legacy jobs.
4. Prepare a durable, per-machine transaction with exact legacy targets,
   replacement registrations and rollback. Preserve disabled choices.
   Re-registering a disabled service under a new name must not turn it on.
5. Request the necessary macOS approvals through its supported UI. Privacy
   permissions cannot be copied by editing TCC databases. Keep existing
   permissions until the replacement has demonstrated the required access.
6. At approved cutover, unregister exact old jobs after their replacements
   are staged, activate replacements without overlap, and verify actual
   functions. Archive old definitions outside launchd's discovery paths.
7. After successful cutover, remove obsolete app copies and grants through
   supported mechanisms with the owner's approval. Never globally reset the
   background-task database or unrelated applications' permissions.

## Release acceptance

- Login/background UI identifies the product and its real publisher;
  no product-owned generic interpreter or shell startup identity remains.
- Every product startup registration maps to one installed signed version.
  Reinstall, update, rollback and reboot create no duplicate registrations.
- Disable remains disabled across repair/update/reboot. Start, stop and
  restart controls report observed state and explain dependencies.
- Existing sessions, pairing, network reachability, scheduled work and
  updater recovery survive the migration. No third-party service is changed.
- A failed upgrade restores the prior service set and permissions remain
  usable. A revoked approval is reported accurately rather than bypassed.
- Tampered privileged code and unauthorized helper callers are rejected.
- Uninstall accounts for every installed helper and registration, with state
  deletion a separate explicit choice.
- Compare fresh background-task records and service inventory with the
  pre-migration snapshot. Review privacy grants in System Settings.

Apple references:
[SMAppService](https://developer.apple.com/documentation/servicemanagement/smappservice),
[background-task management](https://support.apple.com/guide/deployment/manage-login-items-background-tasks-mac-depdca572563/web),
[environment constraints](https://developer.apple.com/videos/play/wwdc2023/10266/).
