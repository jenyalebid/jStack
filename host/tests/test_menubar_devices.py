"""Compile the actual menu's device policy without starting a desktop app."""
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import shutil
import subprocess
import sys
import threading

import pytest


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc"),
                    reason="the menu bar uses macOS AppKit")
@pytest.mark.parametrize("mode", ["open", "managed"])
def test_device_status_and_info_actions(tmp_path, mode):
    source = Path(__file__).resolve().parents[1] / "menubar" / "JStackHostBar.swift"
    main = tmp_path / "main.swift"
    main.write_text(source.read_text() + "\n" + r'''
import Foundation
let decoder = JSONDecoder()
decoder.keyDecodingStrategy = .convertFromSnakeCase
func session(_ json: String) throws -> Session {
    try decoder.decode(Session.self, from: Data(json.utf8))
}
let idle = try session(#"{"turn":"idle","live":true}"#)
let working = try session(#"{"turn":"working","live":false,"unread":true}"#)
let unread = try session(#"{"turn":"idle","unread":true}"#)
let waiting = try session(#"{"attention":"waiting","turn":"working"}"#)
let error = try session(#"{"attention":"error","unread":true}"#)
assert(idle.signal == .idle, "recent output must not overrule an observed idle turn")
assert(working.signal == .working, "a new turn supersedes its own old unread reply")
let legacyWorking = try session(#"{"live":true}"#)
assert(legacyWorking.signal == .working)
assert(SessionSignal.collective([]) == .idle)
assert(SessionSignal.collective([idle, working]) == .working)
assert(SessionSignal.collective([working, unread]) == .unread)
assert(SessionSignal.collective([unread, working, waiting]) == .attention)
assert(SessionSignal.collective([error, working]) == .attention)
let inventoryJSON = #"{"release":"client-or-stack-release","machines":[{"machine":"home","name":"Home","desired":"client-or-stack-release","state":"current","supervisor":true},{"machine":"leaf","name":"Leaf","desired":"client-or-stack-release","state":"available","supervisor":true}]}"#
var inventory = try decoder.decode(UpdateInventory.self, from: Data(inventoryJSON.utf8))
assert(inventory.localUpdate(hostID: "leaf")?.machine == "leaf")
assert(inventory.localUpdate(hostID: "home") == nil, "a leaf update isn't a local update")
assert(inventory.localUpdate(hostID: nil) == nil)
for state in ["pending", "pending/offline", "downloading", "applying", "verifying", "current"] {
    inventory.machines[1].state = state
    assert(inventory.localUpdate(hostID: "leaf") == nil, "duplicate update offered for \(state)")
}
inventory.machines[1].state = "available"
inventory.release = nil
assert(inventory.localUpdate(hostID: "leaf") == nil, "no release is not an available update")
func device(_ json: String) throws -> Device {
    try decoder.decode(Device.self, from: Data(json.utf8))
}
let live = try device(#"{"id":"live","name":"Phone","revoked":false}"#)
let dead = try device(#"{"id":"dead","name":"Old Tablet","revoked":true}"#)
let legacyDead = try device(#"{"id":"legacy","name":"Old","revoked_at":123}"#)
let internalHost = try device(#"{"id":"host-internal","name":"Host"}"#)
assert(DeviceMenu.active([dead, live, legacyDead, internalHost]).map(\.id) == ["live"])
assert(DeviceMenu.active([dead, legacyDead]).isEmpty)
assert(DeviceMenu.active([live], removed: ["live"]).isEmpty, "a stale poll resurrected a removal")
assert(DeviceMenu.removalSucceeded(status: 200, data: nil))
let removed = Data(#"{"detail":"unknown or already revoked device"}"#.utf8)
assert(DeviceMenu.removalSucceeded(status: 404, data: removed))
assert(!DeviceMenu.removalSucceeded(status: 403, data: removed))
assert(!DeviceMenu.removalSucceeded(status: 404, data: Data(#"{"detail":"Not Found"}"#.utf8)))
assert(!DeviceMenu.removalSucceeded(status: 0, data: nil))
assert(HostAgent.token() == "legacy-still-valid")
assert(HostAgent.updaterToken() == "jr1.host-internal.local-proof",
       "updates must never prefer a legacy token over local administrative authority")
print("device menu contract passed")

// Exercise real AppKit controls, including target/sender forwarding. No host
// request or update job is made by this fixture.
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
final class Receiver: NSObject {
    var targets: [String] = []
    @objc func update(_ sender: NSMenuItem) {
        targets.append(sender.representedObject as! String)
    }
}
let receiver = Receiver()
let details = NSMenu()
details.autoenablesItems = false
details.addItem(NSMenuItem(title: "No release available", action: nil, keyEquivalent: ""))
details.addItem(.separator())
let update = NSMenuItem(title: "Update Lab", action: #selector(Receiver.update), keyEquivalent: "")
update.target = receiver
update.representedObject = "lab"
update.setAccessibilityIdentifier("updates_tap_lab")
details.addItem(update)
let window = HostInfoWindow()
window.render(machine: "Lab Mac", status: "Port 9090 · open", details: details)
let scroll = window.contentView as! NSScrollView
let rows = scroll.documentView as! NSStackView
let labels = rows.arrangedSubviews.compactMap { ($0 as? NSTextField)?.stringValue }
assert(labels == ["Lab Mac", "Port 9090 · open", "No release available"])
let button = rows.arrangedSubviews.compactMap { $0 as? NSButton }.first!
assert(button.accessibilityIdentifier() == "updates_tap_lab")
button.performClick(nil)
assert(receiver.targets == ["lab"], "the window lost the update command's target")
window.render(machine: "Lab Mac", status: "Port 9090 · open", details: details)
assert(rows.arrangedSubviews.contains { $0 === button }, "unchanged polls reset keyboard focus")
update.isEnabled = false
window.render(machine: "Lab Mac", status: "Port 9090 · open", details: details)
let disabled = rows.arrangedSubviews.compactMap { $0 as? NSButton }.first!
assert(!disabled.isEnabled)
disabled.performClick(nil)
assert(receiver.targets == ["lab"], "an in-flight update was submitted twice")
let failure = NSMenu()
failure.addItem(NSMenuItem(title: "Update status unavailable (503)", action: nil, keyEquivalent: ""))
window.render(machine: "Lab Mac", status: "Offline", details: failure)
assert(rows.arrangedSubviews.compactMap { $0 as? NSButton }.isEmpty)
assert(rows.arrangedSubviews.compactMap { ($0 as? NSTextField)?.stringValue }.last
       == "Update status unavailable (503)")
window.makeKeyAndOrderFront(nil)
assert(window.isVisible)
window.close()
assert(!window.isVisible)
window.makeKeyAndOrderFront(nil)
assert(window.isVisible, "a closed Info window must reopen")
window.close()
print("info window contract passed")

// Poll a loopback fixture through the real HostProbe, inspect the built native
// menu, and invoke its actions. This covers wiring, not merely policy helpers.
let controller = StatusController()
let statusItem = Mirror(reflecting: controller).children.first { $0.label == "item" }!.value as! NSStatusItem
func waitUntil(_ condition: () -> Bool) {
    let deadline = Date().addingTimeInterval(5)
    while !condition() && Date() < deadline {
        RunLoop.main.run(until: Date().addingTimeInterval(0.02))
    }
    assert(condition(), "menu did not reach expected state")
}
waitUntil { statusItem.menu?.items.contains { $0.title == "Update Available" } == true }
let menu = statusItem.menu!
if ProcessInfo.processInfo.environment["FIXTURE_MODE"] == "open" {
    let devices = menu.items.first { $0.title == "1 Device" }!
    let machines = menu.items.first { $0.title == "1 Managed Mac" }!
    assert(devices.attributedTitle == nil && machines.attributedTitle == nil)
} else {
    assert(!menu.items.contains { $0.title == "1 Device" || $0.title == "1 Managed Mac" })
}
assert(!menu.items.contains { $0.title == "Software Updates" })
assert(statusItem.button!.attributedTitle.string == " ●")
assert(statusItem.button!.accessibilityLabel() == "jStack · Needs attention")
let info = menu.items.first { $0.title == "Info" }!
assert(info.submenu == nil && info.action != nil)
NSApp.sendAction(info.action!, to: info.target, from: info)
waitUntil { app.windows.contains { $0.title == "jStack Info" && $0.isVisible } }
let firstInfo = app.windows.first { $0.title == "jStack Info" && $0.isVisible }!
NSApp.sendAction(info.action!, to: info.target, from: info)
assert(app.windows.filter { $0.title == "jStack Info" && $0.isVisible }.count == 1)
firstInfo.close()
controller.showUpdates() // The jstack://updates entry uses this exact method.
assert(firstInfo.isVisible)
let available = statusItem.menu!.items.first { $0.title == "Update Available" }!
NSApp.sendAction(available.action!, to: available.target, from: available)
waitUntil { statusItem.menu?.items.contains { $0.title == "Update Available" } == false }
firstInfo.close()
print("live menu actions passed")
''')
    binary = tmp_path / "menu-contract"
    built = subprocess.run(["swiftc", "-D", "JSTACK_MENUBAR_TEST",
                            str(main), "-o", str(binary)], capture_output=True,
                           text=True, timeout=120)
    assert built.returncode == 0, built.stderr
    (tmp_path / "api-token").write_text("legacy-still-valid")
    (tmp_path / "internal-token").write_text("jr1.host-internal.local-proof\n")
    queued = []

    class Fixture(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, payload):
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.removeprefix("/api/jremote/v1")
            payload = {
                "/api/health": {"service": "jremote-host", "provisioned": True},
                "/host": {"host_id": "lab", "mode": {"mode": mode},
                          "features": {"device_management": True}},
                "/sessions/active": {"sessions": [
                    {"session_id": "a", "turn": "working"},
                    {"session_id": "b", "unread": True, "turn": "idle"},
                    {"session_id": "c", "attention": "waiting"}]},
                "/devices": {"devices": [{"id": "phone", "name": "Test Phone"}]},
                "/hosts": {"hosts": [{"key": "leaf", "name": "Test Leaf"}]},
                "/updates/inventory": {"release": "new", "machines": [
                    {"machine": "lab", "name": "Lab", "desired": "new",
                     "state": "pending" if queued else "available", "supervisor": True}]},
            }[path]
            self.reply(payload)

        def do_POST(self):
            assert self.path == "/api/jremote/v1/updates/queue"
            assert self.headers["Authorization"] == "Bearer jr1.host-internal.local-proof"
            queued.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.reply({"jobs": [{"id": "test-job", "state": "pending"}], "errors": []})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run([str(binary), "--port", str(server.server_port)],
                                capture_output=True, text=True, timeout=25,
                                env={**os.environ, "FIXTURE_MODE": mode,
                                     "JREMOTE_STATE_DIR": str(tmp_path),
                                     "JREMOTE_TOKEN_PATH": str(tmp_path / "api-token")})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert result.returncode == 0, result.stderr
    assert "device menu contract passed" in result.stdout
    assert "info window contract passed" in result.stdout
    assert "live menu actions passed" in result.stdout
    assert len(queued) == 1
    assert queued[0]["target"] == "self"
    assert queued[0]["request_id"]
