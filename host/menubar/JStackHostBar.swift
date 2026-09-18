//
//  JStackHostBar.swift
//  The host's menu bar app — the whole thing, in one file.
//
//  The host is a terminal program on purpose: installed by a script you can
//  read, run by a user LaunchAgent, answering on a port with no window and no
//  Dock tile. That is the trust argument — a program that watches your terminal
//  sessions should be one you can audit before you run it — but it leaves the
//  machine with no way to answer "is it up, and what is it doing" short of
//  curling a port. This is that answer, in the one place a background program
//  is allowed to be seen.
//
//  It belongs to the host and not to any client app. A status item lives and
//  dies with the process that created it, so putting one in a client means the
//  indicator disappears the moment you quit the client — while the host it was
//  indicating is still running. This app runs under its own LaunchAgent beside
//  the host's, which is the only arrangement where the icon means what it says.
//
//  One file, compiled on your machine by `install.sh`. Nothing is downloaded,
//  so there is no signature to trust and no notarization to check: the binary
//  in your menu bar was built from the source next to it, by you.
//
//  Unsandboxed — a locally built app, not a Store one — so it may do the thing
//  a sandboxed app cannot: operate the LaunchAgent. Start, stop and restart are
//  real here. That is why this is the host's UI and not the client's.
//

import AppKit
import CoreImage
import Foundation
import Network
import ServiceManagement
import SwiftUI

// MARK: - Where the host is

/// Everything about the installed host, read off the LaunchAgent that installed
/// it rather than guessed.
///
/// The plist is the one record of what the host was *installed to be*. A host
/// installed with `--state-dir` keeps its token somewhere this app would never
/// find by resolving defaults, and reading defaults instead would report on a
/// different, empty host — the same trap `jstack-host` avoids by adopting the
/// installed environment before every read command.
enum HostAgent {
    static var appOwned: Bool { Bundle.main.bundleIdentifier == "live.jstack.hub" }

    static func serviceSettings() -> [String: Any] {
        guard appOwned else { return [:] }
        let path = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".local/state/jremote/service-settings.json")
        guard let data = try? Data(contentsOf: path),
              let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              value["schema"] as? Int == 1 else { return [:] }
        return value
    }
    /// The LaunchAgent that owns the host's lifecycle.
    ///
    /// `com.jremote.host` is what `jstack-host install` writes, and on a
    /// machine the installer set up that is the answer. It is not the only
    /// one: a host embedded in a larger application is started and stopped by
    /// *that* application's agent, and a menu hardcoded to this label decides
    /// there is nothing installed, hides Restart and Stop, and leaves the one
    /// machine whose hub you would actually want to operate with a menu that
    /// only reports. `JREMOTE_AGENT_LABEL` names the real one.
    ///
    /// Read from this process's own environment and never through
    /// `environment()` below — that resolves by *reading this label's plist*,
    /// so sourcing the label from it would be circular.
    static let label: String = {
        if appOwned {
            if let capability = serviceSettings()["host_capability"] as? String {
                return "live.jstack.automation." + capability
            }
            return "live.jstack.hub.host"
        }
        let env = ProcessInfo.processInfo.environment["JREMOTE_AGENT_LABEL"] ?? ""
        return env.isEmpty ? "com.jremote.host" : env
    }()
    static let defaultPort = 9090

    static var serviceRole: String { serviceSettings()["host_capability"] as? String ?? "host" }
    static var serviceController: URL {
        Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/JStackHub")
    }

    static var plistURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/\(label).plist")
    }

    static var isInstalled: Bool {
        if appOwned { return !serviceSettings().isEmpty }
        return FileManager.default.fileExists(atPath: plistURL.path)
    }

    private static func job() -> [String: Any]? {
        guard let data = try? Data(contentsOf: plistURL),
              let plist = try? PropertyListSerialization.propertyList(
                  from: data, options: [], format: nil) as? [String: Any]
        else { return nil }
        return plist
    }

    /// The record an embedded host leaves behind — `embed.declare()` on the
    /// Python side, written by the server that mounts the host as it starts.
    ///
    /// A host embedded in another application has no LaunchAgent of its own, by
    /// design, so `job()` above answers nothing about it and every resolver
    /// below fell through to the package defaults. The defaults name
    /// `~/.local/state/jremote`, which on such a machine is a directory the
    /// live host has never read: this bar presented a credential minted into it
    /// and the hub answered `wrong secret for host-internal`, which the menu
    /// then drew, accurately and uselessly, as "the token on disk was refused
    /// by the hub".
    ///
    /// Read fresh, never cached: the marker is rewritten every time the
    /// embedding server starts, and a bar that cached it across a server that
    /// moved its state dir would go on reporting the old one until someone
    /// restarted the menu.
    private static func marker() -> [String: Any] {
        let env = ProcessInfo.processInfo.environment["JREMOTE_EMBED_MARKER"] ?? ""
        let path = env.isEmpty
            ? FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent(".local/state/jremote/embedded.json")
            : URL(fileURLWithPath: (env as NSString).expandingTildeInPath)
        guard let data = try? Data(contentsOf: path),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return [:] }
        return json
    }

    /// One string off the marker, or nil where it says nothing. Empty is nil:
    /// a key present and blank is a marker that failed to record an answer,
    /// and taking it would resolve every path against `/`.
    private static func marked(_ key: String) -> String? {
        guard let value = marker()[key] as? String, !value.isEmpty else { return nil }
        return value
    }

    /// The port to look on: this app's own `--port` if it was given one,
    /// otherwise the port the installed agent serves on — taken from the argv
    /// launchd execs, which is the same string the host is running with.
    static func port() -> Int {
        if let port = serviceSettings()["port"] as? Int, (1...65535).contains(port) { return port }
        let mine = ProcessInfo.processInfo.arguments
        if let i = mine.firstIndex(of: "--port"), i + 1 < mine.count,
           let p = Int(mine[i + 1]) { return p }
        if let args = job()?["ProgramArguments"] as? [String],
           let i = args.firstIndex(of: "--port"),
           i + 1 < args.count,
           let p = Int(args[i + 1]) { return p }
        // The embedded host's own answer, which is the only record of it: the
        // port is the embedding server's, and nothing in this app's argv or in
        // that server's plist has to mention it. `defaultPort` being right here
        // today is luck, not a reading.
        if let p = marker()["port"] as? Int, (1...65535).contains(p) { return p }
        return defaultPort
    }

    /// The address the host was installed to bind, or nil where nothing on
    /// disk says.
    ///
    /// Nil is a real answer and not a default: an embedded host has no agent
    /// of its own to read, and guessing `0.0.0.0` there would put "reachable
    /// from your LAN" under a machine's name on the evidence of nothing.
    static func bind() -> String? {
        if appOwned { return serviceSettings()["bind"] as? String }
        let mine = ProcessInfo.processInfo.arguments
        if let i = mine.firstIndex(of: "--bind"), i + 1 < mine.count {
            return mine[i + 1]
        }
        guard let args = job()?["ProgramArguments"] as? [String],
              let i = args.firstIndex(of: "--bind"), i + 1 < args.count
        else { return nil }
        return args[i + 1]
    }

    /// The state dir the host is using. From the agent when there is one;
    /// otherwise the package's own default — which is the right answer for a
    /// host started with `jstack-host serve`, a documented way to run one and
    /// a case with no plist to read anything off.
    static func stateDir() -> URL {
        if let state = environment()["JREMOTE_STATE_DIR"], !state.isEmpty {
            return URL(fileURLWithPath: (state as NSString).expandingTildeInPath)
        }
        if let marked = marked("state_dir") {
            return URL(fileURLWithPath: (marked as NSString).expandingTildeInPath)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".local/state/jremote")
    }

    /// The variables that say which host this is — the ones a command has to
    /// take on to answer about the installed host rather than about whatever a
    /// shell implied. Mirrors `install_host.MESH_VARS` and the `JREMOTE_`
    /// prefix beside it, and for the same reason: `WG_PEER_DIR` carries no
    /// prefix and is the variable that decides whether this Mac owns a mesh at
    /// all, so a filter that takes only the prefix drops the one fact a hub
    /// cannot be read without (#42).
    static func carries(_ key: String) -> Bool {
        key.hasPrefix("JREMOTE_") || key == "WG_PEER_DIR" || key == "WG_ENDPOINT"
    }

    /// The overrides the host runs under. `install_host` always pins
    /// `JREMOTE_STATE_DIR` in the plist, even when nobody passed `--state-dir`,
    /// so this is never empty for an installed host.
    ///
    /// An explicit export in this process's own environment wins over the
    /// plist — the same precedence `adopt_installed_environment` applies on the
    /// Python side, where the agent's settings go *beneath* whatever the caller
    /// set on purpose. Under launchd there is nothing exported, so the plist is
    /// what answers; run by hand against a second host, the export is.
    static func environment() -> [String: String] {
        var out: [String: String] = [:]
        if let environment = serviceSettings()["environment"] as? [String: String] {
            out = environment.filter { carries($0.key) }
        }
        if let env = job()?["EnvironmentVariables"] as? [String: Any] {
            for (k, v) in env where carries(k) {
                out[k] = String(describing: v)
            }
        }
        for (k, v) in ProcessInfo.processInfo.environment where carries(k) {
            out[k] = v
        }
        return out
    }

    /// The bearer token file, resolved the way the host resolves it:
    /// `JREMOTE_TOKEN_PATH` wins outright, then the embedded host's marker,
    /// otherwise `api-token` inside the state dir.
    static func tokenPath() -> URL {
        if let explicit = environment()["JREMOTE_TOKEN_PATH"], !explicit.isEmpty {
            return URL(fileURLWithPath: (explicit as NSString).expandingTildeInPath)
        }
        if let marked = marked("token_path") {
            return URL(fileURLWithPath: (marked as NSString).expandingTildeInPath)
        }
        return stateDir().appendingPathComponent("api-token")
    }

    /// The host's own local credential — `devices.internal_token()`, kept in
    /// plaintext beside the table that validates it.
    ///
    /// The fallback and never the first answer: `tokenPath()` is what the host
    /// says it expects, and this is what it mints for callers on its own
    /// machine when nobody handed it a token file. On an embedded host that is
    /// the usual case — the marker records a `token_path` under `Credentials/`
    /// that the migration to per-device rows left behind, and the file the hub
    /// actually accepts is this one. Read only, never minted here: minting is
    /// a read-compare-write across a sqlite row and a file, serialized by a
    /// flock this process has no business taking, and the losers of that race
    /// corrupt the credential for everything on the machine.
    static func internalTokenPath() -> URL {
        stateDir().appendingPathComponent("internal-token")
    }

    /// Update authority is deliberately narrower than ordinary host reads.
    /// Never substitute a still-valid legacy or paired-device credential.
    static func updaterToken() -> String? {
        guard let raw = try? String(contentsOf: internalTokenPath(), encoding: .utf8) else { return nil }
        let token = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        return token.hasPrefix("jr1.host-internal.") ? token : nil
    }

    /// Tokens this host has answered 401 to, by value.
    ///
    /// **A token file existing says nothing about the credential being live.**
    /// That is the whole reason this exists. `tokenPath()` on an embedded host
    /// names `Credentials/jremote-api-token`, which holds the pre-per-device
    /// shared bearer; the `legacy` row behind it was revoked on 2026-09-03 and
    /// the file was left on disk. A reader that picks the first candidate that
    /// *exists* therefore picks a revoked credential, for ever, on the one Mac
    /// that runs the hub — the menu bar has read "No Access · refused this
    /// Mac's token" since that revocation while the host beside it was up and
    /// holding a perfectly good credential one path over.
    ///
    /// Only the host can say which token is live, and it says it by answering.
    /// So a 401 retires the exact string that earned it and the next poll — ten
    /// seconds later — advances to the next candidate. Keyed on the value, not
    /// the path, so rewriting a file with a good token clears it with no
    /// restart: the new string was never refused.
    private static let refusedLock = NSLock()
    private static var refusedTokens: Set<String> = []

    /// Called with whatever was sent on any request the host answered 401 or
    /// 403 to. Idempotent, and safe from URLSession's callback queues.
    static func refuse(_ token: String?) {
        guard let token, !token.isEmpty else { return }
        refusedLock.lock()
        defer { refusedLock.unlock() }
        refusedTokens.insert(token)
    }

    private static func refused(_ token: String) -> Bool {
        refusedLock.lock()
        defer { refusedLock.unlock() }
        return refusedTokens.contains(token)
    }

    /// Read fresh every poll, never cached. A host provisioned after this app
    /// launched — the ordinary first-install order — would otherwise stay
    /// tokenless in the menu until someone thought to restart the menu bar.
    ///
    /// Candidates in order, skipping any the host has already refused. When
    /// every candidate has been refused we hand back the last one anyway rather
    /// than nil: nil reads as "this Mac has no token", and "the hub refused the
    /// token this Mac has" is a different fault with a different fix. The menu
    /// has separate words for each and picking the wrong one sends the reader
    /// after the wrong problem.
    static func token() -> String? {
        var last: String? = nil
        for path in [tokenPath(), internalTokenPath()] {
            guard let raw = try? String(contentsOf: path, encoding: .utf8)
            else { continue }
            let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            if trimmed.isEmpty { continue }
            if !refused(trimmed) { return trimmed }
            last = trimmed
        }
        return last
    }

    static func logDirectory() -> URL {
        stateDir().appendingPathComponent("logs")
    }

    /// The program launchd execs for the host. Its *directory* is the useful
    /// part: an embedded host runs out of some environment's `bin`, and the
    /// host's own tooling is installed into that same `bin`.
    static func programPath() -> String? {
        (job()?["ProgramArguments"] as? [String])?.first
    }
}

// MARK: - What the host says

struct Health: Decodable {
    var service: String?
    var profile: String?
    var provisioned: Bool?

    /// Is this *our* host, or merely something else holding the port?
    ///
    /// Worth asking: the port is a default, and another program answering it
    /// with JSON that happens to lack these keys would otherwise be reported as
    /// a healthy host. `service` is the marker the host stamps deliberately.
    var isOurHost: Bool { service == "jremote-host" }
}

/// One row of the Active section. Every field optional: this app renders a
/// menu, and a menu that fails to draw because the host grew a key is worse
/// than one that draws a row with a blank subtitle.
struct Session: Decodable {
    var sessionId: String?
    var agentName: String?
    var emoji: String?
    var subMode: String?
    var windowName: String?
    var live: Bool?
    var managed: Bool?
    var onMac: Bool?
    /// Alive — a process is holding this session — whether or not it is
    /// producing anything this second. `live` is the narrower claim.
    var running: Bool?
    var turn: String?
    var attention: String?
    var unread: Bool?

    // Match jRemote's per-session semantics: a new working turn supersedes
    // that same session's old unread reply. Across sessions, unread wins.
    var signal: SessionSignal {
        if !(attention ?? "").isEmpty { return .attention }
        let working = (turn ?? "").isEmpty ? live == true : turn == "working"
        if working { return .working }
        if unread == true { return .unread }
        return .idle
    }

    /// What to call it, in the order the answer is most specific: the window's
    /// own title, then the agent it belongs to, then nothing anyone can act on.
    var title: String {
        let name = (agentName ?? "").trimmingCharacters(in: .whitespaces)
        let mode = (subMode ?? "").trimmingCharacters(in: .whitespaces)
        let window = (windowName ?? "").trimmingCharacters(in: .whitespaces)
        let who = mode.isEmpty ? name : "\(name) · \(mode)"
        if !who.trimmingCharacters(in: .init(charactersIn: " ·")).isEmpty { return who }
        if !window.isEmpty { return window }
        return sessionId ?? "session"
    }

    /// `live` is producing output right now; `onMac` is a window showing it.
    /// A session can be either without the other, and the dot says which.
    var mark: String {
        if live == true { return "●" }
        if onMac == true || managed == true { return "○" }
        return " "
    }
}

struct ActiveSessions: Decodable { var sessions: [Session]? }

enum SessionSignal: Int {
    case idle, working, unread, attention

    static func collective(_ sessions: [Session]) -> SessionSignal {
        sessions.map(\.signal).max(by: { $0.rawValue < $1.rawValue }) ?? .idle
    }
    var color: NSColor {
        switch self {
        case .idle: .secondaryLabelColor
        case .working: .systemGreen
        case .unread: .systemOrange
        case .attention: .systemRed
        }
    }
    var label: String {
        switch self {
        case .idle: "Idle"
        case .working: "Working"
        case .unread: "Done, unread"
        case .attention: "Needs attention"
        }
    }
    var image: NSImage {
        NSImage(size: NSSize(width: 14, height: 14), flipped: false) { rect in
            self.color.setFill()
            NSBezierPath(ovalIn: rect.insetBy(dx: 3, dy: 3)).fill()
            return true
        }
    }
}

