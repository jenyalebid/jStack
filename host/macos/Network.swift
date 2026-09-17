import Foundation
import Security
import Darwin

// No socket, shell, arbitrary executable, or caller-supplied command surface.
// launchd owns this process; root-owned policy chooses the sole user-owned
// WireGuard data file. That file is read as data, never sourced as code.
struct NetworkPolicy: Decodable {
    let owner: UInt32
    let configuration: String
    let address: String
    let subnet: String
    let nameFile: String
    let forwarding: Bool
    let active: Bool
}

enum NetworkFailure: Error { case invalid(String) }
let policyURL = URL(fileURLWithPath: "/Library/Preferences/live.jstack.network.json")
let applicationURL = URL(fileURLWithPath: "/Library/PrivilegedHelperTools/jStack Network.app")

final class ShutdownState {
    private let lock = NSLock()
    private var stopped = false
    func request() { lock.lock(); stopped = true; lock.unlock() }
    var requested: Bool { lock.lock(); defer { lock.unlock() }; return stopped }
}
let shutdownState = ShutdownState()

// Foundation.Process starts a separate process group. launchd cannot reap
// that group if this supervisor is SIGKILLed. Spawn the persistent tunnel in
// our own group so launchd owns crash cleanup as well as graceful shutdown.
final class TunnelProcess {
    let pid: pid_t
    private var reaped = false
    init(nameFile: String) throws {
        var child: pid_t = 0
        var actions: posix_spawn_file_actions_t?
        guard posix_spawn_file_actions_init(&actions) == 0 else {
            throw NetworkFailure.invalid("cannot initialize tunnel process")
        }
        defer { posix_spawn_file_actions_destroy(&actions) }
        for descriptor in [STDIN_FILENO, STDOUT_FILENO, STDERR_FILENO] {
            guard posix_spawn_file_actions_addopen(&actions, descriptor, "/dev/null",
                descriptor == STDIN_FILENO ? O_RDONLY : O_WRONLY, 0) == 0 else {
                throw NetworkFailure.invalid("cannot configure tunnel process")
            }
        }
        let argv: [UnsafeMutablePointer<CChar>?] = ["wireguard-go", "-f", "utun"].map { $0.withCString { strdup($0) } } + [nil]
        let environment: [UnsafeMutablePointer<CChar>?] = ["PATH=/usr/bin:/bin:/usr/sbin:/sbin", "WG_TUN_NAME_FILE=" + nameFile].map { $0.withCString { strdup($0) } } + [nil]
        defer { argv.forEach { free($0) }; environment.forEach { free($0) } }
        let executable = applicationURL.appendingPathComponent("Contents/MacOS/wireguard-go").path
        guard posix_spawn(&child, executable, &actions, nil, argv, environment) == 0 else {
            throw NetworkFailure.invalid("cannot spawn tunnel process")
        }
        pid = child
    }
    var isRunning: Bool {
        if reaped { return false }
        var status: Int32 = 0
        let result = waitpid(pid, &status, WNOHANG)
        if result == pid || (result < 0 && errno == ECHILD) { reaped = true }
        return !reaped
    }
    func stop() {
        if !isRunning { return }
        kill(pid, SIGTERM)
        let deadline = Date().addingTimeInterval(5)
        while isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
        if isRunning { kill(pid, SIGKILL); var status: Int32 = 0; _ = waitpid(pid, &status, 0); reaped = true }
    }
}

func rootProtected(_ url: URL, runtime: Bool = false) throws {
    var current = url.path
    if runtime {
        guard let resolved = current.withCString({ realpath($0, nil) }) else {
            throw NetworkFailure.invalid("missing runtime path")
        }
        current = String(cString: resolved)
        free(resolved)
    }
    while true {
        var value = stat()
        guard lstat(current, &value) == 0 else {
            throw NetworkFailure.invalid("missing privileged path")
        }
        // macOS owns /private/var/run as root:daemon 0775. This exception
        // applies only to the OS runtime ancestor, never code or policy.
        let systemRuntime = runtime && current == "/private/var/run" &&
            value.st_gid == 1 && value.st_mode & 0o777 == 0o775
        guard value.st_uid == 0,
              value.st_mode & 0o022 == 0 || systemRuntime,
              value.st_mode & S_IFMT != S_IFLNK else {
            throw NetworkFailure.invalid("unprotected privileged path")
        }
        if current == "/" { return }
        current = (current as NSString).deletingLastPathComponent
    }
}

