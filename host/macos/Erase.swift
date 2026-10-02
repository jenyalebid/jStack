import Foundation
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

// Pin every ancestor by descriptor. A user swapping a directory for a symlink
// during a root wipe must not redirect a later unlink into another tree.
func erase(_ parent: Int32, _ name: String, _ device: dev_t) throws {
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
            if child != "." && child != ".." { try erase(descriptor, child, device) }
        }
        guard unlinkat(parent, name, AT_REMOVEDIR) == 0 || errno == ENOENT else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
    } else {
        guard unlinkat(parent, name, 0) == 0 || errno == ENOENT else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
    }
}

do {
    guard CommandLine.arguments.count == 2 else { throw POSIXError(.EINVAL) }
    let path = CommandLine.arguments[1]
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
        if next < 0 && errno == ENOENT { exit(0) }
        guard next >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        close(parent)
        parent = next
    }
    var metadata = stat()
    guard fstat(parent, &metadata) == 0 else { throw POSIXError(.EIO) }
    try erase(parent, components.last!, metadata.st_dev)
} catch {
    fputs("SOS removal incomplete: \(error)\n", stderr)
    exit(1)
}