/// One row of `GET /devices` — the host's roster of paired devices. The token
/// itself is never here: the host keeps only its hash, so a row is an identity
/// and a status, not a credential. `revoked` and `current` are stamped onto the
/// raw row by the host; the timestamps are unix seconds, absent on an older
/// host and never a reason to fail the decode.
struct Device: Decodable {
    var id: String
    var name: String
    var createdAt: Int?
    var lastSeenAt: Int?
    var revoked: Bool
    /// True for the row whose token this menu authenticated with. Removing it
    /// cuts this Mac's own access to the hub, so the confirmation says so.
    var current: Bool

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        name = try c.decode(String.self, forKey: .name)
        createdAt = try c.decodeIfPresent(Int.self, forKey: .createdAt)
        lastSeenAt = try c.decodeIfPresent(Int.self, forKey: .lastSeenAt)
        revoked = try c.decodeIfPresent(Bool.self, forKey: .revoked) == true
            || c.decodeIfPresent(Int.self, forKey: .revokedAt) != nil
        current = try c.decodeIfPresent(Bool.self, forKey: .current) ?? false
    }

    enum CodingKeys: String, CodingKey {
        case id, name, revoked, current, createdAt, lastSeenAt, revokedAt
    }
}

struct DeviceList: Decodable { var devices: [Device] }

enum DeviceMenu {
    /// The menu is current access, not the host's retained audit history.
    static func active(_ devices: [Device], removed: Set<String> = []) -> [Device] {
        devices.filter { !$0.revoked && $0.id != "host-internal" && !removed.contains($0.id) }
            .sorted { $0.name < $1.name }
    }

    static func removalSucceeded(status: Int, data: Data?) -> Bool {
        if status == 200 { return true }
        guard status == 404, let data,
              let body = try? JSONDecoder().decode([String: String].self, from: data)
        else { return false }
        return body["detail"] == "unknown or already revoked device"
    }
}

/// `/host` — the route that proves which machine this is. Behind the token by
/// design, and that is the point: a host answering on loopback that rejects
/// our token is not our host, whatever it would have claimed.
struct HostIdentity: Decodable {
    var hostId: String?
    var name: String?
    var profile: String?
    var features: [String: Bool]?
    var source: UpdateSource?

    /// local / open / managed — the host's own verdict on how a device off
    /// this network reaches it, computed by `jstack_host.mode`. Nil where an
    /// older host predates the field; the headline falls back to the coarser
    /// hub/leaf read below rather than drawing nothing.
    var mode: HostMode?

    /// Hub or leaf, taken from the host's own answer rather than inferred.
    ///
    /// `tunnel_pairing` is true only where `wg0.conf` is — and that file *is*
    /// the mesh, the peer list the interface honours. A hub holds it; a leaf
    /// dialled out to one and has no peers of its own to mint. Nil where the
    /// host did not say, which is not the same as leaf and must not be drawn
    /// as one. Superseded by `mode` on a current host; kept for the fallback.
    var isHub: Bool? { features?["tunnel_pairing"] }
    var canManageDevices: Bool {
        features?["device_management"] == true && mode?.isManaged != true
    }
}

/// The `mode` block `/host` carries: the word, the one-line explanation, and
/// whether the mode is live right now (a managed host's tunnel can be down
/// while the attachment itself stands).
struct HostMode: Decodable {
    var mode: String?
    var note: String?
    var live: Bool?
    /// The hub this machine dialled out to, on the managed answers and empty
    /// everywhere else. A URL and nothing more — the record it is read from
    /// (`parent.json`) also holds this machine's credential on that hub, and
    /// `/host` is served to every device that can reach it.
    var parent: String?

    var isManaged: Bool { mode == "managed" }

    /// "studio.local", from "http://studio.local:9090" — the machine, without
    /// the scheme and port nobody reads it for. The whole URL is what Copy
    /// Diagnostics carries and what the detach confirmation spells out.
    var parentHost: String? {
        guard let parent, !parent.isEmpty else { return nil }
        return URL(string: parent)?.host ?? parent
    }
}

/// One row of `GET /hosts` — a machine this hub adopted, as every device on
/// this hub sees it.
///
/// `delegated` is the fact the tile cannot carry on its own, and the reason
/// this row is drawn at all: a machine enrolled by a build that predates
/// delegated minting shows up on every device identically to one that works,
/// and 502s the moment somebody taps it. Optional rather than defaulted false,
/// because a host too old to answer the field has not said "no" — it has said
/// nothing, and a row claiming "pair by hand" on no evidence is the kind of
/// check that is worse than no check.
struct AdoptedHost: Decodable {
    var key: String
    var name: String?
    var address: String?
    var port: Int?
    var enrolledAt: Int?
    var delegated: Bool?
    var seesHome: Bool?
    var seesLeaves: Bool?

    /// What to call it: the name given at adoption, else the key, which is the
    /// machine's own host id and always there.
    var title: String {
        let named = (name ?? "").trimmingCharacters(in: .whitespaces)
        return named.isEmpty ? key : named
    }

    /// Where it is on the mesh. Empty where the row was written without a peer
    /// — a real state, and one the grant route refuses before it dials.
    var route: String {
        let addr = (address ?? "").trimmingCharacters(in: .whitespaces)
        guard !addr.isEmpty else { return "" }
        return "\(addr):\(port ?? 9090)"
    }
}

struct AdoptedHostList: Decodable { var hosts: [AdoptedHost] }

struct UpdateJobStatus: Decodable {
    var id: String?
    var state: String?
    var detail: String?
}

struct UpdateMachine: Decodable {
    var machine: String
    var name: String
    var desired: String?
    var state: String
    var lastContact: Double?
    var supervisor: Bool
    var job: UpdateJobStatus?
    var observed: UpdateObservation?
    var contactStatus: String?

    private var idleForUpdate: Bool {
        desired != nil && !["downloading", "applying", "verifying", "current", "pending",
                            "pending/offline"].contains(state)
    }
    var canUpdate: Bool { supervisor && idleForUpdate }
    var needsBootstrap: Bool {
        !supervisor && desired != nil && !["downloading", "applying", "verifying", "current"]
            .contains(state)
    }
    var summary: String {
        if needsBootstrap { return "Updater setup required" }
        switch state {
        case "not_published", "unknown": return "No update published"
        case "unknown/offline": return lastContact == nil ? "Not connected to updates" : "Offline"
        case "pending/offline": return "Update queued · offline"
        case "current": return "Up to date"
        case "available": return "Update available"
        case "rolled_back": return "Previous version restored"
        default: return state.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }
}

struct UpdateComponent: Decodable {
    var installed: String?
    var runningPids: [Int]?
    var version: String?
    var distribution: String?
}

struct UpdateSource: Decodable {
    var sha: String?
    var dirty: Bool?
    var version: String?
    var release: String?
    var build: Int?

    var displayVersion: String {
        guard let version else { return "Version not reported" }
        return build.map { "\(version) (\($0))" } ?? version
    }
}

struct UpdateObservation: Decodable {
    var components: [String: UpdateComponent]?
    var hostSource: UpdateSource?
}

struct UpdateInventory: Decodable {
    var release: String?
    var machines: [UpdateMachine]

    func localUpdate(hostID: String?) -> UpdateMachine? {
        guard release != nil, let hostID else { return nil }
        return machines.first {
            $0.machine == hostID && ($0.canUpdate || $0.needsBootstrap)
        }
    }
}

/// One snapshot of the machine, as the menu will render it.
struct HostState {
    var installed = false
    var health: Health?
    var sessions: [Session] = []
    var sessionsReadable = false
    /// The host's roster of paired devices, revoked ones included — the same
    /// list the client app shows, because #27 is the menu bar owning it too.
    var devices: [Device] = []
    /// The machines this hub adopted. Empty on a machine that adopted none,
    /// which is most of them — the section it feeds is hidden then.
    var leaves: [AdoptedHost] = []
    /// The token exists but the board refused it — worth its own state, because
    /// it is the one failure that looks identical to "nothing is running".
    var unauthorized = false
    /// Identified through `/host` rather than `/api/health`: this API is being
    /// served by something larger that owns the health route. Its lifecycle is
    /// not ours, so the agent controls stay off.
    var embedded = false

    var isUp: Bool { health?.isOurHost == true }
    var isProvisioned: Bool { health?.provisioned == true }
    var liveCount: Int { sessions.filter { $0.live == true }.count }

    /// Which machine on the mesh this is, and what it is doing about it.
    var identity: HostIdentity?
    /// The bind address the agent was installed with, where a plist says so.
    var bind: String?

    /// The machine row's second line: the port, and what this Mac is on the
    /// mesh.
    ///
    /// The port because it is the fact you actually need — the thing you type,
    /// the thing you forward, the thing that is wrong when nothing answers.
    /// "1 working" was a number already on the row above it.
    ///
    /// Hub or leaf is said outright because the two are reached in opposite
    /// directions and the menu is where that gets confused: a hub is dialled
    /// *into* and only from outside your LAN if something forwards or tunnels
    /// to it, while a leaf dialled *out* to its hub and needs nothing forwarded
    /// at all. Saying "hub" without saying it is not itself reachable from the
    /// internet would be the more useful half of the truth left out.
    var headline: String {
        guard isUp else {
            // A refused token is not a missing hub, and this is the line that
            // decides which of those a person believes. "Not answering on port
            // 9090" over a hub that answers on 9090 sends them to restart a
            // service that is already running, and the one real repair — the
            // host's own credential has gone stale — is not hinted at anywhere
            // on the menu. Say which failure it is, and the port stays on the
            // row because it is still the fact you need.
            if unauthorized {
                return "Port \(HostAgent.port()) · refused this Mac's token"
            }
            return installed ? "Not answering on port \(HostAgent.port())"
                             : "No hub on this Mac"
        }
        var parts = ["Port \(HostAgent.port())"]
        // Loopback is the one bind that changes what the port means, and it is
        // knowable only where an agent plist recorded it. Unknown says nothing
        // rather than claiming reach this app never measured.
        if let bind, bind == "127.0.0.1" || bind == "localhost" {
            parts.append("this Mac only")
        } else if let m = identity?.mode?.mode {
            // The host's own verdict — local / open / managed — in its own word.
            // A managed host whose tunnel is down still says "managed", and adds
            // that it is not reachable through its parent right now, because the
            // attachment stands while the path is out.
            //
            // And *which* hub, where there is one. "managed" alone is the half
            // of the truth that cannot be acted on: a machine administered from
            // somewhere else is only a useful thing to know once you know from
            // where, and this row is the one place a person looks for it.
            if let parent = identity?.mode?.parentHost {
                parts.append("managed by \(parent)")
            } else {
                parts.append(m)
            }
            if identity?.mode?.live == false { parts.append("offline") }
        } else {
            // Older host with no mode field: the coarser hub/leaf read.
            switch identity?.isHub {
            case true:  parts.append("hub")
            case false: parts.append("leaf")
            case nil:   break
            }
        }
        if !isProvisioned { parts.append("no token") }
        // A host answering with no agent of its own and no larger app behind it
        // is `jstack-host serve` — a foreground run in somebody's terminal.
        // Worth a word, because it is the one arrangement that does not survive
        // closing that window.
        if !installed && !embedded { parts.append("in a terminal") }
        return parts.joined(separator: " · ")
    }

}

// MARK: - Asking

/// Polls the host and hands back a whole snapshot.
///
/// Two requests, and the second is the reason the token is read at all:
/// `/api/health` is deliberately unauthenticated so "is anyone there" can be
/// answered before a token exists, but it says nothing about what is on the
/// machine — the board is behind the token, and this app is the one client
/// entitled to read it off disk, because it is running on the host, as the user
/// who owns it.
final class HostProbe {
    private let session: URLSession

    init() {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 3
        config.waitsForConnectivity = false
        session = URLSession(configuration: config)
    }

    /// Where the token-bearing routes live. `/api/health` is not under it —
    /// the health probe is mounted on the app itself, precisely so it stays
    /// reachable without the version prefix or the token.
    static let apiPrefix = "/api/jremote/v1"

    func poll(_ done: @escaping (HostState) -> Void) {
        var state = HostState()
        state.installed = HostAgent.isInstalled
        state.bind = HostAgent.bind()
        let port = HostAgent.port()
        let base = "http://127.0.0.1:\(port)"
        let finish = { DispatchQueue.main.async { done(state) } }

        get("\(base)/api/health", token: nil) { data, _ in
            if let data, let health = try? Self.decoder.decode(Health.self, from: data) {
                state.health = health
            }
            let token = HostAgent.token()

            let loadSessions = {
                guard let token else { return finish() }
                self.get("\(base)\(Self.apiPrefix)/sessions/active", token: token) { data, status in
                    if status == 401 || status == 403 {
                        state.unauthorized = true
                        HostAgent.refuse(token)
                    }
                    if let data,
                       let active = try? Self.decoder.decode(ActiveSessions.self, from: data) {
                        state.sessions = active.sessions ?? []
                        state.sessionsReadable = status == 200 && active.sessions != nil
                    }
                    // The roster, on the same token and last. A device-list
                    // failure must not throw away the sessions already gathered,
                    // and a token the sessions call already found unauthorized
                    // has recorded that above — so this leg only adds, and its
                    // own 401 would land the same way rather than undo anything.
                    guard state.identity?.canManageDevices == true else { return finish() }
                    self.get("\(base)\(Self.apiPrefix)/devices", token: token) { data, _ in
                        if let data,
                           let list = try? Self.decoder.decode(DeviceList.self, from: data) {
                            state.devices = list.devices
                        }
                        // The adopted machines, last and on the same terms: an
                        // empty answer is the common one (most hubs adopted
                        // nobody) and a failure here must not cost the roster
                        // above it.
                        self.get("\(base)\(Self.apiPrefix)/hosts", token: token) { data, _ in
                            if let data,
                               let list = try? Self.decoder.decode(AdoptedHostList.self,
                                                                   from: data) {
                                state.leaves = list.hosts
                            }
                            finish()
                        }
                    }
                }
            }

            // `/host` on the healthy path too, not only the embedded one. It
            // carries hub-or-leaf, and a menu that can only say which of those
            // this Mac is when the host was awkward to find is a menu that
            // says it least often on the machines set up properly.
            if state.isUp {
                guard let token else { return loadSessions() }
                return self.get("\(base)\(Self.apiPrefix)/host", token: token) { data, _ in
                    if let data,
                       let identity = try? Self.decoder.decode(HostIdentity.self, from: data) {
                        state.identity = identity
                    }
                    loadSessions()
                }
            }

            // `/api/health` did not claim to be a host. That is not the same as
            // no host: the router is mountable inside a larger app, and such a
            // deployment answers `/api/health` with its own payload while
            // serving this whole API underneath. So ask the host's own identity
            // route, which is behind the token *because* the token is the proof
            // — a loopback host that rejects ours is not ours, whatever it
            // would have claimed. A 200 here settles it.
            guard let token else { return finish() }
            self.get("\(base)\(Self.apiPrefix)/host", token: token) { data, status in
                // A refusal here is the *only* signal on this path. The healthy
                // route records 401 on its sessions call, but an embedded host
                // never reaches that route: `/api/health` belongs to the larger
                // app, so `isUp` is false and this call is the whole probe. Drop
                // its status and a hub whose own credential has gone stale is
                // indistinguishable from a hub that was never installed — which
                // is what the menu said on this Mac, over a host that was up and
                // answering, while `/hosts` held a live leaf.
                if status == 401 || status == 403 {
                    state.unauthorized = true
                    HostAgent.refuse(token)
                }
                guard status == 200, let data,
                      let identity = try? Self.decoder.decode(HostIdentity.self, from: data)
                else { return finish() }
                state.health = Health(service: "jremote-host",
                                      profile: identity.profile,
                                      provisioned: true)
                state.embedded = true
                state.identity = identity
                loadSessions()
            }
        }
    }

    private static var decoder: JSONDecoder {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }

    func updates(_ done: @escaping (UpdateInventory?, String?) -> Void) {
        guard let token = HostAgent.updaterToken() else {
            done(nil, "Local updater credential unavailable")
            return
        }
        get("http://127.0.0.1:\(HostAgent.port())\(Self.apiPrefix)/updates/inventory", token: token) { data, status in
            let inventory = data.flatMap { try? Self.decoder.decode(UpdateInventory.self, from: $0) }
            let error = status == 404 ? "Host update required to enable managed updates"
                : "Update status unavailable (\(status))"
            DispatchQueue.main.async { done(inventory, inventory == nil ? error : nil) }
        }
    }

