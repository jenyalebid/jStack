import Foundation
import CryptoKit
import Security
import ServiceManagement
import Darwin

// Invoked once through the OS administrator prompt, from a protected copy.
// No privileged socket, shell dispatch, or persistent installer process.
enum InstallFailure: Error { case refused(String) }
let manager = FileManager.default
let store = URL(fileURLWithPath: "/Library/PrivilegedHelperTools/.jstack-network")
let destination = URL(fileURLWithPath: "/Library/PrivilegedHelperTools/jStack Network.app")
let policyPath = URL(fileURLWithPath: "/Library/Preferences/live.jstack.network.json")

struct CodePin: Codable {
    let path: String
    let sha256: String
    let owner: UInt32
}
struct LegacyJob: Codable {
    let label: String
    let sha256: String
    let loaded: Bool
    let disabled: Bool
    let sources: [CodePin]
}
struct InstallPolicy: Codable, Equatable {
    let owner: UInt32
    let configuration: String
    let address: String
    let subnet: String
    let nameFile: String
    let forwarding: Bool
    var active: Bool
}
struct InstallRequest: Codable {
    let schema: Int
    let action: String
    let transaction: String
    let candidate: String?
    let candidateSeal: String?
    let candidateBinary: String?
    let policy: InstallPolicy?
    let legacy: [LegacyJob]?
}
struct Journal: Codable {
    var state: String
    let request: InstallRequest
    let previousPolicy: String?
    let previousApp: Bool
    var retired: [String]
    let forwardingBefore: Int
    var restoreActive: Bool?
    let previousApproval: String?
    var recoveryPhase: String?
}

func refuse(_ reason: String) -> InstallFailure { .refused(reason) }
func hash(_ data: Data) -> String { SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined() }
func hashFile(_ url: URL) throws -> String { hash(try Data(contentsOf: url)) }
func safeName(_ value: String) -> Bool {
    value.range(of: "^[A-Za-z0-9_.-]{1,128}$", options: .regularExpression) != nil
}
func protected(_ url: URL) throws {
    var current = url.path
    while true {
        var info = stat()
        guard lstat(current, &info) == 0, info.st_uid == 0,
              info.st_mode & 0o022 == 0, info.st_mode & S_IFMT != S_IFLNK else {
            throw refuse("unprotected administrator path")
        }
        if current == "/" { return }
        current = (current as NSString).deletingLastPathComponent
    }
}
func verify(_ url: URL, identifier: String) throws {
    var code: SecStaticCode?
    var requirement: SecRequirement?
    let expression = "anchor apple generic and certificate leaf[subject.OU] = \"MZ95H77RQQ\" and identifier \"\(identifier)\""
    guard SecStaticCodeCreateWithPath(url as CFURL, [], &code) == errSecSuccess,
          SecRequirementCreateWithString(expression as CFString, [], &requirement) == errSecSuccess,
          let code, let requirement,
          SecStaticCodeCheckValidity(code, SecCSFlags(rawValue: kSecCSCheckAllArchitectures | kSecCSCheckNestedCode | kSecCSStrictValidate), requirement) == errSecSuccess else {
        throw refuse("installer or candidate signature rejected")
    }
}
func write<T: Encodable>(_ value: T, to path: URL) throws {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    try encoder.encode(value).write(to: path, options: .atomic)
    try manager.setAttributes([.posixPermissions: 0o600, .ownerAccountID: 0, .groupOwnerAccountID: 0], ofItemAtPath: path.path)
    for item in [path, path.deletingLastPathComponent()] {
        let descriptor = open(item.path, O_RDONLY | O_NOFOLLOW)
        guard descriptor >= 0 else { throw refuse("cannot synchronize transaction") }
        defer { close(descriptor) }
        guard fsync(descriptor) == 0 else { throw refuse("cannot persist transaction") }
    }
}
func hardenedCopy(_ source: URL, _ target: URL) throws {
    try manager.copyItem(at: source, to: target)
    var paths = [target]
    if let entries = manager.enumerator(at: target, includingPropertiesForKeys: nil) {
        while let entry = entries.nextObject() as? URL { paths.append(entry) }
    }
    for path in paths {
        var info = stat()
        guard lstat(path.path, &info) == 0, info.st_mode & S_IFMT != S_IFLNK else {
            throw refuse("linked candidate resources are unsupported")
        }
        let mode = info.st_mode & S_IFMT == S_IFDIR || info.st_mode & 0o111 != 0 ? 0o755 : 0o644
        try manager.setAttributes([.posixPermissions: mode, .ownerAccountID: 0, .groupOwnerAccountID: 0], ofItemAtPath: path.path)
        try protected(path)
    }
}

