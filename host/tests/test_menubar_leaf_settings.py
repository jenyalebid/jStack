"""The menu bar's half of the leaf contract (jStack-Project docs/leaf.md).

Home: each Managed Mac row opens that leaf's Leaf Settings window, and the
menu carries no leaf switch of its own. A Headless leaf: Restart and Settings,
and a Hub Settings that is About and jRemote only.
"""
from pathlib import Path
import os
import shutil
import subprocess
import sys

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "menubar" / "JStackHostBar.swift"


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc"),
                    reason="the menu bar uses macOS AppKit")
def test_leaf_settings_decode_render_and_write(tmp_path):
    main = tmp_path / "main.swift"
    main.write_text(SOURCE.read_text() + "\n" + r'''
import Foundation
let decoder = JSONDecoder()
decoder.keyDecodingStrategy = .convertFromSnakeCase

// A leaf's own /host: the block is what flips its menu.
let headless = try decoder.decode(HostIdentity.self, from: Data(#"""
{"host_id":"bench","leaf":{"mode":"headless","agents_tab":false}}
"""#.utf8))
assert(headless.isHeadless && headless.leaf?.agentsTab == false)
// No block, or a word this build does not know, is Standard.
let plain = try decoder.decode(HostIdentity.self, from: Data(#"{"host_id":"x"}"#.utf8))
assert(!plain.isHeadless)
assert(LeafMode.read("locked") == .standard && LeafMode.read(nil) == .standard)

// Home's roster row carries the stored policy, Standard switches included.
let row = try decoder.decode(AdoptedHost.self, from: Data(#"""
{"key":"bench","name":"Bench","address":"10.66.0.9","port":9090,"delegated":true,
 "sees_home":false,"sees_leaves":true,"usage_reporting":"hidden",
 "mode":"headless","agents_tab":false,"line":"main"}
"""#.utf8))
assert(row.mode == "headless" && row.agentsTab == false && row.line == "main")
assert(row.seesHome == false && UsagePolicy.read(row.usageReporting) == .hidden)
print("leaf contract decoded")

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
var writes: [[String: Any]] = []
var forgot = 0
let window = LeafSettingsWindow(key: "bench")
window.render(LeafSettingsForm(leaf: row, update: nil, install: nil, busy: false,
    set: { writes.append($0) }, forget: { forgot += 1 }))
assert(window.title == "Bench Settings")
let hosting = window.contentView as! NSHostingView<LeafSettingsForm>
hosting.rootView.set(["agents_tab": true])
hosting.rootView.forget()
assert(writes.count == 1 && writes[0]["agents_tab"] as? Bool == true && forgot == 1)
print("leaf settings render")
''')
    binary = tmp_path / "leaf-settings"
    built = subprocess.run(["swiftc", "-D", "JSTACK_MENUBAR_TEST", str(main), "-o", str(binary)],
                           capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, built.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60,
                            env={**os.environ, "JREMOTE_STATE_DIR": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    assert "leaf contract decoded" in result.stdout
    assert "leaf settings render" in result.stdout


def _between(source: str, start: str) -> str:
    return source.split(start, 1)[1].split("\n    }\n", 1)[0]


def test_home_menu_opens_a_window_and_carries_no_leaf_switch():
    """A switch left in the submenu is a second place the same word is set."""
    source = SOURCE.read_text()
    machines = _between(source, "private func machinesItem() -> NSMenuItem? {")
    assert "#selector(doOpenLeafSettings)" in machines
    for gone in ("Sees Home Instance", "Usage on Home", "Forget", "row.submenu"):
        assert gone not in machines
    # Hub Settings carries this Mac only.
    form = source.split("struct HostInfoForm: View {", 1)[1].split("\nstruct ", 1)[0]
    assert "InfoMachineSection" not in form


def test_one_policy_route_and_no_retired_verbs():
    source = SOURCE.read_text()
    assert '"/hosts/\\(escaped(hostKey))/policy"' in source
    assert "/visibility" not in source and '/usage"' not in source


def test_a_headless_menu_is_restart_and_settings():
    source = SOURCE.read_text()
    body = _between(source, "private func headless(_ menu: NSMenu) {")
    actions = [line for line in body.splitlines() if "Self.action(" in line]
    assert len(actions) == 2
    assert '"Restart Hub"' in actions[0] and '"Settings…"' in actions[1]
    form = source.split("struct HostInfoForm: View {", 1)[1].split("\nstruct ", 1)[0]
    assert "if !headless," in form
