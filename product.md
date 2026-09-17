# jStack

## Core contract

Approved 2026-09-16. This section governs terminology, installation and
releases. Historical plans do not override it. Implementation and deployment
evidence are recorded separately in [managed updates](docs/managed-updates.md).

| Term | Meaning |
| --- | --- |
| jStack Plugin | Commands, skills and agent tooling usable independently in Claude Code or Codex, without Hub or jRemote. |
| jStack Hub | The server and its menu bar, installed once per macOS installation. A leaf runs this same software. |
| Parent Hub | An independent Hub managing leaves. An independent Hub without leaves is simply a Hub. |
| Leaf | A Mac whose Hub is managed by, and fully accessible through, its parent Hub. |
| Host | A technical machine/server term, not another product or the name of the root role. |
| jRemote | The independently installed client app, connecting to local or remote Hubs. |

- One installed Hub per operating-system installation, with one active version.
  Menu, service and CLI must resolve to that installation. Updates, repairs and
  role changes replace it; none creates another Hub. Recovery artifacts stay
  inactive and must not register another app or service.
- Develop on feature or release branches. Test installations run in separate
  macOS VMs. Main accepts complete, verified functionality only.
- Every build gets a new build number and records its exact source commit.
  A rebuild of the same commit is a new build. Display product version, build
  and source identity; never ship different bytes under one build identity.
- Publish only when explicitly requested. Promote the exact tested artifacts,
  without rebuilding during publication. A release manifest pins compatible
  Hub and jRemote builds. A push or merge does not publish or deploy a release.
- TestFlight and the Hub-managed Mac app are one jRemote release: both use the
  same committed client source and build number, and neither is complete until
  both channels are published.
- One menu action updates the existing Hub and its eligible client. A parent
  can update itself, one leaf or all eligible leaves. Verify the running build
  after restart; offline, failed and rolled-back targets remain explicit.
- jRemote has its own version and distribution channel. A local Hub is
  optional. The Hub observes a local client's version but updates it only
  when it belongs to hub-managed distribution. App Store and other independently
  managed installations retain their own update paths.
- Info uses a native macOS form: Hub version/build/source and running status;
  jRemote icon, version and status; Open when installed, Download when absent;
  update availability and progress. No published release must not make a
  healthy running Hub appear unknown.

## The problem

A Mac running coding agents is only useful while you are sitting at it. The
sessions live in terminals on that desk — you cannot see what one said,
answer it, or start new work from anywhere else. Owning a second Mac does not
help. It is a second desk with the same problem.

## The offering

jStack turns a Mac into a hub. One script installs it, and from then on every
session that machine runs is visible and drivable from any device you own —
on your own network by default, off-network when you choose it, and other
Macs can attach to a hub to be reachable through the same door. The hub owns
everything real and serves it through one interface every app uses alike: the
app we ship is one client, not the product.

## The functionality

### Sessions
**Live sessions**
- Watch any running session work, from any device, as it happens.
- Type into it as if seated at the Mac — interrupting included.
- Leaving does not end a session. It ends because someone ended it, never
  because nobody was watching.

**Starting work**
- Open a new session from anywhere: pick the agent, the provider it runs on,
  and the model.
- A session can be split into a copy, handed off to continue fresh, or closed
  for good.

**History**
- Every past session kept and searchable, per agent.
- Read back clean: the conversation, not the machinery around it.

**The vocabulary**
- A set of commands every session understands, the same on every machine.
- Prepare on an area, land finished work, account for what a task produced,
  recall a day, put the work itself on screen.
- Hand a unit of work to another agent and keep your own conversation.

### Reach
**Modes**
- Local: your own network only. What a fresh install is; nothing to set up.
- Open: reachable from anywhere on its own. A deliberate step, guided rather
  than hidden.
- Managed: attached to a parent hub. Every device paired with the parent
  reaches this machine with nothing added on either side.
- The mode shows wherever the hub appears, and changing it is an action in
  the interface.

**Devices**
- A device pairs by scanning a code shown at the Mac — single use, short
  lived.
- Each device carries its own key. Revoking a lost phone cuts off that phone
  only, and immediately.
- The home hub's menu bar owns the device list: name, rename and remove.
- A remote app can disconnect only itself; a managed Mac does not administer devices.

**Managed Macs**
- Add a Mac to the home hub by running one generated file on that Mac.
- Devices paired with the hub automatically reach its managed Macs without pairing again.
- The hub controls whether each managed Mac sees the home instance and the other managed Macs. Both are allowed by default.
- Each instance's name and icon remain editable; access and connection settings stay controlled by the hub.

### Working unattended
**Scheduling**
- One-time and recurring runs the hub starts on its own, whether or not
  anyone is watching.
- A scheduled run is a real session, opened with its instructions as the
  first message.
- Runs missed while the machine slept catch up. Runs that hang are ended,
  not left holding a place.
- A job a person set can be locked so nothing automated may move it.

**Memory**
- Every session writes what it did into one running record for the machine.
- A new session starts already told what its predecessors did, instead of
  starting blind.
- Answerable after the fact: what happened that day, on that subject, by
  that agent.
- A session can be opened on a subject, inheriting everything every agent
  has done on it.

**Messaging**
- Addressed messages between agents: news, or a task whose answer returns to
  whoever asked.
- A task cannot be quietly ignored — the receiver cannot finish while one is
  waiting.
- Every exchange leaves a record of what came of it.

**Notifications**
- A ping on your phone when a session finishes or waits on you.
- Quiet when you are already looking at it. Silenceable per agent.

### General
**The host API**
- One versioned interface, the same for every app that connects.
- The app we ship holds no special access — a client someone else writes is
  a supported case.
- A client asks a hub what it can do, and draws only what is really there.

**Files**
- A folder per agent that you and the agent both read and write, from any
  device.
- What you put there is yours: an agent may tidy its own mess, never your
  things.

**Usage**
- Two numbers, kept apart: how close the account is to its limit, and what
  today actually cost.

**The day feed**
- Everything the machine did today — sessions, commits, scheduled runs,
  messages — one stream, in the order it happened.

### On the Mac
**The menu bar**
- The hub run from the machine it lives on: start, stop, start at login.
- Pairing lives here: the code to scan, the device list, removal.
- Attaching this Mac to a parent — or taking another Mac under this one —
  starts here too.
- Shows what is true right now: the mode, the sessions running, the machines
  attached.
- One status dot replaces the menu-bar session counter: red (needs attention),
  then orange (finished unread), then green (working), then neutral (idle).
  The highest-priority session on this Mac wins.
- Devices and Managed Macs are single-line counts. Info opens a status and
  version window; update controls live there. When this Mac has an offered
  client app or jStack update, Update Available also appears in the main menu.

**Install**
- One command on a clean machine ends with a working session on screen.
- Repeating installation safely repairs the installed components.
- The install checks itself and ends with a verdict, not an assumption.
- Removal takes only what install wrote; a deeper option takes the stored data too.

**Releases**
- Changes to either jStack or its client app have one release action that prepares a compatible release of the stack and its apps.
- The parent Hub manages which published release is offered to its leaves and hub-managed clients.
- A release becomes available only after its required product journeys pass on the release being offered.
- Every connected host and client learns when an update is available, including after reconnecting.

**Updates**
- The menu bar updates the single jStack Hub installation and any hub-managed jRemote client with one action.
- From the home hub, update one managed Mac or all eligible managed Macs remotely.
- See what each machine has installed, what it is running, the available update, and its last verified contact.
- Follow each update through completion or recovery; an offline machine stays pending and a failed update is never reported as current.