// File output avoids pipe backpressure defeating the command timeout.
func run(_ executable: String, _ arguments: [String], at work: URL) throws -> (Int32, String) {
    let output = work.appendingPathComponent("output-" + UUID().uuidString)
    guard manager.createFile(atPath: output.path, contents: nil, attributes: [.posixPermissions: 0o600]) else {
        throw refuse("cannot create command observation")
    }
    defer { try? manager.removeItem(at: output) }
    let handle = try FileHandle(forWritingTo: output)
    defer { try? handle.close() }
    let process = Process()
    process.executableURL = URL(fileURLWithPath: executable)
    process.arguments = arguments
    process.environment = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin"]
    process.standardInput = FileHandle.nullDevice
    process.standardOutput = handle
    process.standardError = handle
    try process.run()
    let deadline = Date().addingTimeInterval(30)
    while process.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.02) }
    if process.isRunning {
        kill(process.processIdentifier, SIGKILL)
        process.waitUntilExit()
        throw refuse("administrator command timed out")
    }
    process.waitUntilExit()
    return (process.terminationStatus, String(data: try Data(contentsOf: output), encoding: .utf8) ?? "")
}
func loaded(_ label: String, at work: URL) throws -> Bool {
    let (status, text) = try run("/bin/launchctl", ["print", "system/" + label], at: work)
    if status == 0 { return true }
    if text.contains("Could not find service") { return false }
    throw refuse("system launchd state is unobservable")
}
func disabled(at work: URL) throws -> Set<String> {
    let (status, text) = try run("/bin/launchctl", ["print-disabled", "system"], at: work)
    guard status == 0, text.contains("disabled services = {") else { throw refuse("system disabled state is unobservable") }
    let expression = try NSRegularExpression(pattern: "\"([^\"\\n]+)\"\\s*=>\\s*([A-Za-z]+)")
    var result = Set<String>()
    for match in expression.matches(in: text, range: NSRange(text.startIndex..., in: text)) {
        let label = (text as NSString).substring(with: match.range(at: 1))
        let value = (text as NSString).substring(with: match.range(at: 2))
        guard ["true", "false", "enabled", "disabled"].contains(value) else { throw refuse("unsupported disabled state") }
        if value == "true" || value == "disabled" { result.insert(label) }
    }
    return result
}
func legacyPath(_ job: LegacyJob) throws -> URL {
    guard safeName(job.label), job.label != "live.jstack.network", !job.sources.isEmpty else { throw refuse("invalid reviewed legacy job") }
    return URL(fileURLWithPath: "/Library/LaunchDaemons").appendingPathComponent(job.label + ".plist")
}
func checkLegacy(_ job: LegacyJob, at work: URL, lifecycle: Bool) throws {
    let path = try legacyPath(job)
    try protected(path)
    guard try hashFile(path) == job.sha256 else { throw refuse("legacy definition changed") }
    let plist = try PropertyListSerialization.propertyList(from: Data(contentsOf: path), format: nil) as? [String: Any]
    guard plist?["Label"] as? String == job.label else { throw refuse("legacy label differs from its definition") }
    for pin in job.sources {
        var info = stat()
        guard pin.path.hasPrefix("/"), lstat(pin.path, &info) == 0,
              info.st_uid == pin.owner, info.st_mode & S_IFMT == S_IFREG,
              try hashFile(URL(fileURLWithPath: pin.path)) == pin.sha256 else { throw refuse("reviewed legacy code changed") }
    }
    if lifecycle {
        guard try loaded(job.label, at: work) == job.loaded,
              try disabled(at: work).contains(job.label) == job.disabled else { throw refuse("legacy lifecycle changed") }
    }
}
func bundlePins(_ app: URL, _ request: InstallRequest) throws {
    try verify(app, identifier: "live.jstack.network")
    guard try hashFile(app.appendingPathComponent("Contents/_CodeSignature/CodeResources")) == request.candidateSeal,
          try hashFile(app.appendingPathComponent("Contents/MacOS/JStackHub")) == request.candidateBinary else {
        throw refuse("candidate differs from reviewed bundle")
    }
}

