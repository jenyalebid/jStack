"""Signed login state must never fall back to missing legacy launchd files."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


SOURCE = Path(__file__).parents[1] / "menubar/JStackHostBar.swift"


def test_signed_menu_routes_to_registration_and_system_settings():
    source = SOURCE.read_text()
    login = source.split("// ── Login", 1)[1].split("// No agent label", 1)[0]
    signed, legacy = login.split("} else if HostAgent.isInstalled", 1)
    assert "if HostAgent.appOwned" in signed
    assert "SignedLogin.status(HostAgent.serviceRole)" in signed
    assert 'SignedLogin.status("menu")' in signed
    assert "#selector(doOpenLoginSettings)" in signed
    assert "LoginAgent." not in signed
    assert "!HostAgent.appOwned && LoginAgent.exists(MenuBarAgent.label)" in legacy
    setter = source.split("private func setLogin(", 1)[1].split("/// The state of", 1)[0]
    assert setter.index("guard !HostAgent.appOwned") < setter.index("LoginAgent.setStartsAtLogin")
    diagnostics = source.split("@objc private func doCopyDiagnostics()", 1)[1]
    assert 'SignedLogin.status("menu")' in diagnostics
    assert "SignedLogin.status(HostAgent.serviceRole)" in diagnostics
    assert 'HostAgent.appOwned ? "live.jstack.hub.menu"' in source


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc"), reason="macOS only")
def test_actual_signed_status_policy(tmp_path):
    source = SOURCE.read_text()
    policy = "enum SignedLogin {" + source.split("enum SignedLogin {", 1)[1].split(
        "/// Legacy LaunchAgents only", 1)[0]
    main = tmp_path / "main.swift"
    main.write_text("import Foundation\nimport ServiceManagement\n" + policy + '''
assert(SignedLogin.description(.enabled) == "Enabled")
assert(SignedLogin.description(.notRegistered) == "Not registered")
assert(SignedLogin.description(.requiresApproval) == "Approval required")
assert(SignedLogin.description(.notFound) == "Service not found")
assert(SignedLogin.description(nil) == "Unknown")
assert(SignedLogin.status("menu") == nil, "missing catalog is unknown, not disabled")
print("signed login policy passed")
''')
    binary = tmp_path / "login-policy"
    subprocess.run(["swiftc", str(main), "-o", str(binary)], check=True,
                   capture_output=True, text=True, timeout=120)
    result = subprocess.run([str(binary)], check=True, capture_output=True, text=True, timeout=15)
    assert "signed login policy passed" in result.stdout