    func update(target: String, requestID: String,
                _ done: @escaping (Bool, String) -> Void) {
        guard let token = HostAgent.updaterToken(),
              let url = URL(string: "http://127.0.0.1:\(HostAgent.port())\(Self.apiPrefix)/updates/queue")
        else { return done(false, "Local updater credential unavailable") }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 15
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONEncoder().encode(["target": target, "request_id": requestID])
        session.dataTask(with: request) { data, response, error in
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            let text = data.flatMap { String(data: $0, encoding: .utf8) } ?? error?.localizedDescription ?? "No response"
            // Update All may be partially accepted. Do not hide refused rows
            // behind the HTTP 200 returned for the successfully queued ones.
            let body = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            let partial = !(body?["errors"] as? [Any] ?? []).isEmpty
            DispatchQueue.main.async { done(status == 200 && !partial, text) }
        }.resume()
    }

    /// Kill a session, through the host's own close route so the teardown is
    /// the one the app and the board already use — not a second implementation
    /// that gets the window or the registry wrong.
    ///
    /// `review=false`, and that is the whole difference between the two words.
    /// `review=true` is *Close*: EOF, `claude` exits cleanly, its SessionEnd
    /// hook fires and spawns a review of the session you just ended. Kill is
    /// not a polite exit — asking for one and getting a review agent is the
    /// opposite of what the word promises. False SIGKILLs the pane, no hook
    /// runs, nothing is spawned. The transcript is append-only and resume
    /// tolerates a truncated tail, so the work is still there.
    func kill(sid: String, token: String,
              _ done: @escaping (Bool, String) -> Void) {
        // The teardown waits on `claude` to exit, up to ten seconds — well past
        // the three the polls are configured for.
        post("/sessions/\(escaped(sid))/close?review=false",
             token: token, timeout: 20, done)
    }

    /// Revoke a device through the host's own `/devices/{id}/revoke` — the same
    /// route the client app uses, so the roster stays one list with one meaning
    /// of "removed", not two implementations that can disagree. From the 200 the
    /// token opens nothing and any live connection it held is already cut.
    func revoke(deviceId: String, token: String,
                _ done: @escaping (Bool, String) -> Void) {
        post("/devices/\(escaped(deviceId))/revoke", token: token, timeout: 10,
             removal: true, done)
    }

    /// Drop an adopted machine from the grid, through the host's own route.
    ///
    /// Forgetting is not revoking, and the confirmation says so: it takes the
    /// tile off every device and drops the grant this hub held, so nobody here
    /// can mint on that machine any more. The credentials already minted over
    /// there are that machine's own rows and stay live until it revokes them —
    /// which is a thing only it can do, so this must not claim to have done it.
    func forget(hostKey: String, token: String,
                _ done: @escaping (Bool, String) -> Void) {
        post("/hosts/\(escaped(hostKey))/forget", token: token, timeout: 10, done)
    }

    func visibility(hostKey: String, seesHome: Bool, seesLeaves: Bool, token: String,
                    _ done: @escaping (Bool, String) -> Void) {
        post("/hosts/\(escaped(hostKey))/visibility", token: token, timeout: 10,
             body: ["sees_home": seesHome, "sees_leaves": seesLeaves], done)
    }

    private func escaped(_ component: String) -> String {
        component.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed)
            ?? component
    }

    /// One POST, one verdict, one line of detail — what all three verbs above
    /// need and the only thing that differs between them is the path. Written
    /// once because the interesting part is the failure text: a menu that says
    /// "could not remove" and nothing else is a menu that sends its user to the
    /// logs, so whatever the host actually answered is carried up verbatim.
    private func post(_ path: String, token: String, timeout: TimeInterval,
                      body: [String: Bool]? = nil,
                      removal: Bool = false,
                      _ done: @escaping (Bool, String) -> Void) {
        let port = HostAgent.port()
        guard let url = URL(string: "http://127.0.0.1:\(port)\(Self.apiPrefix)\(path)")
        else { return done(false, "could not build the request") }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let body {
            req.httpBody = try? JSONEncoder().encode(body)
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        req.timeoutInterval = timeout
        session.dataTask(with: req) { data, response, error in
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            let detail: String
            if let error { detail = error.localizedDescription }
            else if let data, let text = String(data: data, encoding: .utf8), !text.isEmpty {
                detail = text
            } else { detail = "the hub answered \(status)" }
            let ok = removal ? DeviceMenu.removalSucceeded(status: status, data: data) : status == 200
            DispatchQueue.main.async { done(ok, detail) }
        }.resume()
    }

    private func get(_ url: String, token: String?,
                     _ done: @escaping (Data?, Int) -> Void) {
        guard let url = URL(string: url) else { return done(nil, 0) }
        var req = URLRequest(url: url)
        if let token { req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        session.dataTask(with: req) { data, response, _ in
            done(data, (response as? HTTPURLResponse)?.statusCode ?? 0)
        }.resume()
    }
}

// MARK: - Doing

/// The operations the menu offers. Every one of them shells out to the same
/// tools a person would type — `launchctl` for the agent, `jstack-host` for the
/// host — because a second implementation of "restart the host" is a second
/// thing to keep true.
/// What this Mac *is*, for the row that names it.
///
/// "Hosting — jj · port 9090" described the software's configuration, which is
/// not what someone opening a menu about their machine is looking for. A hub
/// is a machine; the row should say which one, the way every other Apple
/// surface does — a Mac Studio icon and the words "M2 Max Mac Studio".
enum Machine {
    /// `system_profiler` is the only source that knows the marketing name, and
    /// it costs the better part of a second — so it is asked once, lazily, at
    /// the first menu build rather than on every poll.
    static let name: String = {
        let out = HostControl.run("/usr/sbin/system_profiler", ["SPHardwareDataType"]).out
        func field(_ label: String) -> String {
            for line in out.split(separator: "\n") {
                let parts = line.split(separator: ":", maxSplits: 1)
                guard parts.count == 2,
                      parts[0].trimmingCharacters(in: .whitespaces) == label
                else { continue }
                return parts[1].trimmingCharacters(in: .whitespaces)
            }
            return ""
        }
        let model = field("Model Name")                       // "Mac Studio"
        // "Apple M2 Max" — the word Apple is not information on an Apple menu.
        var chip = field("Chip")
        if chip.hasPrefix("Apple ") { chip.removeFirst("Apple ".count) }
        switch (model.isEmpty, chip.isEmpty) {
        case (false, false): return "\(chip) \(model)"        // "M2 Max Mac Studio"
        case (false, true):  return model
        case (true, false):  return chip
        case (true, true):   return Host.current().localizedName ?? "This Mac"
        }
    }()

    /// The device glyph. Named from the marketing name rather than the model
    /// identifier: `Mac14,13` says nothing without a table that goes stale
    /// every autumn, and "Mac Studio" is stable English.
    static let symbol: String = {
        let m = name.lowercased()
        if m.contains("macbook")    { return "laptopcomputer" }
        if m.contains("mac studio") { return "macstudio" }
        if m.contains("mac mini")   { return "macmini" }
        if m.contains("mac pro")    { return "macpro.gen3" }
        if m.contains("imac")       { return "desktopcomputer" }
        return "desktopcomputer"
    }()
}

/// The client app, if this Mac has one installed.
///
/// Found by bundle id rather than a path: an app is wherever the person who
/// installed it put it, and `/Applications` is a guess. `JREMOTE_APP_BUNDLE_ID`
/// names a different build — a debug one, say — without a rebuild of this.
enum RemoteApp {
    /// The bundle name, which is the product's name and nothing else.
    ///
    /// Not a bundle identifier: an identifier carries whoever signed the
    /// build — a person's or a company's name — and this file ships in a
    /// public repository, so hardcoding one would publish that. Set
    /// `JREMOTE_APP_BUNDLE_ID` to name your own build's identifier instead;
    /// it wins where it is set, and nothing is written down here.
    static let bundleName = "jRemote"

    /// Resolved on every read, not cached: the app can be installed while this
    /// menu bar item is running, and an item that stays missing until the next
    /// login is one that looks broken.
    static var url: URL? {
        if let id = ProcessInfo.processInfo.environment["JREMOTE_APP_BUNDLE_ID"],
           !id.isEmpty {
            return NSWorkspace.shared.urlForApplication(withBundleIdentifier: id)
        }
        let fm = FileManager.default
        for dir in [URL(fileURLWithPath: "/Applications"),
                    fm.homeDirectoryForCurrentUser.appendingPathComponent("Applications")] {
            let candidate = dir.appendingPathComponent("\(bundleName).app")
            if fm.fileExists(atPath: candidate.path) { return candidate }
        }
        return nil
    }
}

/// Tells a running client to bring its board forward rather than whatever
/// window happens to be frontmost.
///
/// `NSWorkspace.openApplication` activates and reuses the running instance,
/// but activation only raises whatever is already frontmost — a thread
/// window left on top stays on top. Which window is "the board" is knowable
/// only inside that process, so the client listens on this loopback port for
/// one line and does the choosing itself; see that file's own BoardControl
/// for the far end. Fixed on both ends, not negotiated: a runtime port needs
/// a second channel to publish it on, which is the exact problem this exists
/// to avoid, and one Mac runs one client.
///
/// Best-effort. No client, an older build with no listener, a cold launch
/// still short of `listen()` — every one of those is answered by the
/// activation call already made, so a failure here is silent.
enum BoardRaise {
    private static let port = NWEndpoint.Port(rawValue: 52845)!

    /// Retries through a cold launch: `openApplication` returns once the
    /// process exists, not once its listener is up, and the gap between the
    /// two is exactly the window this walks.
    static func send(deadline: TimeInterval = 5) {
        let start = Date()
        var done = false

        func attempt() {
            let connection = NWConnection(host: "127.0.0.1", port: port, using: .tcp)
            connection.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    connection.send(content: Data("RAISE-BOARD\n".utf8),
                                    completion: .contentProcessed { _ in
                        done = true
                        connection.cancel()
                    })
                case .failed, .waiting:
                    connection.cancel()
                    guard !done, Date().timeIntervalSince(start) < deadline else { return }
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.3, execute: attempt)
                default:
                    break
                }
            }
            connection.start(queue: .main)
        }
        attempt()
    }
}

/// The client app's mark, drawn rather than shipped.
///
/// A chevron and a cursor — a prompt, which is what the app opens onto. The
/// geometry is the app icon's own, reduced to what it actually is: two
/// round-capped strokes on SF Symbols' 70.459-unit cap-height canvas, so it
/// carries the same optical weight as the system glyphs on the rows around it
/// at every size and in both appearances.
///
/// Drawn in code because this app is one Swift file compiled by `install.sh`
/// with nothing but `swiftc`. A custom SF Symbol means an asset catalog, an
/// asset catalog means `actool`, and `actool` ships with Xcode rather than the
/// command line tools — a whole new build dependency for one icon. A PDF or a
/// PNG in the bundle would be a binary blob in a repository whose entire claim
/// is that you can read the thing you are about to run.
enum JRemoteGlyph {
    /// The canvas the geometry was authored on: SF Symbols' cap height, which
    /// is why scaling against the system font's cap height below lines this up
    /// with `NSImage(systemSymbolName:)` instead of near it.
    private static let capHeight: CGFloat = 70.459
    private static let width: CGFloat = 92.6406
    private static let stroke: CGFloat = 18.2672
    /// The left edge of the *drawn* result — the first stroke's centre less its
    /// own round cap, which is what actually reaches the edge of the box.
    private static let originX: CGFloat = 9.766

    /// Centre lines in the authoring space, where y runs negative upward from
    /// the baseline.
    private static let chevron = [
        CGPoint(x: 18.8996, y: -61.3254),
        CGPoint(x: 55.4339, y: -35.2295),
        CGPoint(x: 18.8996, y: -9.1336),
    ]
    private static let cursor = [
        CGPoint(x: 65.8722, y: -9.1336),
        CGPoint(x: 93.2730, y: -9.1336),
    ]

    static func image(size pointSize: CGFloat) -> NSImage {
        let scale = NSFont.systemFont(ofSize: pointSize).capHeight / capHeight
        let box = NSSize(width: width * scale, height: capHeight * scale)
        let image = NSImage(size: box, flipped: false) { _ in
            func map(_ p: CGPoint) -> NSPoint {
                NSPoint(x: (p.x - originX) * scale, y: -p.y * scale)
            }
            let path = NSBezierPath()
            path.move(to: map(chevron[0]))
            for point in chevron.dropFirst() { path.line(to: map(point)) }
            path.move(to: map(cursor[0]))
            path.line(to: map(cursor[1]))
            path.lineWidth = stroke * scale
            path.lineCapStyle = .round
            path.lineJoinStyle = .round
            NSColor.black.setStroke()
            path.stroke()
            return true
        }
        // A template, so it inverts with the menu the way every other row's
        // glyph does — a mark that stays black on a highlighted row is the one
        // that reads as pasted on.
        image.isTemplate = true
        return image
    }
}

/// What `jstack-host pair --json` answers with — the parts the pairing dialog
/// draws, instead of the paragraph it used to echo. `link` is the same
/// `jremote://pair` URL the installer fires at a local app; here it goes into
/// a QR so a *remote* device's camera can be the thing that fires it.
/// `jstack-host adopt --json` answers in this same shape — a code, a name, a
/// life and the addresses the far end can send it to — so it is parsed by this
/// same type rather than a near-copy of it. The one difference is `link`, which
/// adopt does not mint: a host code is redeemed by a command on another Mac's
/// terminal, not by a camera, and a QR that opens the client app would be the
/// wrong instruction in a prettier form.
struct MintedPairing {
    let name: String
    let code: String
    let expiresIn: Int
    let port: Int
    /// Every address the host named, in its own order — for `pair`, where a
    /// phone genuinely may be on this Wi-Fi or on the mesh and choosing is
    /// the point.
    let allAddresses: [String]
    let firstAddress: String?
    /// The one address on a real network, or nil. The answer for a machine
    /// that does not hold the tunnel yet — its only first-contact address.
    let lanAddress: String?
    /// This hub's own mesh address, or nil if it runs no mesh. The address
    /// every device uses once it holds the tunnel — which, after the first
    /// pairing, is every device, from anywhere in the world.
    let meshAddress: String?
    let link: String?