func verifyBundle() throws {
    try rootProtected(applicationURL)
    var code: SecStaticCode?
    var requirement: SecRequirement?
    let expression = "anchor apple generic and certificate leaf[subject.OU] = \"MZ95H77RQQ\" and identifier \"live.jstack.network\""
    guard SecStaticCodeCreateWithPath(applicationURL as CFURL, [], &code) == errSecSuccess,
          SecRequirementCreateWithString(expression as CFString, [], &requirement) == errSecSuccess,
          let code, let requirement,
          SecStaticCodeCheckValidity(code, SecCSFlags(rawValue: kSecCSCheckAllArchitectures | kSecCSCheckNestedCode | kSecCSStrictValidate), requirement) == errSecSuccess else {
        throw NetworkFailure.invalid("network bundle signature rejected")
    }
    // Directory ownership alone does not protect an already writable child.
    let enumerator = FileManager.default.enumerator(at: applicationURL, includingPropertiesForKeys: nil)
    while let child = enumerator?.nextObject() as? URL { try rootProtected(child) }
}

func ipv4CIDR(_ value: String) -> Bool {
    let parts = value.split(separator: "/", omittingEmptySubsequences: false)
    guard parts.count == 2, let bits = Int(parts[1]), (1...32).contains(bits) else { return false }
    let octets = parts[0].split(separator: ".", omittingEmptySubsequences: false)
    return octets.count == 4 && octets.allSatisfy { item in
        !item.isEmpty && item.allSatisfy(\.isNumber) && Int(item).map { (0...255).contains($0) } == true
    }
}

func readPolicy() throws -> NetworkPolicy {
    try rootProtected(policyURL)
    let value = try JSONDecoder().decode(NetworkPolicy.self, from: Data(contentsOf: policyURL))
    guard value.configuration.hasPrefix("/"),
          ipv4CIDR(value.address), ipv4CIDR(value.subnet),
          value.nameFile.range(of: "^/var/run/wireguard/[a-zA-Z0-9.-]+$", options: .regularExpression) != nil else {
        throw NetworkFailure.invalid("invalid network policy")
    }
    return value
}

func configuration(_ policy: NetworkPolicy) throws -> Data {
    let descriptor = open(policy.configuration, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
    guard descriptor >= 0 else { throw NetworkFailure.invalid("configuration is unavailable") }
    defer { close(descriptor) }
    var metadata = stat()
    guard fstat(descriptor, &metadata) == 0, metadata.st_uid == policy.owner,
          metadata.st_mode & S_IFMT == S_IFREG, metadata.st_mode & 0o077 == 0,
          metadata.st_size > 0, metadata.st_size <= 1_048_576 else {
        throw NetworkFailure.invalid("configuration ownership or size rejected")
    }
    let handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: false)
    let data = try handle.read(upToCount: 1_048_577) ?? Data()
    guard data.count <= 1_048_576, let text = String(data: data, encoding: .utf8) else {
        throw NetworkFailure.invalid("invalid configuration encoding")
    }
    let keys: Set<String> = ["PrivateKey", "ListenPort", "FwMark", "PublicKey", "PresharedKey", "AllowedIPs", "Endpoint", "PersistentKeepalive"]
    for raw in text.components(separatedBy: .newlines) {
        let line = raw.trimmingCharacters(in: .whitespaces)
        if line.isEmpty || line.hasPrefix("#") || line == "[Interface]" || line == "[Peer]" { continue }
        guard let separator = line.firstIndex(of: "="),
              keys.contains(line[..<separator].trimmingCharacters(in: .whitespaces)) else {
            throw NetworkFailure.invalid("unsupported network configuration field")
        }
    }
    return data
}

func process(_ executable: String, _ arguments: [String], input: Data? = nil) throws -> Process {
    let task = Process()
    task.executableURL = URL(fileURLWithPath: executable)
    task.arguments = arguments
    task.environment = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin"]
    task.standardOutput = FileHandle.nullDevice
    task.standardError = FileHandle.nullDevice // never log configuration/key fragments
    if let input {
        let pipe = Pipe()
        task.standardInput = pipe
        try task.run()
        try pipe.fileHandleForWriting.write(contentsOf: input)
        try pipe.fileHandleForWriting.close()
    } else { try task.run() }
    return task
}

func wait(_ task: Process, seconds: TimeInterval = 30) throws {
    let deadline = Date().addingTimeInterval(seconds)
    while task.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
    if task.isRunning {
        task.terminate()
        let grace = Date().addingTimeInterval(5)
        while task.isRunning && Date() < grace { Thread.sleep(forTimeInterval: 0.05) }
        if task.isRunning { kill(task.processIdentifier, SIGKILL) }
        task.waitUntilExit()
        throw NetworkFailure.invalid("network operation timed out")
    }
    task.waitUntilExit()
}

func execute(_ executable: String, _ arguments: [String], input: Data? = nil) throws {
    let task = try process(executable, arguments, input: input)
    try wait(task)
    guard task.terminationStatus == 0 else { throw NetworkFailure.invalid("network operation failed") }
}

