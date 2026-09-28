import AppKit
import ApplicationServices
import Darwin

// Windows on this Mac's desk, read and moved through the Accessibility API
// directly — no Apple events, no System Events, no scripting bridge.
//
// Why this binary exists at all: macOS bills a privacy request to the
// *responsible process* of the session that made it, never to whoever holds
// the credential. Anything spawned by sshd is responsible to the ssh session
// binary, so a window verb run over ssh asks for — and is denied — a grant on
// `sshd-keygen-wrapper` / `com.apple.sshd-session`, whatever the Hub holds
// (jStack#239). Run from inside the sealed bundle's own services, the
// responsible process is jStack Hub, which is the one thing on this machine
// the user ever granted. So the capability lives here, and the parent asks the
// leaf's Hub to run it rather than reaching across a shell.
//
// Everything is typed. There is no verb that takes a script, a selector or an
// executable: a caller names a process, a window index and the title it read,
// and gets a refusal if any of the three moved underneath it. A capability
// that guesses which rectangle it is acting on is a capability that eventually
// closes the wrong one.

let USAGE = """
usage: JStackWindows trust | authorize | list
       JStackWindows minimize|unminimize|raise <pid> <index> <exact title>
       JStackWindows restore <index> <exact dock title>
       JStackWindows hide|unhide <pid>
"""

// Exit codes the Python side maps back to a grade, so a refusal is never read
// as a crash: 64 usage, 69 gone, 70 refused, 77 privileged, 78 untrusted,
// 79 responsible to something other than the Hub.
func fail(_ code: Int32, _ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code)
}

func emit(_ value: Any) {
    guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) else {
        fail(70, "could not serialize the answer")
    }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

// ── who this process is responsible to ──────────────────────────────────────

func parentOf(_ pid: pid_t) -> pid_t {
    var info = kinfo_proc()
    var size = MemoryLayout<kinfo_proc>.stride
    var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_PID, pid]
    guard sysctl(&mib, 4, &info, &size, nil, 0) == 0, size > 0 else { return 0 }
    return info.kp_eproc.e_ppid
}

/// The executable path of a live process. `proc_pidpath` is the direct answer
/// and simply fails for some processes this Mac runs (the Hub's own bundled
/// tmux among them), so the argument vector — the same place `ps` reads a
/// command from — is the fallback. An unreadable ancestor must never silently
/// read as "not under the Hub".
func pathOf(_ pid: pid_t) -> String {
    var buffer = [CChar](repeating: 0, count: 4096)
    if proc_pidpath(pid, &buffer, UInt32(buffer.count)) > 0 { return String(cString: buffer) }
    var size = 0
    var mib: [Int32] = [CTL_KERN, KERN_PROCARGS2, pid]
    guard sysctl(&mib, 3, nil, &size, nil, 0) == 0, size > MemoryLayout<Int32>.size else { return "" }
    var arguments = [CChar](repeating: 0, count: size)
    guard sysctl(&mib, 3, &arguments, &size, nil, 0) == 0 else { return "" }
    // KERN_PROCARGS2 is `int argc` followed by the NUL-terminated exec path.
    return String(cString: Array(arguments[MemoryLayout<Int32>.size...]))
}

/// The bundle identifier of the `.app` a path runs out of, "" for anything else.
func bundleOf(_ path: String) -> String {
    var url = URL(fileURLWithPath: path)
    while url.path != "/" {
        if url.pathExtension == "app" {
            return Bundle(url: url)?.bundleIdentifier ?? ""
        }
        url = url.deletingLastPathComponent()
    }
    return ""
}

/// This process and every ancestor up to launchd, each with the bundle it runs
/// out of. TCC's answer is a property of this chain, so the chain is reported
/// with every answer rather than described in a message nobody can check.
func ancestry() -> [[String: Any]] {
    var chain: [[String: Any]] = []
    var pid = getpid()
    while pid > 1 && chain.count < 32 {
        let path = pathOf(pid)
        chain.append(["pid": Int(pid), "path": path, "bundle": bundleOf(path)])
        pid = parentOf(pid)
    }
    return chain
}

