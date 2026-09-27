import Foundation
import ServiceManagement
import Darwin

// Only these sealed, app-bundled definitions can be registered. Never accept a
// caller-provided plist path, launchd domain, executable or root command.
let privileged = Bundle.main.bundleIdentifier == "live.jstack.network"

func appService(_ plist: String) -> SMAppService {
    privileged ? SMAppService.daemon(plistName: plist) : SMAppService.agent(plistName: plist)
}

func serviceDefinitions() throws -> [String: String] {
    let url = Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/services.json")
    let services = try JSONDecoder().decode([String: String].self, from: Data(contentsOf: url))
    for (role, filename) in services {
        guard role.range(of: "^[a-z][a-z0-9-]{0,63}$", options: .regularExpression) != nil,
              filename.hasPrefix("live.jstack."), filename.hasSuffix(".plist"),
              !filename.contains("/") else {
            throw NSError(domain: "jStack", code: 78, userInfo: [NSLocalizedDescriptionKey: "Invalid sealed service catalog"])
        }
    }
    return services
}

func emit(_ value: Any) throws {
    let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

func statusName(_ value: SMAppService.Status) -> String {
    switch value {
    case .notRegistered: return "not_registered"
    case .enabled: return "enabled"
    case .requiresApproval: return "requires_approval"
    case .notFound: return "not_found"
    @unknown default: return "unknown"
    }
}

// #173: `enabled` is a registration record, not launchd's present state — a
// bundle swap can leave the record behind while launchd holds no such job at
// all. A role with no launchd job must never read enabled.
func crossExamined(_ recorded: String, loaded: Bool) -> String {
    recorded == "enabled" && !loaded ? "not_registered" : recorded
}

// The plist filename IS the launchd label (`serviceDefinitions` requires the
// `live.jstack.` prefix and `.plist` suffix), so no plist read is needed to
// ask launchd about it.
func labelOf(_ plist: String) -> String { String(plist.dropLast(".plist".count)) }

func launchdLoaded(_ label: String) -> Bool {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
    process.arguments = ["print", "gui/\(getuid())/\(label)"]
    process.standardOutput = FileHandle.nullDevice
    process.standardError = FileHandle.nullDevice
    guard (try? process.run()) != nil else { return false }
    process.waitUntilExit()
    return process.terminationStatus == 0
}

func emergencyStopped() -> Bool {
    let settings = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".local/state/jremote/service-settings.json")
    guard let data = try? Data(contentsOf: settings),
          let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let environment = value["environment"] as? [String: Any],
          let state = environment["JREMOTE_STATE_DIR"] as? String else { return false }
    let marker = URL(fileURLWithPath: state).appendingPathComponent("emergency-stop.json")
    guard let markerData = try? Data(contentsOf: marker),
          let record = try? JSONSerialization.jsonObject(with: markerData) as? [String: Any] else { return false }
    return record["schema"] as? Int == 1 && record["active"] as? Bool == true
}

func main() throws {
    guard geteuid() != 0 else { throw NSError(domain: "jStack", code: 77,
        userInfo: [NSLocalizedDescriptionKey: "User service control must not run as root"]) }
    let args = Array(CommandLine.arguments.dropFirst())
    let services = try serviceDefinitions()
    guard let action = args.first else {
        if privileged {
            try emit(services.mapValues { crossExamined(statusName(appService($0).status),
                                                        loaded: launchdLoaded(labelOf($0))) })
            return
        }
        let menu = Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/JStackHostBar")
        let process = Process()
        process.executableURL = menu
        try process.run()
        process.waitUntilExit()
        exit(process.terminationStatus)
    }
    if action == "status" && args.count == 1 {
        var result: [String: String] = [:]
        for (key, plist) in services {
            result[key] = crossExamined(statusName(appService(plist).status),
                                        loaded: launchdLoaded(labelOf(plist)))
        }
        try emit(result)
        return
    }
    if action == "settings" && args.count == 1 {
        SMAppService.openSystemSettingsLoginItems()
        return
    }
    if action == "legacy-status" && args.count == 2 {
        let url = URL(fileURLWithPath: args[1]).standardizedFileURL
        let roots = [URL(fileURLWithPath: "/Library/LaunchDaemons"),
                     URL(fileURLWithPath: "/Library/LaunchAgents"),
                     FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/LaunchAgents")]
        guard roots.contains(url.deletingLastPathComponent()), url.pathExtension == "plist" else {
            throw NSError(domain: "jStack", code: 64, userInfo: [NSLocalizedDescriptionKey: "Not a legacy startup definition"])
        }
        try emit(["status": statusName(SMAppService.statusForLegacyPlist(at: url))])
        return
    }
    guard args.count == 2, let plist = services[args[1]],
          action == "register" || action == "unregister" else {
        throw NSError(domain: "jStack", code: 64, userInfo: [NSLocalizedDescriptionKey:
            "usage: JStackHub status | settings | register|unregister <role in Contents/Resources/services.json>"])
    }
    let service = appService(plist)
    if action == "register" {
        guard !emergencyStopped() else { throw NSError(domain: "jStack", code: 77,
            userInfo: [NSLocalizedDescriptionKey: "jStack emergency stop is active"]) }
        // Approval revocation is not a registration failure to repair away.
        if service.status == .notRegistered || service.status == .notFound {
            do { try service.register() }
            catch {
                // Registering a new daemon can succeed in BTM but return
                // launch-denied while waiting for the administrator's UI
                // approval. Report that state; never retry to defeat it.
                if service.status != .requiresApproval { throw error }
            }
        }
    } else {
        try service.unregister()
    }
    try emit(["service": args[1], "status": statusName(service.status)])
}

do { try main() }
catch {
    FileHandle.standardError.write(Data((error.localizedDescription + "\n").utf8))
    exit(1)
}
