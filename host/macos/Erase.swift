import Foundation
import CoreFoundation
import Darwin

let workspace = "/private/var/db/live.jstack.sos"

func canonical(_ path: String) -> Bool {
    let parts = path.split(separator: "/").map(String.init)
    return path == "/" + parts.joined(separator: "/") &&
        !parts.contains(".") && !parts.contains("..") &&
        !path.contains("\0") && !path.contains("\n") && !path.contains("\r")
}

func requireRootProtection(_ fd: Int32) throws {
    var metadata = stat()
    guard fstat(fd, &metadata) == 0, metadata.st_uid == 0,
          metadata.st_mode & 0o022 == 0 else { throw POSIXError(.EPERM) }
    guard let acl = acl_get_fd_np(fd, ACL_TYPE_EXTENDED) else {
        if errno == ENOENT { return }
        throw POSIXError(.EIO)
    }
    defer { acl_free(UnsafeMutableRawPointer(acl)) }
    guard acl_valid(acl) == 0 else { throw POSIXError(.EIO) }
    var entry: acl_entry_t?
    // Darwin signals the end of a valid ACL with EINVAL, not Linux's zero.
    let result = acl_get_entry(acl, ACL_FIRST_ENTRY.rawValue, &entry)
    guard result == -1, errno == EINVAL else { throw POSIXError(.EPERM) }
}

// The helper has no arbitrary-path mode. Each invocation must match the exact
// inventory approved by the user and then staged in a root-only directory.
// Open the authorization through pinned descriptors, just like deletion.
func protectedFile(_ name: String) throws -> Data {
    var fd = open("/", O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
    guard fd >= 0 else { throw POSIXError(.EIO) }
    defer { close(fd) }
    for component in ["private", "var", "db", "live.jstack.sos", name] {
        try requireRootProtection(fd)
        let flags = component == name ? O_RDONLY | O_NOFOLLOW : O_RDONLY | O_DIRECTORY | O_NOFOLLOW
        let next = openat(fd, component, flags)
        guard next >= 0 else { throw POSIXError(.EPERM) }
        close(fd)
        fd = next
    }
    try requireRootProtection(fd)
    var metadata = stat()
    guard fstat(fd, &metadata) == 0, metadata.st_uid == 0,
          metadata.st_mode & S_IFMT == S_IFREG, metadata.st_mode & 0o077 == 0,
          metadata.st_size > 0, metadata.st_size <= 8 * 1024 * 1024 else { throw POSIXError(.EPERM) }
    let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: false)
    guard let data = try handle.readToEnd() else { throw POSIXError(.EIO) }
    return data
}

func authorize(_ path: String) throws {
    guard geteuid() == 0, canonical(path),
          let manifest = try JSONSerialization.jsonObject(with: protectedFile("manifest.json")) as? [String: Any],
          manifest["schema"] as? Int == 1,
          let home = manifest["home"] as? String, canonical(home),
          let history = manifest["history"] as? [String],
          let data = manifest["data"] as? [String],
          let apps = manifest["apps"] as? [String] else { throw POSIXError(.EPERM) }
    // Exact equality, never prefix containment. An approved child does not
    // authorize its parent or a sibling, including a whole external volume.
    let approved = history + data + apps
    guard approved.allSatisfy({ canonical($0) && $0 != home && !home.hasPrefix($0 + "/") }) else {
        throw POSIXError(.EPERM)
    }
    if path == workspace {
        guard try protectedFile("phase") == Data("complete\n".utf8) else { throw POSIXError(.EPERM) }
    } else {
        guard approved.contains(path), !path.hasPrefix(workspace + "/") else { throw POSIXError(.EPERM) }
    }
}