//: The one permission holder on a jStack machine. Every grant this capability
//: spends belongs to it and to nothing else. The privileged network daemon
//: (`live.jstack.network`) is deliberately not here: it runs as root, a window
//: verb never may, and a second bundle holding a desk grant is a second holder.
let HOLDER = "live.jstack.hub"

/// Whether this binary is running out of the sealed Hub bundle. Read from
/// `Bundle.main`, which is the bundle around this executable — a fact the
/// code signature covers, not a claim a caller can dress up.
func sealed() -> Bool { Bundle.main.bundleIdentifier == HOLDER }

//: The ssh session binaries. macOS bills a privacy request to the responsible
//: process of the session, so anything below one of these asks for a grant on
//: *sshd*, whatever the Hub holds — the exact prompt jStack#239 was filed for.
let SSH_SESSION: Set<String> = ["sshd", "sshd-session", "sshd-keygen-wrapper"]

/// Whether an ssh session sits anywhere above this process.
func overSSH() -> Bool {
    ancestry().contains { SSH_SESSION.contains(URL(fileURLWithPath: ($0["path"] as? String) ?? "").lastPathComponent) }
}

/// Whether the Hub is an ancestor of this process — the single fact that
/// decides *whose* grant macOS is about to consult. Without it, a trust answer
/// describes whatever terminal or ssh session happens to be above this binary,
/// which is a different machine-level claim wearing the same words. Every
/// report carries it so no reader has to assume.
func hubSession() -> Bool {
    ancestry().contains { ($0["bundle"] as? String) == HOLDER }
}

// ── the Accessibility side ──────────────────────────────────────────────────

func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success ? value : nil
}

func settable(_ element: AXUIElement, _ name: String) -> Bool {
    var answer: DarwinBoolean = false
    return AXUIElementIsAttributeSettable(element, name as CFString, &answer) == .success && answer.boolValue
}

/// A CGPoint or CGSize attribute as plain numbers — which display a window is
/// on is the one fact a person checks a window claim against.
func geometry(_ element: AXUIElement, _ name: String) -> [String: Double]? {
    guard let value = attribute(element, name), CFGetTypeID(value) == AXValueGetTypeID() else { return nil }
    let boxed = unsafeBitCast(value, to: AXValue.self)
    switch AXValueGetType(boxed) {
    case .cgPoint:
        var point = CGPoint.zero
        guard AXValueGetValue(boxed, .cgPoint, &point) else { return nil }
        return ["x": Double(point.x), "y": Double(point.y)]
    case .cgSize:
        var size = CGSize.zero
        guard AXValueGetValue(boxed, .cgSize, &size) else { return nil }
        return ["width": Double(size.width), "height": Double(size.height)]
    default:
        return nil
    }
}

/// Read a state back until it agrees with what was asked, or give up.
///
/// An AX setter returns when the application has *received* the message, not
/// when the window has moved; reading immediately reports the state before the
/// change. On a real desk that produced "minimized: false" from the call that
/// minimized the window and "minimized: true" from the call that restored it —
/// both exactly backwards, and both would have been reported as the outcome.
func settle(_ wanted: Bool, _ read: () -> Bool?) -> Bool {
    let deadline = Date().addingTimeInterval(3)
    while Date() < deadline {
        if read() == wanted { return true }
        usleep(50_000)
    }
    return read() == wanted
}