func serve(_ policy: NetworkPolicy) throws {
    let directory = URL(fileURLWithPath: "/var/run/wireguard").resolvingSymlinksInPath()
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                          attributes: [.posixPermissions: 0o755])
    try rootProtected(directory, runtime: true)
    let name = URL(fileURLWithPath: policy.nameFile)
    if FileManager.default.fileExists(atPath: name.path) {
        try rootProtected(name.resolvingSymlinksInPath(), runtime: true)
        let previous = try String(contentsOf: name, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
        guard previous.range(of: "^utun[0-9]+$", options: .regularExpression) != nil else {
            throw NetworkFailure.invalid("invalid stale interface name")
        }
        let probe = try process("/sbin/ifconfig", [previous])
        try wait(probe)
        guard probe.terminationStatus != 0 else {
            throw NetworkFailure.invalid("existing live tunnel requires migration")
        }
        try FileManager.default.removeItem(at: name)
    }
    let task = try TunnelProcess(nameFile: name.path)
    defer {
        task.stop()
        try? FileManager.default.removeItem(at: name)
    }
    let deadline = Date().addingTimeInterval(10)
    while !FileManager.default.fileExists(atPath: name.path) && task.isRunning && Date() < deadline && !shutdownState.requested { Thread.sleep(forTimeInterval: 0.1) }
    if shutdownState.requested { return }
    let interface = try String(contentsOf: name, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
    guard interface.range(of: "^utun[0-9]+$", options: .regularExpression) != nil else {
        throw NetworkFailure.invalid("invalid interface name")
    }
    let wg = applicationURL.appendingPathComponent("Contents/MacOS/wg").path
    var previous = try configuration(policy)
    try execute(wg, ["setconf", interface, "/dev/stdin"], input: previous)
    try execute("/sbin/ifconfig", [interface, "inet", policy.address, String(policy.address.split(separator: "/")[0]), "alias"])
    try execute("/sbin/ifconfig", [interface, "mtu", "1240", "up"])
    do {
        try execute("/sbin/route", ["-q", "-n", "add", "-inet", policy.subnet, "-interface", interface])
    } catch {
        // ifconfig may already have installed the connected route. Accept
        // that only when an independent route lookup names this interface.
        let probe = Process()
        let pipe = Pipe()
        probe.executableURL = URL(fileURLWithPath: "/sbin/route")
        probe.arguments = ["-n", "get", "-inet", String(policy.subnet.split(separator: "/")[0])]
        probe.standardOutput = pipe
        probe.standardError = FileHandle.nullDevice
        try probe.run()
        try wait(probe)
        let output = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        guard probe.terminationStatus == 0,
              output.components(separatedBy: .newlines).contains(where: { $0.trimmingCharacters(in: .whitespaces) == "interface: \(interface)" }) else {
            throw NetworkFailure.invalid("mesh route did not become active")
        }
    }
    if policy.forwarding { try execute("/usr/sbin/sysctl", ["-w", "net.inet.ip.forwarding=1"]) }
    print("jStack Network: interface active", terminator: "\n"); fflush(stdout)
    var refresh = Date()
    while task.isRunning && !shutdownState.requested {
        Thread.sleep(forTimeInterval: 1)
        if !(try readPolicy()).active { return }
        do {
            let next = try configuration(policy)
            if next != previous || Date().timeIntervalSince(refresh) >= 120 {
                try execute(wg, ["syncconf", interface, "/dev/stdin"], input: next)
                previous = next
                refresh = Date()
                print("jStack Network: configuration synchronized"); fflush(stdout)
            }
        } catch {
            fputs("jStack Network: configuration synchronization failed\n", stderr)
        }
    }
    if shutdownState.requested { return }
    throw NetworkFailure.invalid("tunnel process exited")
}

@main struct NetworkProgram {
    static func main() {
        do {
            guard geteuid() == 0 else { throw NetworkFailure.invalid("network service requires root") }
            guard CommandLine.arguments.count == 1 || CommandLine.arguments == [CommandLine.arguments[0], "--check"] else {
                throw NetworkFailure.invalid("unsupported network command")
            }
            try verifyBundle()
            if CommandLine.arguments.count == 2 {
                _ = try configuration(readPolicy())
                print("jStack Network: policy and signature verified")
                return
            }
            // Process does not promise to kill children when its parent dies.
            // Handle launchd's SIGTERM and let serve's bounded cleanup finish.
            signal(SIGTERM, SIG_IGN)
            signal(SIGINT, SIG_IGN)
            let signals = [SIGTERM, SIGINT].map { number in
                let source = DispatchSource.makeSignalSource(signal: number, queue: .global())
                source.setEventHandler { shutdownState.request() }
                source.resume()
                return source
            }
            defer { signals.forEach { $0.cancel() } }
            while !shutdownState.requested {
                let policy = try readPolicy()
                if policy.active { try serve(policy) }
                Thread.sleep(forTimeInterval: 1)
            }
        } catch NetworkFailure.invalid(let reason) {
            fputs("jStack Network: \(reason)\n", stderr)
            exit(1)
        } catch {
            fputs("jStack Network: failed secure validation or operation\n", stderr)
            exit(1)
        }
    }
}