    init?(json: String) {
        guard let data = json.data(using: .utf8),
              let raw = try? JSONSerialization.jsonObject(with: data),
              let top = raw as? [String: Any],
              let code = top["code"] as? String, !code.isEmpty
        else { return nil }
        self.code = code
        name = (top["name"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "a device"
        expiresIn = top["expires_in"] as? Int ?? 600
        port = top["port"] as? Int ?? 9090
        let addresses = top["addresses"] as? [[String: Any]] ?? []
        allAddresses = addresses.compactMap { $0["url"] as? String }
        firstAddress = allAddresses.first
        lanAddress = addresses.first { $0["kind"] as? String == "lan" }?["url"] as? String
        meshAddress = addresses.first { $0["kind"] as? String == "mesh" }?["url"] as? String
        link = top["link"] as? String
    }

    /// The line to run on the machine being adopted, against `parent`.
    func attachCommand(_ parent: String) -> String {
        "jstack-host attach \(code) --parent \(parent)"
    }

    /// The command to lead with — mesh first, because that is the one that
    /// works from where the machine usually *is*.
    ///
    /// This dialog used to print the LAN address alone, under the flat claim
    /// that the machine "has to be on this network". Both were wrong, and they
    /// were wrong in the direction that costs the most: the reader who is *not*
    /// on this network — the only reader who needs the dialog — was handed the
    /// one address that cannot reach the hub for them, and told the failure was
    /// their fault for being elsewhere.
    ///
    /// Redeeming is an HTTP call, and the redeem endpoint applies no locational
    /// rule at all — `tunnel.issue` drops it deliberately, because the code is
    /// the authorization that a LAN source address merely stands in for. So the
    /// real precondition is not *where the machine is*, it is *whether it holds
    /// the tunnel*: a machine already on the mesh adopts from anywhere in the
    /// world, and 3150 mesh requests have reached this hub's HTTP that way.
    /// Only a machine with nothing on it yet has to be on this network once,
    /// because the first tunnel is precisely what `attach` hands back.
    ///
    /// `.local` stays out of both branches: it needs the same LAN as the
    /// numeric address while resolving less reliably on it, so it is never
    /// right when the number is available and never available when it is not.
    ///
    /// Optional, and nil is the whole change here. This fell through to
    /// `http://<this-mac>:\(port)` when neither address was found, which put a
    /// placeholder inside a line whose entire purpose is to be copied to
    /// another keyboard and run — and the one blank in it is the field the
    /// operator cannot fill, because working out this hub's address from the
    /// other Mac is the question the dialog exists to answer. A command with a
    /// hole in it does not read as an error; it reads as an instruction.
    var attachCommand: String? {
        guard let parent = meshAddress ?? lanAddress else { return nil }
        return attachCommand(parent)
    }

    /// "10 minutes", from seconds — the dialog says how long the code lives,
    /// and a number nobody rounds for them is homework.
    var validFor: String {
        let mins = max(1, expiresIn / 60)
        return mins == 1 ? "1 minute" : "\(mins) minutes"
    }
}

/// Which machines this hub adopted are answering it over the mesh right now —
/// `jstack-host leaves --json`, read before the adopt dialog is drawn.
///
/// There are two ways to adopt a Mac and they are not alternatives. A Mac that
/// can reach this hub redeems a code where it stands; a Mac that cannot has
/// the tunnel carried to it in a file, because redeeming needs a route and the
/// route is what redeeming hands back. Nothing steered between them, so the
/// carried file was offered for machines with no use for it — the operator
/// walked a file to a Mac that was already answering over the tunnel, and that
/// Mac's bundle was rewritten with a fresh code on the way out (#61).
///
/// The question is asked of the hub, not guessed from the registry: a row
/// survives the machine being wiped, and a wiped machine is exactly the one
/// the carried file is for.
struct MeshRoster {
    /// Peer names whose machine is live *and answering*. Only `online == true`
    /// lands here. Null — the hub could not read its own peer table — stays
    /// out on purpose: a dialog that read "could not tell" as "already on the
    /// mesh" would take the carried file away from the machine that needs it
    /// most, and that machine has no other route in.
    let live: Set<String>

    static let empty = MeshRoster(live: [])

    init(live: Set<String>) { self.live = live }

    init?(json: String) {
        struct Row: Decodable {
            var peer: String?
            var online: Bool?
        }
        // The whole output first, then its last line. `HostControl.run` folds
        // stderr into stdout, so a warning printed ahead of the payload must
        // not read as "this hub adopted nothing".
        let whole = json.data(using: .utf8)
        let tail = json.split(separator: "\n").last.flatMap { $0.data(using: .utf8) }
        let rows = [whole, tail].compactMap { $0 }
            .compactMap { try? JSONDecoder().decode([Row].self, from: $0) }
        guard let first = rows.first else { return nil }
        live = Set(first.filter { $0.online == true }
                        .compactMap { $0.peer }
                        .filter { !$0.isEmpty })
    }

    /// Is the machine the operator just named already on the mesh.
    func holds(_ typed: String) -> Bool {
        let peer = Self.peerName(typed)
        return !peer.isEmpty && live.contains(peer)
    }

    /// `enrolment.peer_name`, in Swift — "Work Mac" → "work-mac".
    ///
    /// The second copy of a rule, which is worth saying out loud: the name the
    /// operator types is a display name, the peer is a filename and a config
    /// stanza key, and the hub derives one from the other at adoption. Matching
    /// on the typed name instead would miss the machine whose row reads "Work
    /// Mac" the moment someone types "work-mac", which is the same miss this
    /// whole check exists to close. The hub still owns the rule — it emits the
    /// peer name it actually holds, and this only has to slug the live keystroke
    /// the same way to compare against it.
    static func peerName(_ name: String) -> String {
        let runs = name.lowercased().map { ch -> Character in
            (ch >= "a" && ch <= "z") || (ch >= "0" && ch <= "9") ? ch : "-"
        }
        var collapsed = ""
        for ch in runs where ch != "-" || collapsed.last != "-" {
            collapsed.append(ch)
        }
        var slug = String(collapsed.drop(while: { $0 == "-" }))
        while slug.last == "-" { slug.removeLast() }
        slug = String(slug.prefix(31))
        while slug.last == "-" { slug.removeLast() }
        return slug
    }
}

/// Reports every keystroke in an `NSTextField` to a closure.
///
/// Because the adopt dialog has to steer on a name that does not exist until
/// the operator types it: the choice between the two routes and the name they
/// apply to are on the same panel, so the offline button's state has to follow
/// the field rather than be decided once when the panel opens.
final class FieldWatcher: NSObject, NSTextFieldDelegate {
    private let onChange: (String) -> Void

    init(_ onChange: @escaping (String) -> Void) {
        self.onChange = onChange
    }

    func controlTextDidChange(_ note: Notification) {
        onChange((note.object as? NSTextField)?.stringValue ?? "")
    }
}

/// What `jstack-host detach --json` answers: every step by name, whether this
/// machine is actually out, and the mode it landed in.
///
/// Steps rather than a verdict, because they fail independently and mean
/// different things — the grants are the authority, the calls to the parent are
/// courtesy over a mesh that is coming down, and the tunnel is the transport.
/// The mode is read as well as `detached`: the steps can all pass on a machine
/// that still dials out from a second leaf install somewhere, and the mode is
/// the only thing that would say so.
struct DetachOutcome {
    let steps: [(ok: Bool, note: String)]
    let detached: Bool
    let mode: String
    let note: String

    var isManaged: Bool { mode == "managed" }

    init?(json: String) {
        // The whole output first, then its last line — a stray warning ahead of
        // the payload must not turn a detach that reported itself fully into
        // "did not report an outcome".
        let whole = json.data(using: .utf8)
        let tail = json.split(separator: "\n").last.flatMap { $0.data(using: .utf8) }
        let parsed = [whole, tail].compactMap { $0 }
            .compactMap { try? JSONSerialization.jsonObject(with: $0) }
            .compactMap { $0 as? [String: Any] }
        guard let top = parsed.first else { return nil }
        let rows = top["steps"] as? [[String: Any]] ?? []
        steps = rows.map { (ok: $0["ok"] as? Bool ?? false,
                            note: $0["note"] as? String ?? "") }
        detached = top["detached"] as? Bool ?? false
        let landed = top["mode"] as? [String: Any] ?? [:]
        mode = landed["mode"] as? String ?? ""
        note = landed["note"] as? String ?? ""
    }

    /// The steps the way the command prints them in a terminal, and the mode
    /// underneath — the same report, in the place the person actually ran it.
    var report: String {
        var lines = steps.map { "\($0.ok ? "✓" : "✗")  \($0.note)" }
        if !mode.isEmpty { lines.append("\nmode  \(mode) — \(note)") }
        return lines.joined(separator: "\n")
    }
}

enum HostControl {
    /// `gui/<uid>`: the per-user domain, which is where a LaunchAgent lives and
    /// the reason none of this needs a password.
    static var domain: String { "gui/\(getuid())" }

    @discardableResult
    static func run(_ launchPath: String, _ args: [String]) -> (out: String, code: Int32) {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: launchPath)
        task.arguments = args
        // The installed host's own variables, handed to every tool this app
        // runs. Without them `jstack-host` answers about whatever a launched
        // GUI process happens to have inherited — which on this app is nothing
        // — and that is not a smaller answer, it is a different machine's: a
        // Mac holding 10.66.0.1 with five peers reports `mode local` because
        // the process was never told where its mesh is. The app is the one
        // thing that knows which agent serves the host, so it is the one thing
        // that can say. Whatever was exported to this app wins, so a menu bar
        // launched by hand against a second host still talks to that one.
        var env = ProcessInfo.processInfo.environment
        env.merge(HostAgent.environment()) { mine, _ in mine }
        task.environment = env
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = pipe
        do { try task.run() } catch { return ("", -1) }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        task.waitUntilExit()
        return (String(data: data, encoding: .utf8) ?? "", task.terminationStatus)
    }

    static func restart() {
        run("/bin/launchctl", ["kickstart", "-k", "\(domain)/\(HostAgent.label)"])
    }

    static func stop() {
        if HostAgent.appOwned {
            _ = serviceAction("unregister")
            return
        }
        run("/bin/launchctl", ["bootout", "\(domain)/\(HostAgent.label)"])
    }

    static func start() {
        if HostAgent.appOwned {
            let answer = serviceAction("register")
            if answer.out.contains("requires_approval") { SMAppService.openSystemSettingsLoginItems() }
            return
        }
        run("/bin/launchctl", ["bootstrap", domain, HostAgent.plistURL.path])
        run("/bin/launchctl", ["kickstart", "-k", "\(domain)/\(HostAgent.label)"])
    }

    private static func serviceAction(_ action: String) -> (out: String, code: Int32) {
        let controller = HostAgent.serviceController
        let owner = controller.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let requirement = "=anchor apple generic and certificate leaf[subject.OU] = \"MZ95H77RQQ\" and identifier \"live.jstack.hub\""
        let checked = run("/usr/bin/codesign", ["--verify", "--deep", "--strict", "-R", requirement, owner.path])
        guard checked.code == 0 else { return checked }
        return run(controller.path, [action, HostAgent.serviceRole])
    }

    /// Where `jstack-host` is. The installer passes `--host-bin` on the command
    /// line because it is the one thing that knows for certain; the search is
    /// the fallback for an app launched by hand.
    static var hostBinary: String? = {
        if HostAgent.appOwned { return Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/JStackCLI").path }
        let args = ProcessInfo.processInfo.arguments
        if let i = args.firstIndex(of: "--host-bin"), i + 1 < args.count,
           FileManager.default.isExecutableFile(atPath: args[i + 1]) {
            return args[i + 1]
        }
        // Beside the host's own interpreter, before any fixed location.
        //
        // An embedded host runs out of the environment the larger application
        // was installed into, and `jstack-host` is installed into that same
        // environment — so the agent's own argv names the copy that belongs to
        // the host actually running here. The fixed paths below can only find
        // whichever copy a machine happens to have on it, which on a machine
        // with two is a coin toss, and on this one is nothing at all: the
        // binary is in the dashboard's virtualenv and none of them look there.
        // That is why Pair a Device quietly failed to appear.
        if let program = HostAgent.programPath() {
            let sibling = URL(fileURLWithPath: program)
                .deletingLastPathComponent()
                .appendingPathComponent("jstack-host").path
            if FileManager.default.isExecutableFile(atPath: sibling) { return sibling }
        }
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        for candidate in ["\(home)/.local/bin/jstack-host",
                          "/opt/homebrew/bin/jstack-host",
                          "/usr/local/bin/jstack-host"] {
            if FileManager.default.isExecutableFile(atPath: candidate) { return candidate }
        }
        return nil
    }()
}

/// Whether a LaunchAgent brings itself up at login, and flipping that.
///
/// There is no permission to grant here and no API to ask. A *user* LaunchAgent
/// in `~/Library/LaunchAgents` is loaded at login by launchd with no prompt and
/// no sudo — it shows up afterwards in Login Items & Extensions as a switch you
/// may turn off, not as one you had to turn on. So the setting is a property of
/// the plist, and this reads and writes exactly that.
enum LoginAgent {
    static func plistURL(_ label: String) -> URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/\(label).plist")
    }

    static func exists(_ label: String) -> Bool {
        FileManager.default.fileExists(atPath: plistURL(label).path)
    }

    private static func job(_ label: String) -> [String: Any]? {
        guard let data = try? Data(contentsOf: plistURL(label)),
              let plist = try? PropertyListSerialization.propertyList(
                  from: data, options: [], format: nil) as? [String: Any]
        else { return nil }
        return plist
    }

    /// Does it come up at login?
    ///
    /// `RunAtLoad` is the obvious half. The other half is `KeepAlive` as a bare
    /// `true`, which means "keep this running" with no condition attached — so
    /// launchd starts it the moment the job is loaded, whatever `RunAtLoad`
    /// says or fails to say. A *dictionary* `KeepAlive` is conditional
    /// (`SuccessfulExit` and friends) and starts nothing on its own.
    ///
    /// Getting this wrong is how a checkbox ends up unticked next to a hub that
    /// has come up at every login for months.
    static func startsAtLogin(_ label: String) -> Bool {
        guard let job = job(label) else { return false }
        if job["KeepAlive"] as? Bool == true { return true }
        return job["RunAtLoad"] as? Bool == true
    }

    /// True where `KeepAlive` alone guarantees the start. Then `RunAtLoad` is
    /// not the knob, and a switch offering to flip it is a switch that lies —
    /// so the row is shown ticked and locked, with the reason on the tooltip,
    /// rather than offered as a choice that would not take.
    static func pinnedOn(_ label: String) -> Bool {
        job(label)?["KeepAlive"] as? Bool == true
    }

    /// Write the flag. Nothing is unloaded and nothing is restarted: launchd
    /// reads these files fresh at the next login, which is the only moment this
    /// setting means anything — and booting the job out to make a next-login
    /// setting "take" would stop the very thing being configured.
    @discardableResult
    static func setStartsAtLogin(_ label: String, _ on: Bool) -> Bool {
        guard var job = job(label) else { return false }
        job["RunAtLoad"] = on
        guard let data = try? PropertyListSerialization.data(
                  fromPropertyList: job, format: .xml, options: 0),
              (try? data.write(to: plistURL(label), options: .atomic)) != nil
        else { return false }
        return true
    }
}

/// This app's own LaunchAgent label — its bundle identifier, which is what
/// `install.sh` writes into both the bundle and the plist. Read rather than
/// repeated, so the two cannot drift apart.
enum MenuBarAgent {
    static let label = Bundle.main.bundleIdentifier ?? "com.jremote.menubar"
}

// MARK: - The menu bar

/// A reusable, nonmodal status window. Update actions retain their menu command
/// so the window and deep link use the same authority and request handling.
struct InfoAppSnapshot {
    var url: URL?
    var icon: NSImage?
    var version = "Not installed"
    var status = "Download jRemote to use this Mac’s sessions."
    var distribution = ""

    static func read() -> Self {
        guard let url = RemoteApp.url, let bundle = Bundle(url: url) else { return Self() }
        let version = bundle.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "—"
        let build = bundle.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "—"
        let running = NSWorkspace.shared.runningApplications.contains { $0.bundleURL == url }
        let store = FileManager.default.fileExists(atPath: url.appendingPathComponent("Contents/_MASReceipt/receipt").path)
        let channel = bundle.object(forInfoDictionaryKey: "JStackDistribution") as? String
        return Self(url: url, icon: NSWorkspace.shared.icon(forFile: url.path),
                    version: "\(version) (\(build))", status: running ? "Running" : "Installed",
                    distribution: store ? "App Store" : channel == "hub" ? "Managed by your hub" : "Separate installation")
    }
}

struct InfoCommand: View {
    let command: NSMenuItem
    var body: some View {
        Button(command.title) {
            guard command.isEnabled, let action = command.action else { return }
            NSApp.sendAction(action, to: command.target, from: command)
        }
        .disabled(!command.isEnabled)
        .accessibilityIdentifier(command.accessibilityIdentifier())
    }
}

struct InfoHubSection: View {
    let name: String
    let status: String
    let version: String
    let source: String?
    var body: some View {
        Section {
            LabeledContent("Mac", value: name)
            LabeledContent("Status", value: status)
            LabeledContent("Version", value: version)
            if let source {
                DisclosureGroup("Technical details") {
                    LabeledContent("Source", value: source)
                        .textSelection(.enabled)
                }
                .accessibilityIdentifier("info_hub_details")
            }
        } header: {
            Label("jStack Hub", systemImage: "server.rack")
                .font(.headline)
        }
    }
}

struct InfoClientSection: View {
    let app: InfoAppSnapshot
    let open: () -> Void
    let download: () -> Void
    var body: some View {
        Section {
            HStack(spacing: 12) {
                if let icon = app.icon {
                    Image(nsImage: icon).resizable().frame(width: 48, height: 48)
                        .accessibilityHidden(true)
                } else {
                    Image(systemName: "terminal").font(.largeTitle)
                        .frame(width: 48, height: 48).accessibilityHidden(true)
                }
                VStack(alignment: .leading, spacing: 4) {
                    Text("jRemote").font(.headline)
                    Text(app.version).foregroundStyle(.secondary)
                }
                Spacer()
                if app.url != nil {
                    Button("Open", action: open).accessibilityIdentifier("info_open_client")
                } else {
                    Button("Download…", action: download).accessibilityIdentifier("info_download_client")
                }
            }
            LabeledContent("Status", value: app.status)
            if !app.distribution.isEmpty {
                LabeledContent("Updates", value: app.distribution)
            }
        }
    }
}

struct InfoMachineSection: View {
    let machine: UpdateMachine
    let command: NSMenuItem?
    var body: some View {
        Section(machine.name) {
            LabeledContent("Updates", value: machine.summary)
            if let version = machine.observed?.hostSource?.version {
                LabeledContent("jStack", value: version)
            }
            if let client = machine.observed?.components?["client"], let build = client.installed {
                LabeledContent("jRemote", value: client.version.map { "\($0) (\(build))" } ?? "Build \(build)")
            }
            if let contact = machine.lastContact {
                LabeledContent("Last seen") {
                    Text(Date(timeIntervalSince1970: contact), format: .dateTime.month().day().hour().minute())
                }
            }
            if !machine.supervisor {
                Text("Run the current installer on this Mac once to enable managed updates.")
                    .font(.callout).foregroundStyle(.secondary)
            }
            if let detail = machine.job?.detail, !detail.isEmpty {
                Text(detail).font(.callout).textSelection(.enabled)
            }
            if let command { InfoCommand(command: command) }
        }
    }
}

struct HostInfoForm: View {
    let machine: String
    let status: String
    let version: String
    let source: String?
    let app: InfoAppSnapshot
    let updateStatus: String
    let error: String?
    let localCommand: NSMenuItem?
    let machines: [UpdateMachine]
    let commands: [String: NSMenuItem]
    let allCommand: NSMenuItem?
    let open: () -> Void
    let download: () -> Void