/// Every minimized window the Dock is holding, in Dock order.
///
/// This is not a convenience. Some applications drop a window out of their own
/// `AXWindows` the moment it is minimized — Notes does — so the Dock is the
/// only place that window still exists and the only handle that can restore
/// it. A capability that can minimize and not restore is a switch with no way
/// back, which is worse than not having the verb.
///
/// The Dock does not say whose window each item is: the item's parent is the
/// Dock itself and there is no owning-application attribute (read off the live
/// element, not assumed). So these are reported unattributed, by title, rather
/// than guessed onto a process.
func dockMinimized() -> [(element: AXUIElement, title: String)] {
    guard let dock = NSRunningApplication.runningApplications(
        withBundleIdentifier: "com.apple.dock").first else { return [] }
    let root = AXUIElementCreateApplication(dock.processIdentifier)
    var found: [(element: AXUIElement, title: String)] = []
    for list in (attribute(root, kAXChildrenAttribute as String) as? [AXUIElement]) ?? [] {
        for item in (attribute(list, kAXChildrenAttribute as String) as? [AXUIElement]) ?? [] {
            guard attribute(item, kAXSubroleAttribute as String) as? String
                    == "AXMinimizedWindowDockItem" else { continue }
            found.append((item, attribute(item, kAXTitleAttribute as String) as? String ?? ""))
        }
    }
    return found
}

func policyName(_ policy: NSApplication.ActivationPolicy) -> String {
    switch policy {
    case .regular: return "regular"
    case .accessory: return "accessory"
    case .prohibited: return "prohibited"
    @unknown default: return "unknown"
    }
}

/// TCC is asked, never read. `AXIsProcessTrusted` is the non-prompting form of
/// the same question the API itself answers, so a true here and a working verb
/// are one fact — there is no grant table to consult and no second opinion to
/// have. What the refusal has to carry is *why*, because the overwhelmingly
/// common false is not a missing grant at all: it is a grant the Hub holds,
/// asked for by a process macOS holds sshd responsible for.
func requireTrust() {
    guard !AXIsProcessTrusted() else { return }
    if overSSH() {
        fail(79, "this process runs under an ssh session, so macOS bills its window "
               + "grant to sshd — not to jStack Hub, whatever the Hub holds. Ask the "
               + "Hub that owns this machine (POST /windows/act), never a shell on it.")
    }
    guard hubSession() else {
        let under = ancestry().map { ($0["path"] as? String) ?? "?" }.joined(separator: " ← ")
        fail(79, "jStack Hub is not an ancestor of this process (\(under)), so macOS "
               + "consulted that session's grant rather than the Hub's. Ask the Hub, "
               + "not a shell — this says nothing about what the Hub holds.")
    }
    fail(78, "jStack Hub does not hold Accessibility on this Mac — "
           + "run `jstack-host windows authorize` to ask for it under the Hub")
}

func windowsOf(_ pid: pid_t) -> [AXUIElement] {
    let application = AXUIElementCreateApplication(pid)
    return attribute(application, kAXWindowsAttribute as String) as? [AXUIElement] ?? []
}

func describe(_ window: AXUIElement, _ index: Int) -> [String: Any] {
    var row: [String: Any] = [
        "index": index,
        "title": attribute(window, kAXTitleAttribute as String) as? String ?? "",
        "minimized": (attribute(window, kAXMinimizedAttribute as String) as? Bool) ?? false,
        "minimizable": settable(window, kAXMinimizedAttribute as String)]
    if let position = geometry(window, kAXPositionAttribute as String) { row["position"] = position }
    if let size = geometry(window, kAXSizeAttribute as String) { row["size"] = size }
    return row
}

func listWindows() -> [String: Any] {
    var apps: [[String: Any]] = []
    for app in NSWorkspace.shared.runningApplications {
        let windows = windowsOf(app.processIdentifier)
        if windows.isEmpty { continue }
        apps.append([
            "pid": Int(app.processIdentifier),
            "name": app.localizedName ?? "",
            "bundle": app.bundleIdentifier ?? "",
            "policy": policyName(app.activationPolicy),
            "hidden": app.isHidden,
            "windows": windows.enumerated().map { describe($1, $0) }])
    }
    // `minimized` is a second, unattributed list on purpose: a window that is
    // in the Dock and not in its application's own window list is not missing,
    // and a caller that only read `apps` would conclude it was.
    return ["apps": apps.sorted { ($0["name"] as! String).lowercased() < ($1["name"] as! String).lowercased() },
            "minimized": dockMinimized().enumerated().map {
                ["index": $0.offset, "title": $0.element.title] }]
}