// Mixed agent/project trees contain both ordinary files and exported history.
// Treat logs, databases and opaque archives conservatively as possible history;
// recognize renamed native transcripts by their record tags. Never follow links
// or read special files, and never emit file contents into the wipe log.
func historyFile(_ parent: Int32, _ name: String, _ metadata: stat) throws -> Bool {
    let lower = name.lowercased()
    let suffixes = [".jsonl", ".ndjson", ".log", ".sqlite", ".sqlite3", ".db",
                    "-wal", "-shm", "-journal", ".zip", ".tar", ".gz", ".tgz",
                    ".bz2", ".xz", ".7z", ".zst", ".lz4"]
    if suffixes.contains(where: { lower.hasSuffix($0) }) { return true }
    guard metadata.st_mode & S_IFMT == S_IFREG else { return false }
    let descriptor = openat(parent, name, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
    guard descriptor >= 0 else { throw POSIXError(.EIO) }
    defer { close(descriptor) }
    var opened = stat()
    guard fstat(descriptor, &opened) == 0, opened.st_ino == metadata.st_ino,
          opened.st_dev == metadata.st_dev, opened.st_mode & S_IFMT == S_IFREG else {
        throw POSIXError(.ESTALE)
    }
    var bytes = [UInt8](repeating: 0, count: 65536)
    let count = read(descriptor, &bytes, bytes.count)
    guard count >= 0 else { throw POSIXError(.EIO) }
    let data = Data(bytes.prefix(count))
    let magic: [[UInt8]] = [[0x50, 0x4b, 0x03, 0x04], [0x1f, 0x8b],
                            [0x42, 0x5a, 0x68], [0xfd, 0x37, 0x7a, 0x58, 0x5a],
                            [0x37, 0x7a, 0xbc, 0xaf, 0x27, 0x1c], [0x28, 0xb5, 0x2f, 0xfd]]
    if magic.contains(where: { data.starts(with: $0) }) || data.starts(with: Data("SQLite format 3".utf8)) {
        return true
    }
    if count > 262 && data.subdata(in: 257..<262) == Data("ustar".utf8) { return true }
    let text = String(decoding: data, as: UTF8.self)
    return text.range(of: #""type"\s*:\s*"(session_meta|response_item|event_msg|user|assistant|file-history-snapshot|queue-operation)""#,
                      options: .regularExpression) != nil
}

let historyDirectories: Set<String> = [".claude", ".codex", ".git", "sessions", "archived_sessions",
                                       "transcripts", "session-exports", "subagents", "file-history"]

// Pin every ancestor by descriptor. A user swapping a directory for a symlink
// during a root wipe must not redirect a later unlink into another tree.
func erase(_ parent: Int32, _ name: String, _ device: dev_t, copiesOnly: Bool = false) throws {
    var metadata = stat()
    guard fstatat(parent, name, &metadata, AT_SYMLINK_NOFOLLOW) == 0 else {
        if errno == ENOENT { return }
        throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
    }
    guard metadata.st_dev == device else { throw POSIXError(.EXDEV) }
    if (metadata.st_mode & S_IFMT) == S_IFDIR {
        let descriptor = openat(parent, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
        guard descriptor >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        guard let directory = fdopendir(descriptor) else {
            close(descriptor)
            throw POSIXError(.EIO)
        }
        defer { closedir(directory) }
        var opened = stat()
        guard fstat(descriptor, &opened) == 0,
              opened.st_dev == metadata.st_dev, opened.st_ino == metadata.st_ino else {
            throw POSIXError(.ESTALE)
        }
        let filterChildren = copiesOnly && !historyDirectories.contains(name.lowercased())
        while true {
            errno = 0
            guard let entry = readdir(directory) else {
                if errno != 0 { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
                break
            }
            let child = withUnsafePointer(to: entry.pointee.d_name) {
                $0.withMemoryRebound(to: CChar.self, capacity: Int(entry.pointee.d_namlen) + 1) {
                    String(cString: $0)
                }
            }
            if child != "." && child != ".." { try erase(descriptor, child, device, copiesOnly: filterChildren) }
        }
        if filterChildren { return }
        guard unlinkat(parent, name, AT_REMOVEDIR) == 0 || errno == ENOENT else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
    } else {
        if copiesOnly {
            if try !historyFile(parent, name, metadata) { return }
        }
        guard unlinkat(parent, name, 0) == 0 || errno == ENOENT else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
    }
}

func removeTarget(_ path: String, copiesOnly: Bool) throws {
    try authorize(path)
    let components = path.split(separator: "/").map(String.init)
    guard path.hasPrefix("/"), components.count >= 2,
          !(components[0] == "Users" && components.count == 2),
          !["/private/var", "/private/etc", "/usr/local", "/opt/homebrew",
            "/Library/LaunchDaemons", "/Library/LaunchAgents",
            "/Library/PrivilegedHelperTools", "/Library/Application Support"].contains(path),
          !components.contains(".."), !components.contains(".") else { throw POSIXError(.EINVAL) }
    var parent = open("/", O_RDONLY | O_DIRECTORY)
    guard parent >= 0 else { throw POSIXError(.EIO) }
    defer { close(parent) }
    for component in components.dropLast() {
        let next = openat(parent, component, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
        if next < 0 && errno == ENOENT { return }
        guard next >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        close(parent)
        parent = next
    }
    var metadata = stat()
    guard fstat(parent, &metadata) == 0 else { throw POSIXError(.EIO) }
    try erase(parent, components.last!, metadata.st_dev, copiesOnly: copiesOnly)
}

func preferences(verifyOnly: Bool) throws {
    guard getuid() == 0, geteuid() == 0,
          let manifest = try JSONSerialization.jsonObject(with: protectedFile("manifest.json")) as? [String: Any],
          manifest["schema"] as? Int == 1,
          let value = manifest["uid"] as? Int, let uid = uid_t(exactly: value), uid != 0,
          let home = manifest["home"] as? String, canonical(home),
          let domains = manifest["preferences"] as? [String], !domains.isEmpty,
          domains.allSatisfy({ $0.range(of: #"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$"#,
                                       options: .regularExpression) != nil }),
          let account = getpwuid(uid), String(cString: account.pointee.pw_dir) == home else {
        throw POSIXError(.EPERM)
    }
    let name = String(cString: account.pointee.pw_name)
    let gid = account.pointee.pw_gid
    // The executable and authorization stay root-only. Load both before
    // dropping identity; CFPreferences must never use the root account.
    guard let group = Int32(exactly: gid), chdir("/") == 0,
          initgroups(name, group) == 0, setgid(gid) == 0, setuid(uid) == 0,
          getuid() == uid, geteuid() == uid,
          setenv("HOME", home, 1) == 0, setenv("USER", name, 1) == 0,
          setenv("LOGNAME", name, 1) == 0 else { throw POSIXError(.EPERM) }
    // Refuse redirected stores before asking cfprefsd to touch them. A failed
    // or unreadable store is not evidence of an empty preference domain.
    let base = home + "/Library/Preferences"
    for path in [home, home + "/Library", base, base + "/ByHost"] {
        var metadata = stat()
        if lstat(path, &metadata) != 0 {
            guard errno == ENOENT else { throw POSIXError(.EIO) }
        } else {
            guard metadata.st_mode & S_IFMT == S_IFDIR else { throw POSIXError(.EPERM) }
        }
    }
    let byHost = base + "/ByHost"
    let names = FileManager.default.fileExists(atPath: byHost)
        ? try FileManager.default.contentsOfDirectory(atPath: byHost) : []
    for domain in domains {
        let paths = [base + "/" + domain + ".plist"] + names.filter {
            $0.hasPrefix(domain + ".") && $0.hasSuffix(".plist")
        }.map { byHost + "/" + $0 }
        for path in paths {
            var metadata = stat()
            if lstat(path, &metadata) != 0 {
                guard errno == ENOENT else { throw POSIXError(.EIO) }
            } else {
                guard metadata.st_mode & S_IFMT == S_IFREG, metadata.st_uid == uid,
                      metadata.st_flags & UInt32(UF_IMMUTABLE | SF_IMMUTABLE) == 0,
                      access(path, R_OK | W_OK) == 0 else { throw POSIXError(.EPERM) }
            }
        }
    }
    for domain in domains {
        for host in [kCFPreferencesAnyHost, kCFPreferencesCurrentHost] {
            guard CFPreferencesSynchronize(domain as CFString, kCFPreferencesCurrentUser, host) else {
                throw POSIXError(.EIO)
            }
            if !verifyOnly {
                let keys = CFPreferencesCopyKeyList(domain as CFString, kCFPreferencesCurrentUser, host)
                CFPreferencesSetMultiple(nil, keys, domain as CFString, kCFPreferencesCurrentUser, host)
                guard CFPreferencesSynchronize(domain as CFString, kCFPreferencesCurrentUser, host) else {
                    throw POSIXError(.EIO)
                }
            }
            let keys = CFPreferencesCopyKeyList(domain as CFString, kCFPreferencesCurrentUser, host) as? [String]
            guard keys?.isEmpty ?? true else { throw POSIXError(.EEXIST) }
        }
    }
}

do {
    let arguments = Array(CommandLine.arguments.dropFirst())
    if arguments == ["--preferences"] || arguments == ["--verify-preferences"] {
        try preferences(verifyOnly: arguments[0] == "--verify-preferences")
    } else if arguments.first == "--history-copies" {
        guard arguments.count >= 2 else { throw POSIXError(.EINVAL) }
        // Validate every exact target before changing the first one.
        for path in arguments.dropFirst() { try authorize(path) }
        for path in arguments.dropFirst() { try removeTarget(path, copiesOnly: true) }
    } else {
        guard arguments.count == 1 else { throw POSIXError(.EINVAL) }
        try removeTarget(arguments[0], copiesOnly: false)
    }
} catch {
    fputs("SOS removal incomplete: \(error)\n", stderr)
    exit(1)
}