    var body: some View {
        Form {
            InfoHubSection(name: machine, status: status, version: version, source: source)
            InfoClientSection(app: app, open: open, download: download)
            Section("Software Updates") {
                LabeledContent("Status", value: updateStatus)
                if let error {
                    Text(error).foregroundStyle(.secondary).textSelection(.enabled)
                }
                if let localCommand { InfoCommand(command: localCommand) }
                if let allCommand { InfoCommand(command: allCommand) }
            }
            ForEach(machines, id: \.machine) { machine in
                InfoMachineSection(machine: machine, command: commands[machine.machine])
            }
        }
        .formStyle(.grouped)
        .accessibilityIdentifier("host_info_form")
    }
}

/// Keep a single hosting view alive across polls so focus and disclosures survive.
final class HostInfoWindow: NSWindow {
    private var hosting: NSHostingView<HostInfoForm>?
    init() {
        super.init(contentRect: NSRect(x: 0, y: 0, width: 520, height: 640),
                   styleMask: [.titled, .closable, .resizable, .miniaturizable],
                   backing: .buffered, defer: false)
        title = "jStack Info"
        isReleasedWhenClosed = false
        minSize = NSSize(width: 460, height: 400)
        setAccessibilityIdentifier("host_info_window")
        center()
    }

    func render(_ form: HostInfoForm) {
        if let hosting { hosting.rootView = form }
        else {
            let view = NSHostingView(rootView: form)
            hosting = view
            contentView = view
        }
    }
}

final class StatusController: NSObject {
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let probe = HostProbe()
    private var timer: Timer?
    private var state = HostState()
    private var removedDevices: Set<String> = []
    private var updateInventory: UpdateInventory?
    private var updateError: String?
    private var updateRequestInFlight = false
    private var updateActionStatus: String?
    private var menuIsOpen = false
    private var infoWindow: HostInfoWindow?

    override init() {
        super.init()
        item.button?.image = Self.glyph("desktopcomputer")
        item.button?.imagePosition = .imageLeading
        item.menu = NSMenu()
        item.menu?.delegate = self
        refresh()
        // Ten seconds: the answer changes without this app doing anything — an
        // install finishes in a terminal, the agent is booted out, the machine
        // wakes with it not back yet. An indicator frozen on the answer it got
        // at launch is the failure this exists to avoid.
        let t = Timer(timeInterval: 10, repeats: true) { [weak self] _ in self?.refresh() }
        RunLoop.main.add(t, forMode: .common)
        timer = t
    }

    private static func glyph(_ name: String) -> NSImage? {
        let image = NSImage(systemSymbolName: name, accessibilityDescription: "jStack host")
        image?.isTemplate = true
        return image
    }

    func refresh() {
        probe.updates { [weak self] inventory, error in
            guard let self else { return }
            self.updateInventory = inventory
            self.updateError = error
            self.refreshInfoWindow()
            if !self.menuIsOpen { self.build() }
        }
        probe.poll { [weak self] state in
            guard let self else { return }
            self.state = state
            // A poll started before a successful removal cannot resurrect it.
            self.state.devices = DeviceMenu.active(state.devices, removed: self.removedDevices)
            self.draw()
            self.refreshInfoWindow()
            if !self.menuIsOpen { self.build() }
        }
    }

    /// One shape, badged rather than swapped: an icon that changes silhouette
    /// between states is one nobody learns to find, and finding it is the whole
    /// job of a menu bar item. Down is the same glyph dimmed, which is what
    /// `appearsDisabled` is for.
    private func draw() {
        guard let button = item.button else { return }
        let unprovisioned = state.isUp && !state.isProvisioned
        // `server.rack`: what this machine is doing, not what it is. A desktop
        // glyph in a menu bar reads as "a Mac" — which is every Mac — where a
        // rack reads as the thing serving, which is the one fact the icon has
        // room to carry.
        button.image = Self.glyph("server.rack")
        // Colour rather than a second silhouette, so the shape stays learnable:
        // the badged variants exist only for some symbols, and swapping to one
        // moves the icon's outline the moment something is wrong — exactly when
        // you want to find it in the same place.
        button.contentTintColor = unprovisioned ? .systemOrange : nil
        button.appearsDisabled = !state.isUp
        let signal = SessionSignal.collective(state.sessions)
        button.attributedTitle = NSAttributedString(string: " ●", attributes: [
            .foregroundColor: state.sessionsReadable ? signal.color : NSColor.secondaryLabelColor,
            .font: NSFont.systemFont(ofSize: 10) // A status glyph, not reading text.
        ])
        // The same line the menu's first row shows, since it is the answer to
        // "what is this icon telling me" and the icon has no room for it.
        let activity = state.sessionsReadable ? signal.label : "Session status unavailable"
        button.toolTip = "\(Machine.name) — \(state.headline) · \(activity)"
        button.setAccessibilityLabel("jStack · \(activity)")
    }

