"""Compile the actual menu's device policy without starting a desktop app."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc"),
                    reason="the menu bar uses macOS AppKit")
def test_revoked_rows_and_stale_removal_responses(tmp_path):
    source = Path(__file__).resolve().parents[1] / "menubar" / "JStackHostBar.swift"
    main = tmp_path / "main.swift"
    main.write_text(source.read_text() + "\n" + r'''
import Foundation
let decoder = JSONDecoder()
decoder.keyDecodingStrategy = .convertFromSnakeCase
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
print("device menu contract passed")
''')
    binary = tmp_path / "menu-contract"
    built = subprocess.run(["swiftc", "-D", "JSTACK_MENUBAR_TEST",
                            str(main), "-o", str(binary)], capture_output=True,
                           text=True, timeout=120)
    assert built.returncode == 0, built.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "device menu contract passed" in result.stdout