/// The window a caller named, or a refusal. Three facts must still agree —
/// the process, the index, and the title the caller read off `list`. A window
/// list reorders the moment a window opens or closes, so acting on an index
/// alone eventually acts on the wrong rectangle; the title is what makes a
/// stale instruction a refusal instead of a surprise.
func namedWindow(_ pid: pid_t, _ index: Int, _ title: String) -> AXUIElement {
    guard NSRunningApplication(processIdentifier: pid) != nil else {
        fail(69, "no running process with pid \(pid)")
    }
    let windows = windowsOf(pid)
    guard index >= 0 && index < windows.count else {
        let held = dockMinimized().map { $0.title }
        fail(69, "pid \(pid) has \(windows.count) window(s); there is no window \(index)"
               + (held.isEmpty ? "" : ". The Dock is holding \(held.joined(separator: ", ")) — "
                                    + "some applications drop a minimized window out of their own "
                                    + "list, and `restore` is the handle for those"))
    }
    let window = windows[index]
    let current = attribute(window, kAXTitleAttribute as String) as? String ?? ""
    guard current == title else {
        fail(69, "window \(index) of pid \(pid) is now titled \(current.isEmpty ? "(untitled)" : current), "
               + "not \(title.isEmpty ? "(untitled)" : title) — read `list` again")
    }
    return window
}

func setMinimized(_ window: AXUIElement, _ wanted: Bool) -> [String: Any] {
    guard settable(window, kAXMinimizedAttribute as String) else {
        fail(70, "this window does not accept being \(wanted ? "minimized" : "restored")")
    }
    let inDockBefore = dockMinimized().count
    let result = AXUIElementSetAttributeValue(window, kAXMinimizedAttribute as CFString, wanted as CFBoolean)
    guard result == .success else {
        fail(70, "the application refused the change (AXError \(result.rawValue))")
    }
    if settle(wanted, { attribute(window, kAXMinimizedAttribute as String) as? Bool }) {
        return ["minimized": wanted, "ok": true, "via": "window"]
    }
    // The window stopped answering. For an application that drops a minimized
    // window out of its own list that is the success case, and the Dock is
    // where the proof is — so it is checked, not assumed either way.
    let answering = attribute(window, kAXMinimizedAttribute as String) != nil
    if wanted && !answering && dockMinimized().count > inDockBefore {
        return ["minimized": true, "ok": true, "via": "dock",
                "note": "this application stops listing a window once it is minimized; "
                      + "`restore` is what brings it back"]
    }
    return ["minimized": !wanted, "ok": false,
            "error": answering
                ? "the application accepted the change and the window did not move"
                : "the window stopped answering and no minimized window appeared in the Dock"]
}

/// Bring a window back from the Dock. Named by its Dock title, which is the
/// only identity macOS gives these — and a shorter string than the window's
/// own title, which is why `list` reports both lists separately instead of
/// pretending one index space covers them.
func restoreFromDock(_ index: Int, _ title: String) -> [String: Any] {
    let items = dockMinimized()
    guard index >= 0 && index < items.count else {
        fail(69, "the Dock is holding \(items.count) minimized window(s); there is no \(index)")
    }
    guard items[index].title == title else {
        fail(69, "minimized window \(index) is now titled \(items[index].title.isEmpty ? "(untitled)" : items[index].title), "
               + "not \(title.isEmpty ? "(untitled)" : title) — read `list` again")
    }
    let result = AXUIElementPerformAction(items[index].element, kAXPressAction as CFString)
    guard result == .success else {
        fail(70, "the Dock refused the press (AXError \(result.rawValue))")
    }
    let left = settle(true) { dockMinimized().count < items.count }
    return ["ok": left, "restored": title,
            "error": left ? "" : "the window is still in the Dock"]
}

func raiseWindow(_ window: AXUIElement) -> [String: Any] {
    let result = AXUIElementPerformAction(window, kAXRaiseAction as CFString)
    return ["ok": result == .success, "error": result == .success ? "" : "AXError \(result.rawValue)"]
}