    private func build() {
        let menu = NSMenu()
        menu.autoenablesItems = false

        // ── The machine, and what it is running ─────────────────────────────
        //
        // Two rows that open, rather than two lists spilled onto one surface.
        // The top level is then short enough to read at a glance — which is
        // what a menu bar is for — and each row's contents are one hover away
        // instead of scrolled past on the way to the next thing.
        //
        // No "This Mac" heading above them: the machine's own name is the row,
        // and a heading that says less than the line under it is furniture.

        let machine = NSMenuItem(title: Machine.name, action: nil, keyEquivalent: "")
        machine.image = Self.glyph(Machine.symbol, size: 26)
        machine.attributedTitle = Self.twoLine(Machine.name, state.headline)
        // No tooltip. The row already says the two things there are to say, on
        // two lines, without being hovered — a bubble that fades in over them a
        // second later can only repeat it or contradict it.
        machine.submenu = controlsMenu()
        menu.addItem(machine)

        menu.addItem(processesItem())
        // The roster sits beside the process list, not inside the controls
        // submenu: "who is paired" is a fact you read at a glance, the same
        // shape as "what is running", and pairing a new one already lives on
        // the machine's controls where the rest of the do-something rows are.
        if let devices = devicesItem() { menu.addItem(devices) }
        // And the machines this hub took responsibility for, on the same
        // footing: a hub that adopted Macs is administering them, and a menu
        // about this hub that shows its devices but not its machines is a menu
        // that stops just short of what the hub actually is.
        if let machines = machinesItem() { menu.addItem(machines) }
        if updateError == nil,
           updateInventory?.localUpdate(hostID: state.identity?.hostId) != nil {
            let update = Self.action("Update Available", #selector(doUpdate), self,
                                     symbol: "arrow.down.circle")
            update.setAccessibilityIdentifier("updates_available")
            update.representedObject = "self"
            update.isEnabled = !updateRequestInFlight
            menu.addItem(update)
        }
        let info = Self.action("Info", #selector(doInfo), self, symbol: "info.circle")
        info.setAccessibilityIdentifier("host_info")
        menu.addItem(info)

        // ── The app ─────────────────────────────────────────────────────────
        menu.addItem(.separator())
        if RemoteApp.url != nil {
            let remote = Self.action("jRemote", #selector(doOpenApp), self)
            remote.image = JRemoteGlyph.image(size: 13)
            menu.addItem(remote)
        }
        if FileManager.default.fileExists(atPath: HostAgent.logDirectory().path) {
            menu.addItem(Self.action("Open Log Folder", #selector(doLogs), self,
                                     symbol: "folder"))
        }
        // No Refresh. `menuWillOpen` re-polls, so everything below the pointer
        // was fetched on the way to it — a button that re-fetches data a
        // fraction of a second old is a button that can only ever appear to do
        // nothing, and teaches that the numbers need convincing to be true.

        // No Quit by default. This is the hub's indicator, and the hub runs
        // whether or not anyone is looking at it — so "quit" here never meant
        // "stop the hub", it meant "hide the icon", which is not a thing worth
        // a permanent slot in a menu about the hub. Set JREMOTE_MENUBAR_QUIT=1
        // to put it back; `menubar/install.sh --uninstall` removes it for good.
        //
        // An environment variable and not a checkbox: a setting whose whole
        // effect is whether a menu item exists is a menu item about the menu,
        // and it would sit in the same list as the ones about the hub.
        if ProcessInfo.processInfo.environment["JREMOTE_MENUBAR_QUIT"] == "1" {
            menu.addItem(.separator())
            let quit = Self.action("Quit Menu Bar", #selector(doQuit), self)
            quit.toolTip = "Takes this icon off until the next login. "
                + "The host keeps running."
            menu.addItem(quit)
        }

        menu.delegate = self
        item.menu = menu
    }

    private func updateDetails() -> NSMenu {
        let submenu = NSMenu()
        submenu.autoenablesItems = false
        if let error = updateError {
            submenu.addItem(Self.caption(error))
            // The supervisor's journal remains readable through a host restart.
            let path = HostAgent.stateDir().appendingPathComponent("updates/observed.json")
            if let data = try? Data(contentsOf: path),
               let report = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let job = report["job"] as? [String: Any], let state = job["state"] as? String {
                submenu.addItem(Self.caption("Last local report: \(state)"))
                if let detail = job["detail"] as? String, !detail.isEmpty {
                    submenu.addItem(Self.caption(detail))
                }
            }
            return submenu
        }
        guard let inventory = updateInventory else {
            submenu.addItem(Self.caption("Checking…"))
            return submenu
        }
        submenu.addItem(Self.caption(inventory.release.map { "Available release: \($0)" }
                                       ?? "No release available"))
        let localID = state.identity?.hostId
        for machine in inventory.machines {
            submenu.addItem(.separator())
            submenu.addItem(Self.caption("\(machine.name) — \(machine.summary)"))
            if let source = machine.observed?.hostSource, let sha = source.sha, !sha.isEmpty {
                submenu.addItem(Self.caption("Running stack: \(sha.prefix(12))\(source.dirty == true ? " + local changes" : "")"))
            }
            for kind in ["menubar", "client"] {
                if let component = machine.observed?.components?[kind], let installed = component.installed {
                    let state = (component.runningPids ?? []).isEmpty ? "not running" : "running"
                    submenu.addItem(Self.caption("\(kind): \(installed) installed · \(state)"))
                }
            }
            if let contact = machine.lastContact {
                submenu.addItem(Self.caption("Last contact: \(Date(timeIntervalSince1970: contact).formatted())"))
            }
            if let detail = machine.job?.detail, !detail.isEmpty {
                submenu.addItem(Self.caption(detail))
            }
            let canRecoverHere = machine.machine == localID && machine.needsBootstrap
            if machine.canUpdate || canRecoverHere {
                let title = canRecoverHere ? "Enable Updates and Continue" : "Update \(machine.name)"
                let update = Self.action(title, #selector(doUpdate), self)
                update.setAccessibilityIdentifier("updates_tap_" + machine.machine)
                update.representedObject = machine.machine
                update.isEnabled = !updateRequestInFlight
                submenu.addItem(update)
            }
        }
        if inventory.machines.count > 1 && inventory.release != nil {
            submenu.addItem(.separator())
            let all = Self.action("Update All Macs", #selector(doUpdate), self)
            all.setAccessibilityIdentifier("updates_tap_all")
            all.representedObject = "all"
            all.isEnabled = !updateRequestInFlight
            submenu.addItem(all)
        }
        return submenu
    }

    @objc private func doUpdate(_ sender: NSMenuItem) {
        guard !updateRequestInFlight, let target = sender.representedObject as? String else { return }
        updateRequestInFlight = true
        let localID = state.identity?.hostId
        let local = updateInventory?.machines.first { $0.machine == localID }
        if (target == "self" || target == localID), local?.needsBootstrap == true {
            updateActionStatus = "Enabling managed updates…"
            showUpdates()
            guard let binary = HostControl.hostBinary else {
                finishUpdate(false, "The installed jStack host command could not be found.")
                return
            }
            DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                let result = HostControl.run(binary, ["updates", "enable"])
                let detail = result.out.trimmingCharacters(in: .whitespacesAndNewlines)
                DispatchQueue.main.async {
                    guard let self else { return }
                    if result.code == 0 {
                        self.updateActionStatus = "Updater enabled · queueing update…"
                        self.refreshInfoWindow()
                        self.queueUpdate(target)
                    } else {
                        self.finishUpdate(false, detail.isEmpty
                            ? "The update supervisor could not be installed." : detail)
                    }
                }
            }
            return
        }
        showUpdates()
        queueUpdate(target)
    }

    private func queueUpdate(_ target: String) {
        probe.update(target: target, requestID: UUID().uuidString) { [weak self] ok, detail in
            guard let self else { return }
            self.finishUpdate(ok, detail)
        }
    }

    private func finishUpdate(_ ok: Bool, _ detail: String) {
        updateRequestInFlight = false
        updateActionStatus = nil
        if !ok {
            let alert = NSAlert()
            alert.messageText = "Update request needs attention"
            alert.informativeText = detail
            alert.runModal()
        }
        refresh()
    }

    func showUpdates() {
        if infoWindow == nil { infoWindow = HostInfoWindow() }
        refreshInfoWindow()
        NSApp.activate(ignoringOtherApps: true)
        infoWindow?.makeKeyAndOrderFront(nil)
        refresh()
    }

    @objc private func doInfo() { showUpdates() }

    private func refreshInfoWindow() {
        guard let infoWindow else { return }
        let local = updateInventory?.machines.first { $0.machine == state.identity?.hostId }
        let source = state.identity?.source ?? local?.observed?.hostSource
        let details = updateDetails()
        var commands: [String: NSMenuItem] = [:]
        for item in details.items where item.action != nil {
            if let target = item.representedObject as? String { commands[target] = item }
        }
        let localID = state.identity?.hostId ?? ""
        infoWindow.render(HostInfoForm(
            machine: Machine.name,
            status: state.isUp ? (state.identity?.mode?.isManaged == true ? "Running · Managed Mac" : "Running") : "Not running",
            version: source?.displayVersion ?? "Version not reported",
            source: source?.sha,
            app: InfoAppSnapshot.read(),
            updateStatus: updateActionStatus ?? (updateError != nil ? "Could not check for updates"
                : local?.summary ?? (updateInventory == nil ? "Checking…" : "No update published")),
            error: updateError ?? local?.job?.detail,
            localCommand: commands[localID],
            machines: updateInventory?.machines.filter { $0.machine != localID } ?? [],
            commands: commands, allCommand: commands["all"],
            open: { [weak self] in self?.doOpenApp() },
            download: { [weak self] in self?.downloadClient() }))
    }

    private func downloadClient() {
        guard let token = HostAgent.updaterToken(),
              let url = URL(string: "http://127.0.0.1:\(HostAgent.port())\(HostProbe.apiPrefix)/app/mac/download")
        else { return }
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "jRemote.zip"
        panel.begin { [weak self] response in
            guard response == .OK, let destination = panel.url else { return }
            var request = URLRequest(url: url)
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            URLSession.shared.downloadTask(with: request) { temporary, response, error in
                var failure = error?.localizedDescription
                if let temporary, (response as? HTTPURLResponse)?.statusCode == 200 {
                    do {
                        // The save panel owns overwrite confirmation.
                        let data = try Data(contentsOf: temporary)
                        try data.write(to: destination, options: .atomic)
                    } catch { failure = error.localizedDescription }
                } else if failure == nil {
                    failure = "This hub has no downloadable jRemote release. Check its release settings."
                }
                DispatchQueue.main.async {
                    if let failure {
                        let alert = NSAlert()
                        alert.messageText = "Could not download jRemote"
                        alert.informativeText = failure
                        alert.runModal()
                    } else {
                        NSWorkspace.shared.activateFileViewerSelecting([destination])
                    }
                    self?.refresh()
                }
            }.resume()
        }
    }

    /// The Active section. Rows are informational — a menu bar is where you
    /// look to find out, and the thing you would do about it is a terminal
    /// command or the app, neither of which belongs behind a status item.
    /// What opens off the machine's row: the things you can do to the hub.
    ///
    /// Start / Stop only when there is an agent to operate. A hub running in a
    /// terminal is stopped by the terminal it is running in, and a Shut Down
    /// that boots out a job which does not exist is a button that lies.
    private func controlsMenu() -> NSMenu {
        let sub = NSMenu()
        sub.autoenablesItems = false
        if state.installed {
            // `unauthorized` counts as running, and it is the same rule as the
            // one above read the other way round: a refusal is an *answer*, and
            // only something serving the port can produce one. Offering Start
            // for a hub that is already up is the lying button, and it is the
            // worse direction of the two — Shut Down at least fails loudly,
            // while Start quietly does nothing to a job launchd already has
            // loaded, leaving the stale credential that caused the refusal
            // untouched and unmentioned.
            if state.isUp || state.unauthorized {
                sub.addItem(Self.action("Restart Hub", #selector(doRestart), self,
                                        symbol: "arrow.clockwise"))
                sub.addItem(Self.action("Shut Down Hub", #selector(doStop), self,
                                        symbol: "power"))
            } else {
                sub.addItem(Self.action("Start Hub", #selector(doStart), self,
                                        symbol: "power"))
            }
        }
        if state.isUp, state.identity?.canManageDevices == true, HostControl.hostBinary != nil {
            sub.addItem(Self.action("Pair a Device…", #selector(doPair), self,
                                    symbol: "plus.circle"))
            // Adopt and Detach are the two directions of the same relationship,
            // and a machine is only ever in one of them: a managed Mac rides
            // another hub's mesh and has no peers of its own to hand out, so
            // offering it Adopt is offering a button whose only outcome is the
            // refusal underneath it.
            //
            // The gate is the mode and NOT `features.tunnel_pairing`. That flag
            // is `can_pair()`, which answers whether *this process* can find the
            // peer table — false on a hub whose mesh predates the package, which
            // is exactly the machine that most needs this item (#42). A mode of
            // `local` or `open` is a hub either way; where the peer table is
            // genuinely unreachable the CLI says so in its own words, which are
            // better than anything this menu could guess.
            if state.identity?.mode?.isManaged == true {
                let parent = state.identity?.mode?.parentHost
                let detach = Self.action(
                    parent.map { "Detach from \($0)…" } ?? "Detach from Parent Hub…",
                    #selector(doDetach), self, symbol: "eject")
                detach.toolTip = "Stop being administered from "
                    + "\(state.identity?.mode?.parent ?? "the parent hub")"
                    + " and leave its mesh."
                sub.addItem(detach)
            } else {
                let adopt = Self.action("Adopt a Mac…", #selector(doAdopt), self,
                                        symbol: "plus.rectangle.on.rectangle")
                adopt.toolTip = "Join another Mac to this hub's mesh. Every "
                    + "device already paired here gets into it without a "
                    + "second code."
                sub.addItem(adopt)
            }
        }
        if sub.items.isEmpty {
            sub.addItem(Self.caption("No agent to operate this hub."))
        }

        // ── Login ───────────────────────────────────────────────────────────
        //
        // Here and not behind a Settings row of its own. Whether the hub comes
        // up at login is a fact about this machine's hub, which is what this
        // submenu already is — a Settings item next to it would be a second
        // door onto the same room.
        sub.addItem(.separator())
        if HostAgent.isInstalled {
            let hub = Self.check("Start Hub at Login",
                                 on: LoginAgent.startsAtLogin(HostAgent.label),
                                 #selector(doToggleHubLogin), self)
            if LoginAgent.pinnedOn(HostAgent.label) {
                hub.action = nil
                hub.isEnabled = false
                hub.toolTip = "Always on: this agent is set to be kept running, "
                    + "so launchd starts it at login whatever this says. "
                    + "Change it where the agent is installed from."
            } else {
                hub.toolTip = "Nothing to grant — a user LaunchAgent loads at "
                    + "login on its own. Takes effect at the next login."
            }
            sub.addItem(hub)
        }
        if LoginAgent.exists(MenuBarAgent.label) {
            let bar = Self.check("Start Menu Bar at Login",
                                 on: LoginAgent.startsAtLogin(MenuBarAgent.label),
                                 #selector(doToggleBarLogin), self)
            bar.toolTip = "Off means the icon is gone until you launch the app "
                + "again. The hub is unaffected either way."
            sub.addItem(bar)
        }

        // No agent label, state dir or token path on the menu. They are the
        // answer to "why is this menu wrong", which is a question asked while
        // something is broken and never while it works — and a permanent row
        // that cannot be clicked reads as a control that died, not as a fact.
        // Copy Diagnostics carries all three, untruncated, to the place they
        // are actually usable.
        sub.addItem(.separator())
        let copy = Self.action("Copy Diagnostics", #selector(doCopyDiagnostics), self,
                               symbol: "doc.on.clipboard")
        copy.toolTip = "Everything on this submenu, plus what the host answered, "
            + "as text. No token value is copied."
        sub.addItem(copy)

        return sub
    }

    /// A checkbox row. `.on`/`.off` rather than a tick drawn into the title, so
    /// it reads as a setting to the system and to VoiceOver both.
    private static func check(_ title: String, on: Bool,
                              _ selector: Selector,
                              _ target: AnyObject) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: selector, keyEquivalent: "")
        item.target = target
        item.isEnabled = true
        item.state = on ? .on : .off
        return item
    }

    /// The processes row: a count you read at the top level, a list you open.
    private func processesItem() -> NSMenuItem {
        let sub = NSMenu()
        sub.autoenablesItems = false

        // Refusal is tested *before* liveness, and the order is the whole point.
        // `isUp` is false whenever the host would not answer us — including when
        // it answered 401 — so an `isUp` guard placed first returns "Hub is not
        // running" over a hub that is running and simply will not take our
        // token, and the branch below that says so exactly can never be reached.
        // That is what the menu did on this Mac: two rows, both claiming a dead
        // hub, over a live one holding an adopted leaf.
        if state.unauthorized {
            sub.addItem(Self.caption("The token on disk was refused by the hub."))
            sub.addItem(Self.caption("The hub is running — this Mac's own"))
            sub.addItem(Self.caption("credential is stale, not the service."))
            sub.addItem(Self.caption("Run: jstack-host status"))
            return Self.opener("No Access", symbol: "lock", submenu: sub)
        }
        guard state.isUp else {
            let item = NSMenuItem(title: "Hub is not running", action: nil, keyEquivalent: "")
            item.image = Self.glyph("bolt.horizontal.circle", size: 14)
            item.isEnabled = false
            return item
        }
        if !state.isProvisioned {
            sub.addItem(Self.caption("No token, so every request is refused."))
            sub.addItem(Self.caption("Run: jstack-host status"))
            return Self.opener("No Access", symbol: "lock", submenu: sub)
        }

        let sorted = state.sessions.sorted {
            (-$0.signal.rawValue, $0.title) < (-$1.signal.rawValue, $1.title)
        }
        guard !sorted.isEmpty else {
            let item = NSMenuItem(title: "Nothing running", action: nil, keyEquivalent: "")
            item.image = Self.glyph("moon.zzz", size: 14)
            item.isEnabled = false
            return item
        }
        for session in sorted {
            let emoji = (session.emoji ?? "").isEmpty ? "" : "\(session.emoji!) "
            let row = NSMenuItem(title: "\(emoji)\(session.title)",
                                 action: nil, keyEquivalent: "")
            // The dot is the state, and it is an icon rather than a character
            // in the title so every row's text starts at the same x — a list
            // whose left edge moves with the status is one you cannot scan.
            row.image = session.signal.image

            // Kill hangs off the process rather than sitting beside its name:
            // a one-click kill in a list you are scrolling is a session ended
            // by the mouse passing over it.
            let actions = NSMenu()
            actions.autoenablesItems = false
            let kill = Self.action("Kill", #selector(doKill), self, symbol: "xmark.circle")
            kill.representedObject = session
            actions.addItem(kill)
            row.submenu = actions
            sub.addItem(row)
        }
        // Kill All last and behind a separator, so the pointer travelling down
        // the list of sessions does not arrive on it.
        sub.addItem(.separator())
        let killAll = Self.action("Kill All", #selector(doKillAll), self,
                                  symbol: "xmark.octagon")
        killAll.representedObject = sorted
        sub.addItem(killAll)

        // Same shape as the machine's row: a count, and the split underneath.
        //
        // The two states are what you opened the menu to tell apart — a
        // machine with five sessions all idle and one with five mid-turn are
        // not the same machine, and a single total says the same thing about
        // both. They partition the list rather than nest: working is a subset
        // of running everywhere else in this API, and counting it twice here
        // would leave the numbers refusing to add up to the rows beneath them.
        let working = sorted.filter { $0.signal == .working }.count
        let idle = sorted.count - working
        let subtitle: String
        switch (working, idle) {
        case (0, _): subtitle = "\(idle) running"
        case (_, 0): subtitle = "\(working) working"
        default:     subtitle = "\(working) working, \(idle) running"
        }
        let title = "\(sorted.count) "
            + (sorted.count == 1 ? "Process" : "Processes")
        let item = Self.opener(title, symbol: "person.2", submenu: sub)
        item.attributedTitle = Self.twoLine(title, subtitle)
        item.image = Self.glyph("person.2", size: 26)
        return item
    }

    /// The paired-devices row: a count read at the top level, the roster opened
    /// off it. Only where the host answered a token, since the list lives behind
    /// it — and only when something is actually paired, because a row that can
    /// only ever say "nobody" is furniture, and pairing a first one already
    /// lives on the machine's controls.
    private func devicesItem() -> NSMenuItem? {
        guard state.isUp, state.isProvisioned, !state.unauthorized,
              state.identity?.canManageDevices == true else { return nil }
        let active = DeviceMenu.active(state.devices, removed: removedDevices)
        guard !active.isEmpty else { return nil }

        let sub = NSMenu()
        sub.autoenablesItems = false

        for device in active {
            let row = NSMenuItem(title: device.name, action: nil, keyEquivalent: "")
            // Filled for the device this menu is signed in as, hollow for the
            // rest — the same dot the process list uses for live/idle, so one
            // glyph vocabulary covers the whole menu.
            row.image = Self.dot(live: device.current, idle: true)
            let detail = device.current ? "this device" : Self.seen(device.lastSeenAt)
            row.attributedTitle = Self.twoLine(device.name, detail)

            // Remove hangs off the device rather than beside its name, for the
            // reason Kill does: a one-click revoke in a list the pointer travels
            // is access cut by a mouse passing over it.
            let actions = NSMenu()
            actions.autoenablesItems = false
            let remove = Self.action("Remove", #selector(doRemoveDevice), self,
                                     symbol: "minus.circle")
            remove.representedObject = device
            actions.addItem(remove)
            row.submenu = actions
            sub.addItem(row)
        }

        let title = "\(active.count) " + (active.count == 1 ? "Device" : "Devices")
        let item = Self.opener(title, symbol: "laptopcomputer.and.iphone", submenu: sub)
        return item
    }

    /// The adopted-machines row: the Macs this hub joined to its own mesh, and
    /// whether a device paired here actually gets into each one.
    ///
    /// Hidden entirely on a hub that adopted nobody, which is most of them —
    /// the same rule the roster follows, and for the same reason: a permanent
    /// row that can only ever say "none" is furniture, and adopting a first one
    /// lives on the machine's controls with the other verbs.
    ///
    /// The second line is the access, not the address, because the access is
    /// the thing that is ever wrong. A machine adopted by a build that predates
    /// delegated minting sits in this list looking identical to a working one
    /// and refuses the moment a device asks — so the row says which it is,
    /// out loud, and the fix is one item away.
    private func machinesItem() -> NSMenuItem? {
        guard state.isUp, state.isProvisioned, !state.unauthorized,
              state.identity?.canManageDevices == true else { return nil }
        let machines = state.leaves.sorted { $0.title < $1.title }
        guard !machines.isEmpty else { return nil }

        let sub = NSMenu()
        sub.autoenablesItems = false

        for machine in machines {
            let row = NSMenuItem(title: machine.title, action: nil, keyEquivalent: "")
            // Filled where a device here can be let in without pairing to that
            // machine, hollow where it cannot — the same two dots the process
            // and device lists use, carrying the one difference that matters.
            // Neither where the host never answered the question: an empty
            // column is the only honest mark for an answer nobody gave.
            row.image = Self.dot(live: machine.delegated == true,
                                 idle: machine.delegated != nil)
            row.attributedTitle = Self.twoLine(machine.title, Self.access(machine))

            let actions = NSMenu()
            actions.autoenablesItems = false
            let home = Self.check("Sees Home Instance", on: machine.seesHome ?? true,
                                  #selector(doToggleLeafVisibility), self)
            home.tag = 0
            home.setAccessibilityIdentifier("leaf_toggle_home_\(machine.key)")
            home.representedObject = machine
            actions.addItem(home)
            let siblings = Self.check("Sees Other Leaf Instances", on: machine.seesLeaves ?? true,
                                      #selector(doToggleLeafVisibility), self)
            siblings.tag = 1
            siblings.setAccessibilityIdentifier("leaf_toggle_siblings_\(machine.key)")
            siblings.representedObject = machine
            actions.addItem(siblings)
            actions.addItem(.separator())
            let forget = Self.action("Forget", #selector(doForgetMachine), self,
                                     symbol: "minus.circle")
            forget.representedObject = machine
            actions.addItem(forget)
            row.submenu = actions
            sub.addItem(row)
        }

        let title = "\(machines.count) "
            + (machines.count == 1 ? "Managed Mac" : "Managed Macs")
        let item = Self.opener(title, symbol: "point.3.connected.trianglepath.dotted", submenu: sub)
        return item
    }

    /// One adopted machine's second line: where it is, and whether a device
    /// paired to this hub can actually reach it.
    private static func access(_ machine: AdoptedHost) -> String {
        let route = machine.route
        switch machine.delegated {
        case true:  return route.isEmpty ? "no address on the mesh" : route
        case false:
            return route.isEmpty
                ? "no address — re-adopt this machine"
                : "\(route) · pair by hand"
        // The host did not answer the field: too old to know, so the row says
        // where the machine is and claims nothing about getting into it.
        case nil:   return route.isEmpty ? "no address on the mesh" : route
        }
    }

    /// "last seen 3h ago", or that it has never connected. Unix seconds in —
    /// what the host sends — and a device that has authenticated once always
    /// carries a stamp.
    private static func seen(_ ts: Int?) -> String {
        guard let ts, ts > 0 else { return "never connected" }
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .abbreviated
        let when = Date(timeIntervalSince1970: TimeInterval(ts))
        return "last seen " + formatter.localizedString(for: when, relativeTo: Date())
    }

    /// The header row's two lines: what the machine is, and what it is doing.
    ///
    /// An attributed title rather than a custom view. A menu item with a view
    /// stops being a menu item — it loses the system's highlight, its
    /// keyboard handling and its submenu triangle, all of which would then
    /// have to be redrawn by hand and would still be slightly wrong. Two
    /// paragraphs and a taller image get the same result and stay native.
    private static func twoLine(_ title: String, _ subtitle: String) -> NSAttributedString {
        let paragraph = NSMutableParagraphStyle()
        paragraph.lineSpacing = 1
        let out = NSMutableAttributedString(string: title, attributes: [
            .font: NSFont.systemFont(ofSize: NSFont.systemFontSize, weight: .semibold),
            .foregroundColor: NSColor.labelColor,
            .paragraphStyle: paragraph,
        ])
        guard !subtitle.isEmpty else { return out }
        out.append(NSAttributedString(string: "\n" + subtitle, attributes: [
            .font: NSFont.systemFont(ofSize: NSFont.smallSystemFontSize),
            .foregroundColor: NSColor.secondaryLabelColor,
            .paragraphStyle: paragraph,
        ]))
        return out
    }

    /// A row whose job is to open something.
    private static func opener(_ title: String, symbol: String,
                               submenu: NSMenu) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.image = glyph(symbol, size: 14)
        item.submenu = submenu
        return item
    }

    /// A filled dot for a session that is producing, a hollow one for a session
    /// that is up but quiet, nothing for the rest.
    private static func dot(live: Bool, idle: Bool) -> NSImage? {
        guard live || idle else { return blank(width: 10) }
        let name = live ? "circle.fill" : "circle"
        let config = NSImage.SymbolConfiguration(pointSize: 7, weight: .semibold)
        let image = NSImage(systemSymbolName: name, accessibilityDescription:
                                live ? "running" : "idle")?
            .withSymbolConfiguration(config)
        image?.isTemplate = true
        return image ?? blank(width: 10)
    }

    /// Occupies the icon column so a row with no dot still lines up with one
    /// that has.
    private static func blank(width: CGFloat) -> NSImage {
        let image = NSImage(size: NSSize(width: width, height: 1))
        image.isTemplate = true
        return image
    }

    private static func glyph(_ symbol: String, size: CGFloat) -> NSImage? {
        let config = NSImage.SymbolConfiguration(pointSize: size, weight: .regular)
        let image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)
            ?? NSImage(systemSymbolName: "desktopcomputer", accessibilityDescription: nil)
        let out = image?.withSymbolConfiguration(config)
        out?.isTemplate = true
        return out
    }

    // MARK: Items

    /// A row that says something rather than does something.
    ///
    /// Disabled so it cannot be clicked, but drawn with an explicit color:
    /// AppKit greys a disabled item to the point of looking broken, and these
    /// are the content of the menu, not a dead option in it.
    private static func caption(_ text: String, dim: Bool = true) -> NSMenuItem {
        let item = NSMenuItem(title: text, action: nil, keyEquivalent: "")
        item.isEnabled = false
        item.attributedTitle = NSAttributedString(string: text, attributes: [
            .font: NSFont.menuFont(ofSize: NSFont.systemFontSize),
            .foregroundColor: dim ? NSColor.secondaryLabelColor : NSColor.labelColor,
        ])
        return item
    }

    private static func action(_ title: String, _ selector: Selector,
                               _ target: AnyObject,
                               symbol: String? = nil,
                               indent: Int = 0) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: selector, keyEquivalent: "")
        item.target = target
        item.isEnabled = true
        item.indentationLevel = indent
        if let symbol { item.image = glyph(symbol, size: 13) }
        return item
    }

    // MARK: Actions

    @objc private func doRestart() {
        HostControl.restart()
        after(1.5) { self.refresh() }
    }

    @objc private func doStop() {
        HostControl.stop()
        after(1.0) { self.refresh() }
    }

    @objc private func doStart() {
        HostControl.start()
        after(2.0) { self.refresh() }
    }

    /// Kill a session, after asking.
    ///
    /// A confirmation because this is not undoable and the menu is a place the
    /// pointer passes through: the transcript survives, but the turn in flight
    /// does not, and "which one was highlighted" is not a question to answer
    /// after the fact.
    @objc private func doKill(_ sender: NSMenuItem) {
        guard let session = sender.representedObject as? Session,
              let sid = session.sessionId, !sid.isEmpty,
              let token = HostAgent.token() else { return }

        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Kill \(session.title)?"
        alert.informativeText = "It stops mid-turn — no clean exit, no review. "
            + "The transcript is kept, so it can be resumed later."
        alert.addButton(withTitle: "Kill")
        alert.addButton(withTitle: "Cancel")
        NSApp.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        probe.kill(sid: sid, token: token) { [weak self] ok, detail in
            if !ok {
                let failed = NSAlert()
                failed.alertStyle = .warning
                failed.messageText = "Could not kill \(session.title)"
                failed.informativeText = detail
                failed.runModal()
            }
            self?.refresh()
        }
    }

    /// Kill everything on the list.
    ///
    /// Spelled out in the confirmation, count and all, because this is the one
    /// control in the menu that can end work you were not thinking about —
    /// including the session you are reading this from.
    @objc private func doKillAll(_ sender: NSMenuItem) {
        guard let sessions = sender.representedObject as? [Session],
              !sessions.isEmpty, let token = HostAgent.token() else { return }
        let sids = sessions.compactMap { $0.sessionId }.filter { !$0.isEmpty }
        guard !sids.isEmpty else { return }

        let alert = NSAlert()
        alert.alertStyle = .critical
        alert.messageText = "Kill all \(sids.count) processes?"
        alert.informativeText = "Every one stops mid-turn — no clean exit, no "
            + "review. This includes any session you are currently talking to. "
            + "Transcripts are kept, so they can be resumed later."
        alert.addButton(withTitle: "Kill All")
        alert.addButton(withTitle: "Cancel")
        NSApp.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        let group = DispatchGroup()
        var failures: [String] = []
        for sid in sids {
            group.enter()
            probe.kill(sid: sid, token: token) { ok, detail in
                if !ok { failures.append("\(sid.prefix(8)): \(detail)") }
                group.leave()
            }
        }
        group.notify(queue: .main) { [weak self] in
            if !failures.isEmpty {
                let failed = NSAlert()
                failed.alertStyle = .warning
                failed.messageText = "\(failures.count) of \(sids.count) did not stop"
                failed.informativeText = failures.joined(separator: "\n")
                failed.runModal()
            }
            self?.refresh()
        }
    }

    /// Remove a device, after asking. It revokes the token, which is not
    /// undoable — the device has to be paired again to come back — so the same
    /// confirmation Kill gets applies here. The warning sharpens for the row
    /// this menu is signed in as: removing it cuts this Mac's own access.
    @objc private func doRemoveDevice(_ sender: NSMenuItem) {
        guard state.identity?.canManageDevices == true,
              let device = sender.representedObject as? Device,
              !device.id.isEmpty, let token = HostAgent.token() else { return }
        guard DeviceMenu.active(state.devices, removed: removedDevices).contains(where: { $0.id == device.id }) else {
            build()
            refresh()
            return
        }

        let alert = NSAlert()
        alert.alertStyle = device.current ? .critical : .warning
        alert.messageText = "Remove \(device.name)?"
        alert.informativeText = device.current
            ? "This is the device this menu is signed in as. Removing it cuts "
            + "this Mac's own access to the hub until it is paired again. The "
            + "token stops working immediately and any live connection is dropped."
            : "The token stops working immediately and any live connection from "
            + "it is dropped. Pair the device again to restore access."
        alert.addButton(withTitle: "Remove")
        alert.addButton(withTitle: "Cancel")
        NSApp.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        probe.revoke(deviceId: device.id, token: token) { [weak self] ok, detail in
            if ok, let self {
                self.removedDevices.insert(device.id)
                self.state.devices.removeAll { $0.id == device.id }
                self.build()
            } else if !ok {
                let failed = NSAlert()
                failed.alertStyle = .warning
                failed.messageText = "Could not remove \(device.name)"
                failed.informativeText = detail
                failed.runModal()
            }
            self?.refresh()
        }
    }

    @objc private func doToggleLeafVisibility(_ sender: NSMenuItem) {
        guard state.identity?.canManageDevices == true,
              let machine = sender.representedObject as? AdoptedHost,
              let token = HostAgent.token() else { return }
        let home = machine.seesHome ?? true
        let siblings = machine.seesLeaves ?? true
        probe.visibility(hostKey: machine.key, seesHome: sender.tag == 0 ? !home : home,
                         seesLeaves: sender.tag == 1 ? !siblings : siblings, token: token) {
            [weak self] ok, detail in
            if !ok {
                let alert = NSAlert()
                alert.messageText = "Could Not Update Leaf Visibility"
                alert.informativeText = detail
                alert.runModal()
            }
            self?.refresh()
        }
    }

    /// Opens the client app, or brings it forward if it is already running.
    ///
    /// Activation rather than a second copy: two instances of a client that
    /// each hold their own connection to a hub is a way to be told two
    /// different things about one machine. Activation alone only raises
    /// whatever window was already frontmost, though — `BoardRaise` is what
    /// gets the board itself in front of a thread window left on top.
    @objc private func doOpenApp() {
        guard let url = RemoteApp.url else { return }
        NSWorkspace.shared.openApplication(at: url,
                                           configuration: NSWorkspace.OpenConfiguration())
        BoardRaise.send()
    }

    @objc private func doLogs() {
        NSWorkspace.shared.open(HostAgent.logDirectory())
    }

    @objc private func doQuit() { NSApp.terminate(nil) }

    /// Mint an enrolment code and show it. A code rather than the raw token:
    /// it expires, it names the device before the device connects, and revoking
    /// it later does not re-key everything else on the host.
    @objc private func doPair() {
        guard let binary = HostControl.hostBinary else { return }
        NSApp.activate(ignoringOtherApps: true)

        let ask = NSAlert()
        ask.messageText = "Pair a device"
        ask.informativeText = "What should this host call it?"
        ask.addButton(withTitle: "Get a Code")
        ask.addButton(withTitle: "Cancel")
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 240, height: 24))
        field.stringValue = "My device"
        ask.accessoryView = field
        ask.window.initialFirstResponder = field
        guard ask.runModal() == .alertFirstButtonReturn else { return }

        let name = field.stringValue.trimmingCharacters(in: .whitespaces)
        let result = HostControl.run(binary,
                                     ["pair", name.isEmpty ? "My device" : name, "--json"])

        guard result.code == 0, let minted = MintedPairing(json: result.out) else {
            // Nothing to draw — say what the host said, verbatim. This is also
            // the path for a host binary too old to answer `--json`.
            let failed = NSAlert()
            failed.alertStyle = .warning
            failed.messageText = "Could not mint a code"
            failed.informativeText = result.out.trimmingCharacters(in: .whitespacesAndNewlines)
            failed.runModal()
            return
        }

        // The address depends on ONE fact, and it is a fact the person holding
        // the device knows and this hub cannot: does that device already have
        // the tunnel?
        //
        // This showed `firstAddress` — the LAN one — as though that were the
        // answer. It is the answer for exactly one case: a device being set up
        // for the first time, standing on this network. Every device that has
        // been paired once holds a WireGuard conf routing 10.66.0.0/24, and
        // from that moment the mesh address is the one that works, from
        // anywhere in the world — which is the steady state of every device
        // here and the whole reason the mesh exists. Handing those a LAN
        // address that only resolves inside this building is how a device that
        // could have connected from an office got a timeout instead.
        //
        // So: both, each under the condition that picks it, mesh first because
        // it is the common case after day one. Never `.local` — it needs the
        // same LAN as the numeric address and resolves less reliably on it, so
        // it is never the right answer and never the only one.
        let byHand: String
        if let mesh = minted.meshAddress, let lan = minted.lanAddress {
            byHand = "By hand instead — in the app, Instances › Add a Mac:"
                + "\n\n    \(mesh)\n    if that device already has the tunnel "
                + "(anywhere in the world)"
                + "\n\n    \(lan)\n    first time on it, while it is on this "
                + "network"
                + "\n\nThen the code. Good for \(minted.validFor)."
        } else if let only = minted.meshAddress ?? minted.lanAddress {
            byHand = "By hand instead: in the app, Instances › Add a Mac — "
                + "address \(only), then the code. Good for "
                + "\(minted.validFor)."
        } else {
            byHand = ""
        }
        let shown = NSAlert()
        shown.messageText = "Pair \(minted.name)"
        shown.informativeText = minted.link != nil && !byHand.isEmpty
            ? "Point that device's camera at the code — it opens the app and "
            + "connects on its own.\n\n" + byHand
            : byHand.isEmpty
            ? "This Mac could not work out an address a second device can "
            + "reach — connect both machines to the same network and mint a "
            + "new code. This one is good for \(minted.validFor)."
            : byHand
        shown.accessoryView = pairAccessory(minted)
        shown.addButton(withTitle: "Done")
        shown.addButton(withTitle: "Copy Code")
        if shown.runModal() == .alertSecondButtonReturn {
            // The code alone, not the whole message — what gets pasted into the
            // app is the code, and a paste that carries the explanation with it
            // is a paste that fails.
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(minted.code, forType: .string)
        }
    }

    /// The QR above the code it encodes. The code is drawn even though it is
    /// inside the QR: the fallback for a device with no camera to point is
    /// typing, and typing needs something legible to type.
    private func pairAccessory(_ minted: MintedPairing) -> NSView {
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 10

        if let link = minted.link, let qr = Self.qrImage(link, side: 180) {
            let image = NSImageView(image: qr)
            image.imageScaling = .scaleNone
            stack.addArrangedSubview(image)
        }

        let code = NSTextField(labelWithString: minted.code)
        code.font = .monospacedSystemFont(ofSize: 22, weight: .semibold)
        code.isSelectable = true
        stack.addArrangedSubview(code)

        stack.frame = NSRect(x: 0, y: 0, width: 260,
                             height: stack.fittingSize.height)
        return stack
    }

    /// `string` as a QR the size a dialog wants. Nearest-neighbour scaling by
    /// transform, not by resize — a QR with soft edges is a QR a phone camera
    /// hunts on.
    static func qrImage(_ string: String, side: CGFloat) -> NSImage? {
        guard let data = string.data(using: .ascii),
              let filter = CIFilter(name: "CIQRCodeGenerator") else { return nil }
        filter.setValue(data, forKey: "inputMessage")
        filter.setValue("M", forKey: "inputCorrectionLevel")
        guard let output = filter.outputImage else { return nil }
        let scale = (side / output.extent.width).rounded(.down)
        let scaled = output.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
        let rep = NSCIImageRep(ciImage: scaled)
        let image = NSImage(size: rep.size)
        image.addRepresentation(rep)
        return image
    }

    // MARK: Managed hubs

    /// Mint a host code and show the command that redeems it.
    ///
    /// A code for a *machine*, which is not the same kind as a device's and is
    /// refused outright where the two are crossed — `attach` given a device
    /// code would be handed a client conf where a leaf bundle was needed. So
    /// this is its own item rather than a checkbox on Pair a Device.
    ///
    /// And a command rather than the QR the pairing dialog earns: the far end
    /// of an adoption is a terminal on another Mac, so the thing to hand over
    /// is the line to run there. A QR would be a prettier form of the wrong
    /// instruction.
    @objc private func doAdopt() {
        guard let binary = HostControl.hostBinary else { return }
        NSApp.activate(ignoringOtherApps: true)

        // Asked before the panel is drawn, and that is the whole point. The
        // carried file is wrong for a Mac already on this mesh, and the moment
        // to act on that is while the route is still being *offered* — an
        // alert cannot un-offer a button somebody has already pressed, and by
        // then `adopt --offline` has rewritten that machine's bundle.
        let roster = MeshRoster(json: HostControl.run(binary, ["leaves", "--json"]).out)
            ?? .empty

        let ask = NSAlert()
        ask.messageText = "Adopt a Mac"
        ask.informativeText = "What should this hub call it?"
        ask.addButton(withTitle: "Get a Code")
        // The second way in, and the only one that works for a Mac off this
        // network. A code is redeemed over HTTP, and a hub publishes none
        // publicly — so a machine that has never held the tunnel has no
        // address to redeem against and "get a code" cannot help it. That case
        // gets a file instead: the tunnel travels to the machine.
        ask.addButton(withTitle: "Save a Joiner File…")
        ask.addButton(withTitle: "Cancel")
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
        field.stringValue = "New Mac"
        let note = NSTextField(wrappingLabelWithString: Self.onMeshNote)
        note.font = .systemFont(ofSize: 11)
        note.textColor = .secondaryLabelColor
        note.preferredMaxLayoutWidth = 280
        let stack = NSStackView(views: [field, note])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8
        // Measured with the note in place and emptied afterwards: the panel is
        // laid out once, so a frame sized around a blank label would clip the
        // sentence the moment a typed name earns it.
        stack.frame = NSRect(x: 0, y: 0, width: 280,
                             height: stack.fittingSize.height)
        note.stringValue = ""
        ask.accessoryView = stack
        ask.window.initialFirstResponder = field

        let offline = ask.buttons[1]
        let steer = { (typed: String) in
            let onMesh = roster.holds(typed)
            offline.isEnabled = !onMesh
            note.stringValue = onMesh ? Self.onMeshNote : ""
        }
        let watcher = FieldWatcher(steer)
        field.delegate = watcher
        steer(field.stringValue)
        let choice = withExtendedLifetime(watcher) { ask.runModal() }
        guard choice == .alertFirstButtonReturn
                || choice == .alertSecondButtonReturn else { return }

        let name = field.stringValue.trimmingCharacters(in: .whitespaces)
        if choice == .alertSecondButtonReturn {
            doAdoptOffline(binary, name.isEmpty ? "New Mac" : name)
            return
        }
        let result = HostControl.run(
            binary, ["adopt", name.isEmpty ? "New Mac" : name, "--json"])

        guard result.code == 0, let minted = MintedPairing(json: result.out) else {
            // Verbatim, and this is the path that matters most here: `adopt`
            // refuses on a Mac that cannot mint a mesh peer, and names the
            // directory it looked in. Paraphrasing that would throw away the
            // one thing in the message that says what to fix.
            let failed = NSAlert()
            failed.alertStyle = .warning
            failed.messageText = "Could not mint a code"
            failed.informativeText = result.out
                .trimmingCharacters(in: .whitespacesAndNewlines)
            failed.runModal()
            return
        }

        let shown = NSAlert()
        shown.messageText = "Adopt \(minted.name)"
        // Two conditions, because there are two, and the reader knows which
        // one they are in. The dialog used to state one flat precondition —
        // "that Mac has to be on this network" — which is false for every
        // machine that already holds the tunnel, i.e. the common case, and it
        // sent exactly the person it was written for to an address that could
        // not answer them. See `attachCommand` for why the mesh branch is real.
        if minted.meshAddress != nil {
            shown.informativeText =
                "Run this on that Mac, where jStack is installed. The code is "
                + "good for \(minted.validFor).\n\n"
                + "That address is this hub on the mesh — it reaches here from "
                + "anywhere in the world, and it is the answer for any Mac "
                + "that has been on this mesh even once."
                + (minted.lanAddress != nil
                   ? "\n\nOnly a Mac that has NEVER been on this mesh needs a "
                   + "different address, and it has to be on this network for "
                   + "it — `jstack-host where` prints that one."
                   : "")
        } else if minted.lanAddress != nil {
            shown.informativeText =
                "Run this on that Mac, where jStack is installed. It has to be "
                + "on this network — this hub runs no mesh, so there is no "
                + "address that reaches it from anywhere else. The code is "
                + "good for \(minted.validFor)."
        } else {
            // Nothing to run, so nothing is drawn to run. `adopt` refuses this
            // case outright now — before it mints, so there is no live code
            // burning down behind this panel — and a hub too old to refuse it
            // reaches here instead, where the answer is the same: say what is
            // missing, and offer no command at all rather than one with a hole
            // where the address goes.
            shown.informativeText =
                "This Mac has no address another machine can redeem against — "
                + "only loopback, and it runs no mesh. Put it on a real "
                + "network, then mint a new code."
        }
        if let command = minted.attachCommand {
            shown.accessoryView = Self.commandAccessory(command)
        }
        shown.addButton(withTitle: "Done")
        if minted.attachCommand != nil {
            shown.addButton(withTitle: "Copy Command")
        }
        if shown.runModal() == .alertSecondButtonReturn,
           let command = minted.attachCommand {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(command, forType: .string)
        }
        refresh()
    }

    /// Why the carried file is greyed out, and what to press instead.
    ///
    /// Both halves matter. "Already on this mesh" alone leaves the operator
    /// working out for themselves that the other button applies to them, and a
    /// disabled control that does not say what to do instead is a dead end
    /// wearing the clothes of a refusal.
    private static let onMeshNote =
        "That Mac is already on this mesh — it answers this hub over the "
        + "tunnel, so it can redeem a code from where it stands. Use Get a "
        + "Code. The joiner file is for a Mac that cannot reach here at all."

    /// Adopt a Mac this hub cannot reach — by handing over a file, not a code.
    ///
    /// A code is redeemed over HTTP, and a hub publishes no public HTTP. So a
    /// Mac that is off this network and has never held the tunnel has no
    /// address to redeem against: joining needs the tunnel, and the tunnel is
    /// what joining was supposed to install. "Get a Code" cannot reach that
    /// machine however the dialog is worded.
    ///
    /// `adopt --offline` writes one executable file carrying the tunnel keys,
    /// the bringup scripts and the code. It is saved wherever the person says
    /// — a USB stick, a folder they are about to AirDrop — because the whole
    /// premise is that they are carrying it themselves.
    @objc private func doAdoptOffline(_ binary: String, _ name: String) {
        let result = HostControl.run(binary, ["adopt", name, "--offline", "--json"])
        guard result.code == 0,
              let data = result.out.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let made = obj["file"] as? String else {
            let failed = NSAlert()
            failed.alertStyle = .warning
            failed.messageText = "Could not make a joiner file"
            // Verbatim: `adopt` names the directory it looked in when a Mac
            // cannot mint a mesh peer, and that name is the whole repair.
            failed.informativeText = result.out
                .trimmingCharacters(in: .whitespacesAndNewlines)
            failed.runModal()
            return
        }
        let mins = ((obj["expires_in"] as? Int) ?? 600) / 60

        let save = NSSavePanel()
        save.title = "Save Joiner File"
        save.nameFieldStringValue = URL(fileURLWithPath: made).lastPathComponent
        save.message = "Carry this to \(name) and run it there. It carries that "
            + "machine's private key — delete it once the join succeeds."
        guard save.runModal() == .OK, let dest = save.url else { return }

        do {
            if FileManager.default.fileExists(atPath: dest.path) {
                try FileManager.default.removeItem(at: dest)
            }
            try FileManager.default.copyItem(at: URL(fileURLWithPath: made), to: dest)
            // Copied, not moved: the source lives in the hub's credentials
            // directory beside the peer it belongs to, and a save panel
            // pointed at a USB stick should not be what removes it from there.
            try FileManager.default.setAttributes([.posixPermissions: 0o700],
                                                  ofItemAtPath: dest.path)
        } catch {
            let failed = NSAlert()
            failed.alertStyle = .warning
            failed.messageText = "Could not save the joiner file"
            failed.informativeText = error.localizedDescription
            failed.runModal()
            return
        }

        let done = NSAlert()
        done.messageText = "Joiner file for \(name)"
        done.informativeText =
            "Run it on that Mac. Everything is inside it — the tunnel keys, the "
            + "installer and the code — so nothing is typed and nothing is "
            + "downloaded.\n\nThe code inside is good for \(mins) minutes. If you "
            + "get there after it expires the trip is not wasted: the tunnel is "
            + "the permanent half, and once it is up that Mac can redeem a fresh "
            + "code by itself. The file says so if it happens."
        done.addButton(withTitle: "Done")
        done.addButton(withTitle: "Show in Finder")
        if done.runModal() == .alertSecondButtonReturn {
            NSWorkspace.shared.activateFileViewerSelecting([dest])
        }
        refresh()
    }

    /// Leave the parent hub. Confirmed first, then run off the main thread.
    ///
    /// Off it because the steps talk to the parent, and the parent is
    /// occasionally the thing that went away — two calls that each wait out a
    /// timeout would freeze the menu bar for half a minute, which is this app
    /// looking like the casualty of its own button.
    @objc private func doDetach() {
        guard let binary = HostControl.hostBinary else { return }
        let mode = state.identity?.mode
        let named = mode?.parentHost ?? "the parent hub"
        let url = mode?.parent ?? ""
        NSApp.activate(ignoringOtherApps: true)

        let ask = NSAlert()
        ask.alertStyle = .critical
        ask.messageText = "Detach from \(named)?"
        ask.informativeText = "This Mac stops being administered from "
            + "\(url.isEmpty ? named : url). The grants it issued are revoked, "
            + "so nothing there can mint credentials here; that hub is asked to "
            + "drop this machine from its grid; and the leaf tunnel comes down, "
            + "so this Mac leaves the mesh.\n\nDevices paired directly to this "
            + "Mac keep working. Rejoining needs a fresh code from that hub."
        ask.addButton(withTitle: "Detach")
        ask.addButton(withTitle: "Cancel")
        guard ask.runModal() == .alertFirstButtonReturn else { return }

        let panel = Self.workingPanel("Detaching from \(named)…")
        DispatchQueue.global(qos: .userInitiated).async {
            let result = HostControl.run(binary, ["detach", "--json"])
            DispatchQueue.main.async { [weak self] in
                panel.close()
                self?.showDetachOutcome(result)
                self?.refresh()
            }
        }
    }

    /// Every step detach reported, in its own words, and what to do about the
    /// half this app cannot perform.
    ///
    /// The tunnel lives under `root` — two LaunchDaemons and a `/etc/wireguard`
    /// conf — and this app runs as the user, with no terminal to put a password
    /// into. So that half can fail while everything else succeeded, and the
    /// result is a machine that revoked its grants and is still on the mesh.
    /// Saying "detached" there would be the menu lying about the one state that
    /// matters; instead it says which half did not happen and hands over the
    /// command that finishes it.
    private func showDetachOutcome(_ result: (out: String, code: Int32)) {
        let alert = NSAlert()
        guard let outcome = DetachOutcome(json: result.out) else {
            alert.alertStyle = .warning
            alert.messageText = "Detach did not report an outcome"
            alert.informativeText = result.out
                .trimmingCharacters(in: .whitespacesAndNewlines)
            NSApp.activate(ignoringOtherApps: true)
            alert.runModal()
            return
        }

        let finished = outcome.detached && !outcome.isManaged
        alert.alertStyle = finished ? .informational : .warning
        alert.messageText = finished ? "Detached" : "Partly detached"
        var body = outcome.report
        if !finished {
            body += "\n\nThe tunnel is installed under root and this app runs "
                + "as you, with nowhere to put a password. Finish in a "
                + "terminal:"
        }
        alert.informativeText = body
        if !finished {
            alert.accessoryView = Self.commandAccessory("sudo jstack-host detach")
            alert.addButton(withTitle: "Copy Command")
            alert.addButton(withTitle: "Done")
            NSApp.activate(ignoringOtherApps: true)
            if alert.runModal() == .alertFirstButtonReturn {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString("sudo jstack-host detach",
                                               forType: .string)
            }
            return
        }
        alert.addButton(withTitle: "Done")
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }

    /// Drop an adopted machine from the grid, after asking.
    ///
    /// The confirmation draws the line the route itself draws: forgetting takes
    /// the tile off every device and drops the grant, and it does not touch the
    /// credentials that machine already holds or the tunnel it dialled out on.
    /// Claiming a lockout this hub cannot perform would be the more comforting
    /// sentence and the false one.
    @objc private func doForgetMachine(_ sender: NSMenuItem) {
        guard let machine = sender.representedObject as? AdoptedHost,
              !machine.key.isEmpty, let token = HostAgent.token() else { return }

        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Forget \(machine.title)?"
        alert.informativeText = "The tile comes off every device paired to this "
            + "hub, and the grant held for that machine is dropped — nothing "
            + "here can mint access to it any more.\n\nIt is not a lockout: "
            + "credentials already minted over there stay live until that "
            + "machine revokes them, and its tunnel stays up until it detaches. "
            + "Adopting it again takes a fresh code."
        alert.addButton(withTitle: "Forget")
        alert.addButton(withTitle: "Cancel")
        NSApp.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        probe.forget(hostKey: machine.key, token: token) { [weak self] ok, detail in
            if !ok {
                let failed = NSAlert()
                failed.alertStyle = .warning
                failed.messageText = "Could not forget \(machine.title)"
                failed.informativeText = detail
                failed.runModal()
            }
            self?.refresh()
        }
    }

    /// A command, selectable and wrapping, in the face it will be typed in.
    ///
    /// Selectable as well as copyable: Copy Command is a button somebody may
    /// have already walked past, and a command you cannot select is one you
    /// retype by eye.
    private static func commandAccessory(_ command: String) -> NSView {
        let field = NSTextField(wrappingLabelWithString: command)
        field.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        field.isSelectable = true
        field.preferredMaxLayoutWidth = 260
        field.frame = NSRect(x: 0, y: 0, width: 260,
                             height: max(20, field.fittingSize.height))
        return field
    }

    /// A window that says something is happening and offers nothing to answer.
    ///
    /// Not an NSAlert: `runModal()` blocks the main thread, and the whole point
    /// here is that the work is on another one — a modal loop would be a
    /// spinner turning over an application that has stopped. Not cancellable
    /// either, because a Cancel that cannot recall a `launchctl bootout`
    /// already in flight is the one control in this menu that would lie.
    private static func workingPanel(_ text: String) -> NSWindow {
        let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 340, height: 92),
                            styleMask: [.titled], backing: .buffered, defer: false)
        panel.title = "jStack"
        let spinner = NSProgressIndicator(
            frame: NSRect(x: 22, y: 36, width: 20, height: 20))
        spinner.style = .spinning
        spinner.startAnimation(nil)
        let label = NSTextField(labelWithString: text)
        label.frame = NSRect(x: 54, y: 34, width: 264, height: 24)
        panel.contentView?.addSubview(spinner)
        panel.contentView?.addSubview(label)
        panel.center()
        NSApp.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
        return panel
    }

