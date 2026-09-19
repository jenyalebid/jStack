import Foundation
import Security
import Darwin

// Root-side watchdog for a Hub bricked mid-self-update. The Hub's updater
// replaces the bundle it runs from; if it dies between renames, launchd
// cannot relaunch it from the broken bundle and no user-level process can
// recover. Restoration happens only when ALL of these hold: the update
// journal is stale in applying/verifying, the Hub bundle is missing or fails
// its signed requirement, and the updater has no running process. A Hub that
// verifies, or an updater still alive, is never touched, and nothing is ever
// registered — the user's own launchd registrations relaunch the restored
// bundle.
struct HubRecoveryPolicy: Decodable {
    let owner: UInt32
    let bundle: String
    let state: String
}

let hubRecoveryPolicyURL = URL(fileURLWithPath: "/Library/Preferences/live.jstack.hub.recovery.json")
let hubRequirement = "anchor apple generic and certificate leaf[subject.OU] = \"MZ95H77RQQ\" and identifier \"live.jstack.hub\""
let hubJournalStaleAfter: TimeInterval = 900

private var lastRecoveryLog = ""
private func recoveryLog(_ message: String) {
    if message == lastRecoveryLog { return }
    lastRecoveryLog = message
    fputs("jStack Network: hub recovery: \(message)\n", stderr)
}

func readHubRecoveryPolicy() throws -> HubRecoveryPolicy? {
    var value = stat()
    if lstat(hubRecoveryPolicyURL.path, &value) != 0 { return nil }
    try rootProtected(hubRecoveryPolicyURL)
    let policy = try JSONDecoder().decode(HubRecoveryPolicy.self, from: Data(contentsOf: hubRecoveryPolicyURL))
    guard policy.owner != 0, policy.bundle.hasPrefix("/"), policy.bundle.hasSuffix(".app"),
          policy.state.hasPrefix("/") else {
        throw NetworkFailure.invalid("invalid hub recovery policy")
    }
    return policy
}

private struct HubJournal: Decodable { let state: String? }

private func userFile(_ path: String, owner: UInt32) throws -> (data: Data, modified: TimeInterval)? {
    let descriptor = open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
    if descriptor < 0 { return nil }
    defer { close(descriptor) }
    var metadata = stat()
    guard fstat(descriptor, &metadata) == 0, metadata.st_uid == owner,
          metadata.st_mode & S_IFMT == S_IFREG, metadata.st_size <= 1_048_576 else {
        throw NetworkFailure.invalid("hub journal ownership rejected")
    }
    let handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: false)
    let data = try handle.read(upToCount: 1_048_577) ?? Data()
    guard data.count <= 1_048_576 else { throw NetworkFailure.invalid("hub journal ownership rejected") }
    return (data, TimeInterval(metadata.st_mtimespec.tv_sec))
}

private func hubVerifies(_ path: String) -> Bool {
    var code: SecStaticCode?
    var requirement: SecRequirement?
    guard SecStaticCodeCreateWithPath(URL(fileURLWithPath: path) as CFURL, [], &code) == errSecSuccess,
          SecRequirementCreateWithString(hubRequirement as CFString, [], &requirement) == errSecSuccess,
          let code, let requirement else { return false }
    return SecStaticCodeCheckValidity(code, SecCSFlags(rawValue: kSecCSCheckAllArchitectures | kSecCSCheckNestedCode | kSecCSStrictValidate), requirement) == errSecSuccess
}

// Unobservable means assume alive: recovery holds off rather than racing a
// possibly live updater it failed to see.
private func updaterRunning(_ owner: UInt32) -> Bool {
    guard let result = try? runNetworkCommand("/bin/launchctl", ["print", "gui/\(owner)/live.jstack.hub.updater"],
                                              captureOutput: true, cancelled: { shutdownState.requested }) else {
        return true
    }
    guard result.status == 0 else { return false }
    guard let text = String(data: result.output, encoding: .utf8) else { return true }
    return text.contains("state = running")
}