func setHidden(_ pid: pid_t, _ wanted: Bool) -> [String: Any] {
    guard let app = NSRunningApplication(processIdentifier: pid) else {
        fail(69, "no running process with pid \(pid)")
    }
    // `hide()` is an application-level request and an accessory or background
    // application has nothing to hide — it answers false, and the window the
    // caller meant stays exactly where it was. Say which happened.
    let accepted = wanted ? app.hide() : app.unhide()
    _ = settle(wanted) { NSRunningApplication(processIdentifier: pid)?.isHidden }
    let observed = NSRunningApplication(processIdentifier: pid)?.isHidden ?? false
    var row: [String: Any] = ["hidden": observed, "ok": observed == wanted,
                              "policy": policyName(app.activationPolicy)]
    if observed != wanted {
        row["error"] = accepted
            ? "the application accepted the request and stayed \(observed ? "hidden" : "visible")"
            : "a \(policyName(app.activationPolicy)) application has no hideable presence; "
              + "minimize its window instead"
    }
    return row
}

// ── verbs ───────────────────────────────────────────────────────────────────

func trustReport(prompting: Bool) -> [String: Any] {
    let before = AXIsProcessTrusted()
    var prompted = false
    if prompting && !before {
        let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
        _ = AXIsProcessTrustedWithOptions([key: true] as CFDictionary)
        prompted = true
    }
    return ["trusted": before, "prompted": prompted, "sealed": sealed(),
            "hub_session": hubSession(), "over_ssh": overSSH(),
            "bundle": Bundle.main.bundleIdentifier ?? "", "ancestry": ancestry()]
}

func main() {
    guard geteuid() != 0 else { fail(77, "window verbs must never run privileged") }
    let args = Array(CommandLine.arguments.dropFirst())
    guard let verb = args.first else { fail(64, USAGE) }

    switch (verb, args.count) {
    case ("trust", 1):
        emit(trustReport(prompting: false))
    case ("authorize", 1):
        // The one place a TCC dialog is ever raised. It names whichever bundle
        // is responsible for this process, so it is refused outright anywhere
        // but under the Hub — a prompt naming the wrong holder is the defect,
        // not a step towards fixing it.
        //
        // A machine that already answered the dialog answers here too, with no
        // prompt and no guards to satisfy: that is what makes this safe for a
        // doctor rung and a route to call, rather than a thing a caller can
        // flash at whoever is sitting at the Mac.
        if hubSession() && AXIsProcessTrusted() {
            emit(trustReport(prompting: false))
            return
        }
        guard !overSSH() else {
            fail(79, "refusing to raise a permission dialog from under an ssh session — "
                   + "it would name sshd. Ask the Hub that owns this machine.")
        }
        guard sealed() && hubSession() else {
            let under = ancestry().map { ($0["path"] as? String) ?? "?" }.joined(separator: " ← ")
            fail(79, "refusing to raise a permission dialog from outside the Hub's own "
                   + "session (\(under)): the dialog names whichever bundle macOS holds "
                   + "responsible, and a grant on anything but the Hub is the defect this "
                   + "capability exists to end, not a step towards fixing it")
        }
        emit(trustReport(prompting: true))
    case ("list", 1):
        requireTrust()
        emit(listWindows())
    case ("minimize", 4), ("unminimize", 4), ("raise", 4):
        requireTrust()
        guard let pid = pid_t(args[1]), let index = Int(args[2]) else { fail(64, USAGE) }
        let window = namedWindow(pid, index, args[3])
        emit(verb == "raise" ? raiseWindow(window) : setMinimized(window, verb == "minimize"))
    case ("restore", 3):
        requireTrust()
        guard let index = Int(args[1]) else { fail(64, USAGE) }
        emit(restoreFromDock(index, args[2]))
    case ("hide", 2), ("unhide", 2):
        requireTrust()
        guard let pid = pid_t(args[1]) else { fail(64, USAGE) }
        emit(setHidden(pid, verb == "hide"))
    default:
        fail(64, USAGE)
    }
}

main()