    // MARK: Settings

    @objc private func doToggleHubLogin(_ sender: NSMenuItem) {
        setLogin(HostAgent.label, sender, what: "the hub")
    }

    @objc private func doToggleBarLogin(_ sender: NSMenuItem) {
        setLogin(MenuBarAgent.label, sender, what: "the menu bar app")
    }

    /// Flip the flag, and say so if the file would not take it.
    ///
    /// Silence on failure is the thing to avoid here: the row would redraw from
    /// the plist on the next open and simply appear not to have been clicked,
    /// which is indistinguishable from a menu that ignores you.
    private func setLogin(_ label: String, _ sender: NSMenuItem, what: String) {
        let wanted = sender.state != .on
        guard LoginAgent.setStartsAtLogin(label, wanted) else {
            let failed = NSAlert()
            failed.alertStyle = .warning
            failed.messageText = "Could not change the login setting"
            failed.informativeText = "\(LoginAgent.plistURL(label).path) could "
                + "not be written."
            NSApp.activate(ignoringOtherApps: true)
            failed.runModal()
            return
        }
        sender.state = wanted ? .on : .off
        // Said out loud once, because a checkbox that ticks instantly reads as
        // something that happened instantly — and this one has not happened yet.
        if !wanted {
            let note = NSAlert()
            note.messageText = "\(what.prefix(1).uppercased())\(what.dropFirst()) "
                + "will not start at the next login"
            note.informativeText = "Whatever is running now keeps running. "
                + "Turn it back on here."
            NSApp.activate(ignoringOtherApps: true)
            note.runModal()
        }
    }

