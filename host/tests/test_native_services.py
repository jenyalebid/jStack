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
    sources = [str(source)]
    if filename in {"Network.swift", "NetworkInstall.swift"}:
        sources.append(str(source.parent / "ProtectedPaths.swift"))
        sources.append(str(source.parent / "NetworkAddress.swift"))
    if filename == "Network.swift":
        sources.append(str(source.parent / "NetworkCommand.swift"))
    result = subprocess.run(["swiftc", *flags, "-O", "-o", str(tmp_path / "probe"), *sources],
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


@pytest.fixture(scope="module")
def acl_probe(tmp_path_factory):
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        pytest.skip("requires macOS Swift toolchain")
    directory = tmp_path_factory.mktemp("native-acl")
    source = Path(__file__).resolve().parents[1] / "macos/ProtectedPaths.swift"
    main = directory / "main.swift"
    main.write_text('''import Darwin
do {
    try rejectWritableACL(CommandLine.arguments[1])
    exit(0)
} catch PathProtectionFailure.writableACL { exit(10) }
catch { exit(11) }
''')
    binary = directory / "probe"
    subprocess.run(["swiftc", "-o", str(binary), str(main), str(source)], check=True, capture_output=True, text=True, timeout=60)
    return binary


@pytest.mark.parametrize("grant,expected", [(None, 0), ("allow read", 0), ("deny delete", 0),
                                           ("allow write", 10), ("allow append", 10),
                                           ("allow writesecurity", 10), ("allow writeattr", 10),
                                           ("allow writeextattr", 10), ("allow chown", 10)])
def test_native_acl_protection(acl_probe, tmp_path, grant, expected):
    import os
    import pwd
    target = tmp_path / "protected-resource"
    target.write_text("fixture")
    target.chmod(0o600)
    entry = f"user:{pwd.getpwuid(os.getuid()).pw_name} {grant}"
    if grant:
        subprocess.run(["/bin/chmod", "+a", entry, str(target)], check=True, capture_output=True)
    try:
        result = subprocess.run([str(acl_probe), str(target)], capture_output=True, text=True, timeout=10)
        assert result.returncode == expected, result.stderr
    finally:
        if grant:
            subprocess.run(["/bin/chmod", "-a", entry, str(target)], check=True, capture_output=True)


@pytest.fixture(scope="module")
def command_probe(tmp_path_factory):
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        pytest.skip("requires macOS Swift toolchain")
    directory = tmp_path_factory.mktemp("native-command")
    source = Path(__file__).resolve().parents[1] / "macos/NetworkCommand.swift"
    main = directory / "main.swift"
    main.write_text(r'''
import Foundation
import Darwin
let mode = CommandLine.arguments[1]
let pidFile = CommandLine.arguments[2]
if mode.hasPrefix("child-") {
    try String(getpid()).write(toFile: pidFile, atomically: true, encoding: .utf8)
    switch mode {
    case "child-block": sleep(10)
    case "child-echo":
        let input = FileHandle.standardInput.readDataToEndOfFile()
        print(input.count)
        exit(7)
    case "child-group": print(getpgrp())
    case "child-flood": FileHandle.standardOutput.write(Data(repeating: 65, count: 1048576)); sleep(10)
    case "child-closed": break
    default: fatalError("unknown child")
    }
    exit(0)
}
let start = ProcessInfo.processInfo.systemUptime
let binary = CommandLine.arguments[0]
let payload = Data(repeating: 65, count: 1048576)
switch mode {
case "timeout":
    do {
        _ = try runNetworkCommand(binary, ["child-block", pidFile], input: payload, seconds: 0.5)
        fatalError("blocked stdin escaped timeout")
    } catch NetworkCommandFailure.timeout {}
case "echo":
    let result = try runNetworkCommand(binary, ["child-echo", pidFile], input: payload, captureOutput: true, seconds: 5)
    precondition(result.status == 7)
    precondition(String(data: result.output, encoding: .utf8) == "1048576\n")
case "group":
    let result = try runNetworkCommand(binary, ["child-group", pidFile], captureOutput: true, seconds: 2)
    precondition(result.status == 0)
    precondition(String(data: result.output, encoding: .utf8) == "\(getpgrp())\n", "command left launchd process group")
case "flood":
    do {
        _ = try runNetworkCommand(binary, ["child-flood", pidFile], captureOutput: true, seconds: 2)
        fatalError("unbounded output accepted")
    } catch NetworkCommandFailure.output {}
case "closed":
    do {
        _ = try runNetworkCommand(binary, ["child-closed", pidFile], input: payload, seconds: 2)
        fatalError("unconsumed input accepted")
    } catch NetworkCommandFailure.input {}
case "cancel":
    do {
        _ = try runNetworkCommand(binary, ["child-block", pidFile], seconds: 2, cancelled: { true })
        fatalError("cancellation ignored")
    } catch NetworkCommandFailure.cancelled {}
default: fatalError("unknown test")
}
if mode != "echo" { precondition(ProcessInfo.processInfo.systemUptime - start < 2, "command exceeded deadline") }
if mode != "cancel" {
    let child = Int32(try String(contentsOfFile: pidFile, encoding: .utf8))!
    precondition(kill(child, 0) != 0 && errno == ESRCH, "command child was not reaped")
}
''')
    binary = directory / "probe"
    subprocess.run(["swiftc", "-o", str(binary), str(main), str(source)], check=True, capture_output=True, text=True, timeout=60)
    return binary


@pytest.mark.parametrize("scenario", ["timeout", "echo", "group", "flood", "closed", "cancel"])
def test_native_command_lifecycle(command_probe, tmp_path, scenario):
    result = subprocess.run([str(command_probe), scenario, str(tmp_path / "child.pid")],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