private func restoreHub(_ policy: HubRecoveryPolicy) throws {
    let bundleURL = URL(fileURLWithPath: policy.bundle)
    let parent = bundleURL.deletingLastPathComponent()
    let name = bundleURL.lastPathComponent
    let entries = (try? FileManager.default.contentsOfDirectory(atPath: parent.path)) ?? []
    let backups = entries.filter { $0.hasPrefix(name + ".previous-") }.sorted()
    guard backups.count == 1, let backup = backups.first else {
        throw NetworkFailure.invalid(backups.isEmpty ? "no retained hub backup to restore"
                                                     : "ambiguous retained hub backups")
    }
    let backupURL = parent.appendingPathComponent(backup)
    var info = stat()
    guard lstat(backupURL.path, &info) == 0, info.st_mode & S_IFMT == S_IFDIR else {
        throw NetworkFailure.invalid("retained hub backup is not a bundle")
    }
    guard hubVerifies(backupURL.path) else {
        throw NetworkFailure.invalid("retained hub backup fails the signed requirement")
    }
    var aside: String?
    if lstat(bundleURL.path, &info) == 0 {
        let failed = parent.appendingPathComponent(name + ".failed-recovery")
        var probe = stat()
        guard lstat(failed.path, &probe) != 0 else {
            throw NetworkFailure.invalid("earlier failed hub candidate requires review")
        }
        guard rename(bundleURL.path, failed.path) == 0 else {
            throw NetworkFailure.invalid("cannot retire the broken hub bundle")
        }
        aside = failed.lastPathComponent
    }
    guard rename(backupURL.path, bundleURL.path) == 0 else {
        throw NetworkFailure.invalid("cannot restore the retained hub backup")
    }
    recoveryLog("restored \(backup) to \(policy.bundle)")
    // The marker tells the relaunched updater a root-side restore happened;
    // its absence must never undo one, so failure here only logs.
    var marker: [String: Any] = ["schema": 1, "restored": backup,
                                 "reason": "stale update journal with unverifiable hub and stopped updater",
                                 "at": ISO8601DateFormatter().string(from: Date())]
    if let aside { marker["failed"] = aside }
    let markerURL = URL(fileURLWithPath: policy.state).appendingPathComponent("updates/recovery.json")
    let temporary = markerURL.deletingLastPathComponent().appendingPathComponent(".recovery.json." + UUID().uuidString)
    do {
        let data = try JSONSerialization.data(withJSONObject: marker, options: [.sortedKeys])
        try data.write(to: temporary)
        _ = chmod(temporary.path, 0o644)
        _ = chown(temporary.path, uid_t(policy.owner), gid_t(bitPattern: Int32(-1)))
        guard rename(temporary.path, markerURL.path) == 0 else { throw NetworkFailure.invalid("marker") }
    } catch {
        try? FileManager.default.removeItem(at: temporary)
        recoveryLog("restored hub but could not record the recovery marker")
    }
}

func hubRecoveryTick() {
    do {
        guard let policy = try readHubRecoveryPolicy() else { return }
        guard let journal = try userFile(policy.state + "/updates/job.json", owner: policy.owner) else { return }
        let decoded = try? JSONDecoder().decode(HubJournal.self, from: journal.data)
        guard let state = decoded?.state, state == "applying" || state == "verifying" else { return }
        guard Date().timeIntervalSince1970 - journal.modified >= hubJournalStaleAfter else { return }
        var info = stat()
        if lstat(policy.state + "/updates/recovery.json", &info) == 0,
           TimeInterval(info.st_mtimespec.tv_sec) >= journal.modified {
            return  // This stall was already acted on; the updater owes the next move.
        }
        if hubVerifies(policy.bundle) { return }
        if updaterRunning(policy.owner) { return }
        try restoreHub(policy)
    } catch NetworkFailure.invalid(let reason) {
        recoveryLog(reason)
    } catch {
        recoveryLog("recovery observation failed")
    }
}

func hubRecoveryLoop() {
    var pause = 0
    while !shutdownState.requested {
        if pause <= 0 { hubRecoveryTick(); pause = 30 }
        pause -= 1
        Thread.sleep(forTimeInterval: 1)
    }
}
