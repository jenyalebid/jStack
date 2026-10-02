import Foundation
import Darwin

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