func sameBundle(_ app: URL, _ backup: URL) throws {
    for item in [app, backup] {
        try protected(item)
        try verify(item, identifier: "live.jstack.network")
    }
    for item in ["Contents/_CodeSignature/CodeResources", "Contents/MacOS/JStackHub"] {
        guard try hashFile(app.appendingPathComponent(item)) == hashFile(backup.appendingPathComponent(item)) else {
            throw refuse("installed bundle changed during recovery")
        }
    }
}

func recoveryBundle(_ journal: Journal, at transaction: URL) throws {
    if journal.previousApp && (try? sameBundle(destination, transaction.appendingPathComponent("previous.app"))) != nil {
        return
    }
    try protected(destination)
    try bundlePins(destination, journal.request)
}

func userControl(_ owner: UInt32, action: String, work: URL) throws -> String {
    guard owner != 0, ["status", "register", "unregister"].contains(action) else { throw refuse("invalid user service control") }
    try protected(destination)
    try verify(destination, identifier: "live.jstack.network")
    var arguments = ["asuser", String(owner), "/usr/bin/sudo", "-n", "-H", "-u", "#" + String(owner),
                     destination.appendingPathComponent("Contents/MacOS/JStackHub").path, action]
    if action != "status" { arguments.append("network") }
    let result = try run("/bin/launchctl", arguments, at: work)
    guard result.0 == 0, let data = result.1.data(using: .utf8),
          let value = try JSONSerialization.jsonObject(with: data) as? [String: String],
          let status = value[action == "status" ? "network" : "status"],
          ["enabled", "not_registered", "not_found", "requires_approval"].contains(status) else {
        throw refuse("Network owner approval is unobservable")
    }
    return status
}
func stopNetwork(_ owner: UInt32, at work: URL, allowDenied: Bool = false) throws {
    let status = try userControl(owner, action: "status", work: work)
    guard allowDenied || status != "requires_approval" else {
        throw refuse("Network approval changed before unregister; leaving the approval choice intact")
    }
    var group: pid_t?
    if try loaded("live.jstack.network", at: work) {
        let observation = try run("/bin/launchctl", ["print", "system/live.jstack.network"], at: work).1
        let expression = try NSRegularExpression(pattern: "(?m)^\\tpid = ([0-9]+)$")
        if let match = expression.firstMatch(in: observation, range: NSRange(observation.startIndex..., in: observation)),
           let pid = Int32((observation as NSString).substring(with: match.range(at: 1))) {
            guard getpgid(pid) == pid else { throw refuse("Network process group is not isolated") }
            group = pid
        }
    }
    if status == "enabled" || status == "requires_approval" { _ = try userControl(owner, action: "unregister", work: work) }
    let deadline = Date().addingTimeInterval(30)
    while Date() < deadline {
        let registered = try loaded("live.jstack.network", at: work)
        let childrenGone = group.map { kill(-$0, 0) != 0 && errno == ESRCH } ?? true
        if !registered && childrenGone { return }
        Thread.sleep(forTimeInterval: 0.05)
    }
    throw refuse("Network process group has not stopped")
}
func restoreForwarding(_ value: Int, work: URL) throws {
    guard [0, 1].contains(value), try run("/usr/sbin/sysctl", ["-w", "net.inet.ip.forwarding=" + String(value)], at: work).0 == 0 else {
        throw refuse("forwarding state could not be restored")
    }
}

