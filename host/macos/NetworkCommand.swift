import Foundation
import Darwin

enum NetworkCommandFailure: Error { case setup, timeout, cancelled, input, output, observation }

private func closeCommandFD(_ descriptor: inout Int32) {
    if descriptor >= 0 { close(descriptor); descriptor = -1 }
}

private func commandPipe() throws -> [Int32] {
    var descriptors: [Int32] = [-1, -1]
    guard pipe(&descriptors) == 0 else { throw NetworkCommandFailure.setup }
    for descriptor in descriptors {
        guard descriptor > STDERR_FILENO,
              fcntl(descriptor, F_SETFD, FD_CLOEXEC) == 0 else {
            descriptors.forEach { close($0) }
            throw NetworkCommandFailure.setup
        }
    }
    return descriptors
}

// Both pipe IO and child lifetime share one monotonic deadline. posix_spawn
// retains launchd's process group, so a killed supervisor leaves no detached
// wg command. Sensitive input stays in a pipe; it never touches a temp file.
func runNetworkCommand(_ executable: String, _ arguments: [String], input: Data? = nil,
                       captureOutput: Bool = false, seconds: TimeInterval = 30,
                       cancelled: () -> Bool = { false }) throws -> (status: Int32, output: Data) {
    let deadline = ProcessInfo.processInfo.systemUptime + seconds
    var incoming: [Int32] = [-1, -1]
    var outgoing: [Int32] = [-1, -1]
    defer {
        for index in 0...1 { closeCommandFD(&incoming[index]); closeCommandFD(&outgoing[index]) }
    }
    if input != nil { incoming = try commandPipe() }
    if captureOutput { outgoing = try commandPipe() }
    if incoming[1] >= 0 {
        guard fcntl(incoming[1], F_SETFL, O_NONBLOCK) == 0,
              fcntl(incoming[1], F_SETNOSIGPIPE, 1) == 0 else { throw NetworkCommandFailure.setup }
    }
    if outgoing[0] >= 0 && fcntl(outgoing[0], F_SETFL, O_NONBLOCK) != 0 { throw NetworkCommandFailure.setup }
    var actions: posix_spawn_file_actions_t?
    guard posix_spawn_file_actions_init(&actions) == 0 else { throw NetworkCommandFailure.setup }
    defer { posix_spawn_file_actions_destroy(&actions) }
    for (target, source, mode) in [(STDIN_FILENO, incoming[0], O_RDONLY),
                                   (STDOUT_FILENO, outgoing[1], O_WRONLY),
                                   (STDERR_FILENO, Int32(-1), O_WRONLY)] {
        let result = source >= 0 ? posix_spawn_file_actions_adddup2(&actions, source, target)
            : posix_spawn_file_actions_addopen(&actions, target, "/dev/null", mode, 0)
        guard result == 0 else { throw NetworkCommandFailure.setup }
    }
    for descriptor in incoming + outgoing where descriptor >= 0 {
        guard posix_spawn_file_actions_addclose(&actions, descriptor) == 0 else { throw NetworkCommandFailure.setup }
    }
    let argv = ([executable] + arguments).map { $0.withCString { strdup($0) } } + [nil]
    let environment = [strdup("PATH=/usr/bin:/bin:/usr/sbin:/sbin"), nil]
    defer { argv.forEach { free($0) }; environment.forEach { free($0) } }
    var child: pid_t = 0
    guard posix_spawn(&child, executable, &actions, nil, argv, environment) == 0 else { throw NetworkCommandFailure.setup }
    closeCommandFD(&incoming[0])
    closeCommandFD(&outgoing[1])
    var reaped = false
    var status: Int32 = 0
    defer {
        if !reaped {
            kill(child, SIGKILL)
            while waitpid(child, &status, 0) < 0 && errno == EINTR {}
        }
    }
    var sent = 0
    var output = Data()
    var buffer = [UInt8](repeating: 0, count: 8192)
    while true {
        if cancelled() { throw NetworkCommandFailure.cancelled }
        guard ProcessInfo.processInfo.systemUptime < deadline else { throw NetworkCommandFailure.timeout }
        if let input, incoming[1] >= 0 {
            if sent < input.count {
                let count = input.withUnsafeBytes { bytes in
                    Darwin.write(incoming[1], bytes.baseAddress!.advanced(by: sent), min(65536, input.count - sent))
                }
                if count > 0 { sent += count }
                else if count < 0 && errno != EAGAIN && errno != EINTR { throw NetworkCommandFailure.input }
            }
            if sent == input.count { closeCommandFD(&incoming[1]) }
        }
        if outgoing[0] >= 0 {
            while true {
                let count = buffer.withUnsafeMutableBytes { read(outgoing[0], $0.baseAddress, $0.count) }
                if count > 0 {
                    guard output.count + count <= 65536 else { throw NetworkCommandFailure.output }
                    output.append(contentsOf: buffer.prefix(count))
                } else if count == 0 { closeCommandFD(&outgoing[0]); break }
                else if errno == EAGAIN || errno == EINTR { break }
                else { throw NetworkCommandFailure.output }
            }
        }
        if !reaped {
            let result = waitpid(child, &status, WNOHANG)
            if result == child { reaped = true }
            else if result < 0 && errno != EINTR {
                // Do not signal a possibly reused PID after another observer
                // has already reaped the child.
                if errno == ECHILD { reaped = true }
                throw NetworkCommandFailure.observation
            }
        }
        if reaped && outgoing[0] < 0 {
            guard input == nil || sent == input!.count else { throw NetworkCommandFailure.input }
            let code = status & 0x7f == 0 ? (status >> 8) & 0xff : 128 + (status & 0x7f)
            return (code, output)
        }
        Thread.sleep(forTimeInterval: 0.01)
    }
}
