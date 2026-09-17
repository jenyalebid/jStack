"""Compile the actual security-sensitive native sources in the host gate."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc"), reason="requires macOS Swift toolchain")
@pytest.mark.parametrize("filename,flags", [("Network.swift", ["-parse-as-library"]),
                                          ("NetworkInstall.swift", ["-parse-as-library"]),
                                          ("ServiceControl.swift", [])])
def test_native_service_compiles(tmp_path, filename, flags):
    source = Path(__file__).resolve().parents[1] / "macos" / filename
    result = subprocess.run(["swiftc", *flags, "-O", "-o", str(tmp_path / "probe"), str(source)],
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc"), reason="requires macOS Swift toolchain")
def test_network_recovery_does_not_unregister_a_newly_denied_owner(tmp_path):
    source = (Path(__file__).resolve().parents[1] / "macos/NetworkInstall.swift").read_text()
    # Compile the actual stop operation with OS observations replaced. Any
    # access past the denial guard fails, so this never touches real services.
    operation = source[source.index("func stopNetwork("):source.index("func restoreForwarding(")]
    fixture = r'''
import Foundation
import Darwin
enum Refusal: Error { case denied(String) }
func refuse(_ reason: String) -> Refusal { .denied(reason) }
var actions: [String] = []
var allowObservations = false
func userControl(_ owner: UInt32, action: String, work: URL) throws -> String {
    actions.append(action)
    return action == "status" ? "requires_approval" : "not_registered"
}
func loaded(_ label: String, at work: URL) throws -> Bool {
    precondition(allowObservations, "denied recovery went past its approval check")
    return false
}
func run(_ command: String, _ args: [String], at work: URL) throws -> (Int32, String) {
    fatalError("unexpected command execution")
}
'''
    fixture += operation
    fixture += r'''
let work = URL(fileURLWithPath: "/unused-test-path")
do {
    try stopNetwork(501, at: work)
    fatalError("denied recovery was accepted")
} catch Refusal.denied(let reason) {
    precondition(reason.contains("approval changed before unregister"))
}
precondition(actions == ["status"])
actions = []
allowObservations = true
try stopNetwork(501, at: work, allowDenied: true)
precondition(actions == ["status", "unregister"], "explicit uninstall should still remove a denied registration")
'''
    path = tmp_path / "main.swift"
    path.write_text(fixture)
    binary = tmp_path / "probe"
    subprocess.run(["swiftc", "-o", str(binary), str(path)], check=True, capture_output=True, text=True, timeout=60)
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