func stage(_ request: InstallRequest, at transaction: URL, work: URL) throws {
    guard !manager.fileExists(atPath: transaction.path), let source = request.candidate,
          source.hasPrefix("/"), let policy = request.policy, policy.owner != 0,
          let jobs = request.legacy, Set(jobs.map(\.label)).count == jobs.count else { throw refuse("invalid or previously staged transaction") }
    for job in jobs { try checkLegacy(job, at: work, lifecycle: true) }
    if policy.active && jobs.contains(where: { !$0.loaded || $0.disabled }) {
        throw refuse("migration cannot enable an OFF legacy network")
    }
    let candidate = URL(fileURLWithPath: source)
    try bundlePins(candidate, request)
    try manager.createDirectory(at: transaction, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
    let incoming = transaction.appendingPathComponent("candidate.app")
    try hardenedCopy(candidate, incoming)
    try bundlePins(incoming, request)
    guard try run("/usr/sbin/spctl", ["--assess", "--type", "execute", incoming.path], at: work).0 == 0 else {
        throw refuse("protected candidate was not accepted by Gatekeeper")
    }
    let hadApp = manager.fileExists(atPath: destination.path)
    if hadApp { try protected(destination); try verify(destination, identifier: "live.jstack.network") }
    let previousApproval = hadApp ? try userControl(policy.owner, action: "status", work: work) : nil
    let previous = manager.fileExists(atPath: policyPath.path) ? try Data(contentsOf: policyPath) : nil
    if previous != nil { try protected(policyPath) }
    if hadApp {
        guard jobs.isEmpty, let previous else { throw refuse("existing Network installation is incomplete") }
        var old = try JSONDecoder().decode(InstallPolicy.self, from: previous)
        old.active = old.active && previousApproval == "enabled"
        guard policy == old else { throw refuse("update must preserve Network configuration and the existing OFF choice") }
        try hardenedCopy(destination, transaction.appendingPathComponent("previous.app"))
    }
    for job in jobs { try hardenedCopy(legacyPath(job), transaction.appendingPathComponent(job.label + ".original.plist")) }
    let forwarding = try run("/usr/sbin/sysctl", ["-n", "net.inet.ip.forwarding"], at: work)
    guard forwarding.0 == 0, let wasForwarding = Int(forwarding.1.trimmingCharacters(in: .whitespacesAndNewlines)),
          [0, 1].contains(wasForwarding) else { throw refuse("forwarding state is unobservable") }
    var journal = Journal(state: "staging", request: request, previousPolicy: previous?.base64EncodedString(), previousApp: hadApp, retired: [], forwardingBefore: wasForwarding, previousApproval: previousApproval)
    let journalPath = transaction.appendingPathComponent("journal.json")
    try write(journal, to: journalPath)
    if hadApp {
        journal.state = "staged_update"
        try write(journal, to: journalPath)
        return
    }
    let installing = transaction.appendingPathComponent("installing.app")
    try hardenedCopy(incoming, installing)
    try manager.moveItem(at: installing, to: destination)
    var inactive = policy
    inactive.active = false
    try write(inactive, to: policyPath)
    let check = try run(destination.appendingPathComponent("Contents/MacOS/JStackNetwork").path, ["--check"], at: work)
    guard check.0 == 0 else { throw refuse("protected network policy validation failed") }
    journal.state = "staged"
    try write(journal, to: journalPath)
}

func activate(_ request: InstallRequest, at transaction: URL, work: URL) throws {
    let path = transaction.appendingPathComponent("journal.json")
    var journal = try JSONDecoder().decode(Journal.self, from: Data(contentsOf: path))
    guard ["staged", "staged_update", "awaiting_approval"].contains(journal.state),
          let policy = journal.request.policy, let jobs = journal.request.legacy else { throw refuse("transaction is not ready for activation") }
    if journal.state == "staged_update" {
        let previous = transaction.appendingPathComponent("previous.app")
        try protected(previous)
        let previousPolicyHash = journal.previousPolicy.flatMap { Data(base64Encoded: $0) }.map { hash($0) }
        guard try hashFile(destination.appendingPathComponent("Contents/_CodeSignature/CodeResources")) == hashFile(previous.appendingPathComponent("Contents/_CodeSignature/CodeResources")),
              try hashFile(destination.appendingPathComponent("Contents/MacOS/JStackHub")) == hashFile(previous.appendingPathComponent("Contents/MacOS/JStackHub")),
              try hashFile(policyPath) == previousPolicyHash,
              try userControl(policy.owner, action: "status", work: work) == journal.previousApproval else { throw refuse("existing Network changed after staging") }
        journal.state = "replacing"
        try write(journal, to: path)
        try stopNetwork(policy.owner, at: work)
        try manager.moveItem(at: destination, to: transaction.appendingPathComponent("retired-installed.app"))
        let installing = transaction.appendingPathComponent("installing.app")
        try hardenedCopy(transaction.appendingPathComponent("candidate.app"), installing)
        try manager.moveItem(at: installing, to: destination)
        var inactive = policy
        inactive.active = false
        try write(inactive, to: policyPath)
        guard try run(destination.appendingPathComponent("Contents/MacOS/JStackNetwork").path, ["--check"], at: work).0 == 0 else {
            throw refuse("updated network policy failed validation; explicit recovery required")
        }
        journal.state = "awaiting_approval"
        try write(journal, to: path)
        if policy.active {
            let status = try userControl(policy.owner, action: "register", work: work)
            if status != "enabled" { return }
        }
    }
    try bundlePins(destination, journal.request)
    if policy.active {
        guard try userControl(policy.owner, action: "status", work: work) == "enabled",
              try loaded("live.jstack.network", at: work) else { throw refuse("approve the installed Network owner before cutover") }
    }
    for job in jobs { try checkLegacy(job, at: work, lifecycle: true) }
    journal.state = "retiring"
    try write(journal, to: path)
    for job in jobs {
        try checkLegacy(job, at: work, lifecycle: true)
        journal.retired.append(job.label)
        try write(journal, to: path)
        if job.loaded { _ = try run("/bin/launchctl", ["bootout", "system/" + job.label], at: work) }
        let deadline = Date().addingTimeInterval(30)
        while try loaded(job.label, at: work) {
            guard Date() < deadline else { throw refuse("legacy network has not stopped") }
            Thread.sleep(forTimeInterval: 0.1)
        }
        try manager.moveItem(at: legacyPath(job), to: transaction.appendingPathComponent(job.label + ".retired.plist"))
    }
    if policy.active {
        let deadline = Date().addingTimeInterval(30)
        while true {
            let interfaces = try run("/sbin/ifconfig", ["-a"], at: work)
            guard interfaces.0 == 0 else { throw refuse("network interfaces are unobservable") }
            if !interfaces.1.contains("inet " + String(policy.address.split(separator: "/")[0]) + " ") { break }
            guard Date() < deadline else { throw refuse("legacy address is still live; refusing overlapping tunnels") }
            Thread.sleep(forTimeInterval: 0.1)
        }
    }
    if try policy.active && !loaded("live.jstack.network", at: work) { throw refuse("Network approval changed during cutover") }
    journal.state = "activating"
    try write(journal, to: path)
    try write(policy, to: policyPath)
    if policy.active {
        let deadline = Date().addingTimeInterval(30)
        var healthy = false
        while Date() < deadline {
            if let name = try? String(contentsOfFile: policy.nameFile, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines),
               name.range(of: "^utun[0-9]+$", options: .regularExpression) != nil {
                let result = try run("/sbin/ifconfig", [name], at: work)
                if result.0 == 0 && result.1.contains("inet " + String(policy.address.split(separator: "/")[0]) + " ") { healthy = true; break }
            }
            Thread.sleep(forTimeInterval: 0.25)
        }
        guard healthy else { throw refuse("new network did not become active; explicit recovery required") }
    }
    journal.state = "active"
    try write(journal, to: path)
}

func rollback(at transaction: URL, work: URL, uninstall: Bool = false) throws {
    let path = transaction.appendingPathComponent("journal.json")
    var journal = try JSONDecoder().decode(Journal.self, from: Data(contentsOf: path))
    if journal.state == (uninstall ? "uninstalled" : "rolled_back") { return }
    guard !["uninstalled", "rolled_back"].contains(journal.state),
          !uninstall || ["active", "uninstalling"].contains(journal.state),
          uninstall || journal.state != "uninstalling" else { throw refuse("recovery action conflicts with transaction state") }
    guard var policy = journal.request.policy, let jobs = journal.request.legacy else { throw refuse("missing original Network transaction") }
    let staged = transaction.appendingPathComponent("candidate.app")
    try protected(staged)
    try bundlePins(staged, journal.request)
    let archive = transaction.appendingPathComponent(uninstall ? "uninstalled.app" : "retired-candidate.app")
    if journal.recoveryPhase == nil {
        // Before any self-initiated unregister, retain the observed OFF choice.
        if !manager.fileExists(atPath: destination.path) { try hardenedCopy(staged, destination) }
        try recoveryBundle(journal, at: transaction)
        let approval = try userControl(policy.owner, action: "status", work: work)
        if !uninstall && approval == "requires_approval" {
            throw refuse("Network approval was denied; recovery requires the user's approval choice")
        }
        journal.restoreActive = approval == "enabled" || (!journal.previousApp && journal.retired.isEmpty)
        journal.state = uninstall ? "uninstalling" : "rolling_back"
        journal.recoveryPhase = "stopping"
        try write(journal, to: path)
    }
    if journal.recoveryPhase == "stopping" {
        try recoveryBundle(journal, at: transaction)
        policy.active = false
        try write(policy, to: policyPath)
        try stopNetwork(policy.owner, at: work, allowDenied: uninstall)
        if try !uninstall && userControl(policy.owner, action: "status", work: work) == "requires_approval" {
            throw refuse("Network approval changed during recovery")
        }
        journal.recoveryPhase = "archiving"
        try write(journal, to: path)
    }
    if journal.recoveryPhase == "archiving" {
        if manager.fileExists(atPath: destination.path) {
            try recoveryBundle(journal, at: transaction)
            guard !manager.fileExists(atPath: archive.path) else { throw refuse("recovery archive conflicts with current bundle") }
            try manager.moveItem(at: destination, to: archive)
        }
        guard manager.fileExists(atPath: archive.path), try !loaded("live.jstack.network", at: work) else {
            throw refuse("stopped Network archive is missing or still registered")
        }
        journal.recoveryPhase = "restoring"
        try write(journal, to: path)
    }
    if uninstall {
        if manager.fileExists(atPath: policyPath.path) {
            try manager.moveItem(at: policyPath, to: transaction.appendingPathComponent("uninstalled-policy.json"))
        }
    } else {
        if journal.previousApp {
            let previous = transaction.appendingPathComponent("previous.app")
            try protected(previous)
            try verify(previous, identifier: "live.jstack.network")
            if !manager.fileExists(atPath: destination.path) { try hardenedCopy(previous, destination) }
            try sameBundle(destination, previous)
        }
        if let encoded = journal.previousPolicy, let data = Data(base64Encoded: encoded) {
            if journal.restoreActive == false {
                var old = try JSONDecoder().decode(InstallPolicy.self, from: data)
                old.active = false
                try write(old, to: policyPath)
            } else {
                try data.write(to: policyPath, options: .atomic)
                try manager.setAttributes([.posixPermissions: 0o600], ofItemAtPath: policyPath.path)
            }
        } else if manager.fileExists(atPath: policyPath.path) { try manager.removeItem(at: policyPath) }
        for job in jobs where journal.retired.contains(job.label) {
            let backup = transaction.appendingPathComponent(job.label + ".original.plist")
            guard try hashFile(backup) == job.sha256 else { throw refuse("legacy backup changed") }
            if journal.restoreActive == false && job.loaded && !job.disabled { continue }
            let original = try legacyPath(job)
            if !manager.fileExists(atPath: original.path) { try hardenedCopy(backup, original) }
            try checkLegacy(job, at: work, lifecycle: false)
            let legacyApproval = SMAppService.statusForLegacyPlist(at: original)
            if legacyApproval == .requiresApproval { throw refuse("legacy Network owner requires approval; recovery remains pending") }
            guard [.enabled, .notRegistered, .notFound].contains(legacyApproval) else { throw refuse("legacy approval is unobservable") }
            if try job.loaded && !job.disabled && !disabled(at: work).contains(job.label) && !loaded(job.label, at: work) {
                guard try run("/bin/launchctl", ["bootstrap", "system", original.path], at: work).0 == 0,
                      try loaded(job.label, at: work) else { throw refuse("legacy network could not be restored") }
            }
        }
        let previousWasActive = journal.previousPolicy.flatMap { Data(base64Encoded: $0) }
            .flatMap { try? JSONDecoder().decode(InstallPolicy.self, from: $0) }?.active == true
        if journal.previousApp && previousWasActive && journal.restoreActive == true {
            guard try userControl(policy.owner, action: "register", work: work) == "enabled" else { throw refuse("previous Network owner requires approval") }
        }
    }
    try restoreForwarding(journal.forwardingBefore, work: work)
    journal.state = uninstall ? "uninstalled" : "rolled_back"
    try write(journal, to: path)
}

@main struct NetworkInstaller {
    static func main() {
        do {
            guard geteuid() == 0, CommandLine.arguments.count == 2 else { throw refuse("installer requires approved protected execution") }
            let executable = URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL
            let work = executable.deletingLastPathComponent()
            let input = URL(fileURLWithPath: CommandLine.arguments[1]).standardizedFileURL
            guard input == work.appendingPathComponent("request.json"), work.deletingLastPathComponent() == store.appendingPathComponent("invocations") else { throw refuse("invalid approved request location") }
            try protected(executable)
            try protected(input)
            try verify(executable, identifier: "JStackNetworkInstaller")
            let request = try JSONDecoder().decode(InstallRequest.self, from: Data(contentsOf: input))
            guard request.schema == 1, request.transaction.range(of: "^[a-f0-9]{32}$", options: .regularExpression) != nil else { throw refuse("invalid transaction identity") }
            let transactions = store.appendingPathComponent("transactions")
            try manager.createDirectory(at: transactions, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            try protected(transactions)
            let lockPath = store.appendingPathComponent("operation.lock")
            let lock = open(lockPath.path, O_CREAT | O_RDWR | O_NOFOLLOW, 0o600)
            guard lock >= 0 else { throw refuse("cannot lock Network transaction") }
            defer { close(lock) }
            try protected(lockPath)
            guard flock(lock, LOCK_EX | LOCK_NB) == 0 else { throw refuse("another Network transaction is running") }
            let transaction = transactions.appendingPathComponent(request.transaction)
            if request.action == "stage" { try stage(request, at: transaction, work: work) }
            else if request.action == "activate" { try protected(transaction); try activate(request, at: transaction, work: work) }
            else if request.action == "rollback" || request.action == "uninstall" {
                try protected(transaction)
                try rollback(at: transaction, work: work, uninstall: request.action == "uninstall")
            }
            else { throw refuse("unsupported Network transaction action") }
            let journal = try JSONDecoder().decode(Journal.self, from: Data(contentsOf: transaction.appendingPathComponent("journal.json")))
            let result = try JSONSerialization.data(withJSONObject: ["transaction": request.transaction, "state": journal.state], options: [.sortedKeys])
            FileHandle.standardOutput.write(result)
        } catch InstallFailure.refused(let reason) {
            fputs("jStack Network installer: \(reason)\n", stderr)
            exit(1)
        } catch {
            fputs("jStack Network installer: protected transaction failed\n", stderr)
            exit(1)
        }
    }
}
