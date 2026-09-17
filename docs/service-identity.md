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
