import Foundation
import ServiceManagement
import Darwin

// Only these sealed, app-bundled definitions can be registered. Never accept a
// caller-provided plist path, launchd domain, executable or root command.
let services = ["host": "live.jstack.hub.host.plist",
                "updater": "live.jstack.hub.updater.plist",
                "menu": "live.jstack.hub.menu.plist"]

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

func main() throws {
    guard geteuid() != 0 else { throw NSError(domain: "jStack", code: 77,
        userInfo: [NSLocalizedDescriptionKey: "User service control must not run as root"]) }
    let args = Array(CommandLine.arguments.dropFirst())
    guard let action = args.first else {
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
            result[key] = statusName(SMAppService.agent(plistName: plist).status)
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
            "usage: JStackHub status | settings | register|unregister host|updater|menu"])
    }
    let service = SMAppService.agent(plistName: plist)
    if action == "register" {
        // Approval revocation is not a registration failure to repair away.
        if service.status == .notRegistered || service.status == .notFound { try service.register() }
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