    /// The state of everything, as text, for pasting into a message when
    /// something is wrong. The token's *path* and whether it reads — never its
    /// value: this goes to a clipboard, and a clipboard goes anywhere.
    @objc private func doCopyDiagnostics() {
        var lines = [
            "jStack host — \(Machine.name)",
            "hub        \(state.headline)",
            "agent      \(HostAgent.label)"
                + (HostAgent.isInstalled ? "" : " (no plist)"),
            "login      hub \(HostAgent.isInstalled && LoginAgent.startsAtLogin(HostAgent.label) ? "yes" : "no")"
                + ", menu bar \(LoginAgent.startsAtLogin(MenuBarAgent.label) ? "yes" : "no")",
            "bind       \(HostAgent.bind() ?? "not recorded")",
            "state      \(HostAgent.stateDir().path)",
            "token      \(HostAgent.tokenPath().path)"
                + (HostAgent.token() == nil ? " — missing" : " — present"),
        ]
        if let identity = state.identity {
            lines.append("host_id    \(identity.hostId ?? "?")")
            lines.append("profile    \(identity.profile ?? "?")")
        }
        if let mode = state.identity?.mode {
            // The parent's URL in full here, where the menu row shows only the
            // host: this is the paste that goes to whoever is working out why
            // the two machines cannot see each other, and the port is half of
            // that answer. It is the address, never the credential — the token
            // beside it in `parent.json` is not on this route at all.
            lines.append("mode       \(mode.mode ?? "?")"
                         + (mode.live == false ? " (tunnel down)" : "")
                         + (mode.parent.map { $0.isEmpty ? "" : " ← \($0)" } ?? ""))
        }
        lines.append("sessions   \(state.sessions.count) "
                     + "(\(state.liveCount) working)")
        if !state.leaves.isEmpty {
            let delegated = state.leaves.filter { $0.delegated == true }.count
            lines.append("machines   \(state.leaves.count) adopted "
                         + "(\(delegated) delegated)")
            for machine in state.leaves.sorted(by: { $0.title < $1.title }) {
                let access = machine.delegated.map { $0 ? "delegated" : "pair-by-hand" }
                    ?? "access not reported"
                lines.append("           \(machine.title) — "
                             + "\(machine.route.isEmpty ? "no address" : machine.route)"
                             + " — \(access)")
            }
        }
        if state.unauthorized { lines.append("auth       token refused by the hub") }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(lines.joined(separator: "\n"), forType: .string)
    }

    private func after(_ seconds: TimeInterval, _ block: @escaping () -> Void) {
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds, execute: block)
    }
}

extension StatusController: NSMenuDelegate {
    /// Poll on open as well as on the timer. The ten-second tick is for the
    /// icon; a menu being opened is someone asking right now, and showing them
    /// a board up to ten seconds stale is the thing that makes an indicator
    /// stop being believed.
    func menuWillOpen(_ menu: NSMenu) {
        menuIsOpen = true
        refresh()
    }
    func menuDidClose(_ menu: NSMenu) {
        menuIsOpen = false
        build()
    }
}

// MARK: - The app

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var controller: StatusController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // No Dock tile, no menu bar of its own — this app *is* its status item.
        // Set in code as well as in Info.plist so a binary run straight out of
        // the build directory behaves the same as the installed bundle.
        NSApp.setActivationPolicy(.accessory)
        controller = StatusController()
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        if urls.contains(where: { $0.scheme == "jstack" && $0.host == "updates" }) {
            controller?.showUpdates()
        }
    }
}

#if !JSTACK_MENUBAR_TEST
let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
#endif
