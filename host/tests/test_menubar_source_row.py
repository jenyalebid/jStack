"""What the Info window's Source row says, in each state it can be in.

The row is the only place a person is told which ref this hub follows and
whether it is behind one, so every sentence it can print is pinned here — a
window that renders "Up to date" over a failed check is the lie the whole
source mechanism was rebuilt to stop telling.
"""
from pathlib import Path
import os
import shutil
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc"),
                    reason="the menu bar uses macOS AppKit")
def test_source_row_states_and_the_refs_it_will_offer(tmp_path):
    source = Path(__file__).resolve().parents[1] / "menubar" / "JStackHostBar.swift"
    main = tmp_path / "main.swift"
    main.write_text(source.read_text() + "\n" + r'''
import Foundation
let decoder = JSONDecoder()
decoder.keyDecodingStrategy = .convertFromSnakeCase
func hub(_ json: String) throws -> HubSource {
    try decoder.decode(HubSource.self, from: Data(json.utf8))
}
let base = #"""
{"ref":"dev","repository":"example/stack","enabled":true,"managed":false,
 "running":{"release":"2026-09-22-abcdef12","sha":"abcdef1234567890","dirty":false,
            "version":"0.75.0"},
 "check":{"status":"current","head":"abcdef1234567890","checked":1000,"detail":""},
 "build":{"state":"idle"},"can_build":true,"blocked":""}
"""#
var current = try hub(base)
assert(current.summary == "Up to date with dev.")
assert(current.running?.displayVersion == "2026-09-22-abcdef12")
assert(current.switchable && current.canBuild && !current.building)

current.check?.status = "behind"
assert(current.summary == "dev has moved ahead. Rebuild to take it.")

current.build = HubBuildPhase(state: "building", ref: "dev")
assert(current.building && current.summary == "Building dev…")

current.build = HubBuildPhase(state: "failed", detail: "the checkout is gone")
assert(!current.building && current.summary == "Build failed: the checkout is gone")

// A finished build outranks a check that predates it: the machine is not
// "behind" once the commit is built and waiting to be installed.
current.build = HubBuildPhase(state: "built", release: "2026-09-23-99999999")
assert(current.summary == "Built 2026-09-23-99999999. Install it under Software Updates.")
current.build = HubBuildPhase(state: "built", release: "2026-09-22-abcdef12")
assert(current.summary == "dev has moved ahead. Rebuild to take it.")

current.build = HubBuildPhase(state: "stalled", ref: "dev")
assert(current.summary.contains("interrupted"))

current.build = HubBuildPhase(state: "idle")
current.check?.status = "failed"
current.check?.detail = "404 no such ref"
assert(current.summary == "Could not check dev: 404 no such ref")
current.check = nil
assert(current.summary == "Not checked yet.")

// `stable` is the config's name for main; the control shows the name a person
// picking it knows, and offers exactly the two it can set.
assert(HubSource.offered == ["stable", "dev"])
assert(HubSource.label("stable") == "main" && HubSource.label("dev") == "dev")
var arbitrary = try hub(base)
arbitrary.ref = "feature/lab"
assert(!arbitrary.switchable, "a CLI-set ref must not be rewritten by the control")
assert(HubSource.label("feature/lab") == "feature/lab")

var managed = try hub(base)
managed.managed = true
assert(!managed.switchable)

// A hub with adopted machines gets the reason, not a button that errors.
let blocked = try hub(#"""
{"ref":"stable","enabled":true,"managed":false,"can_build":false,
 "blocked":"this hub has adopted machines (Office Mac) — see #144",
 "check":{"status":"behind"},"build":{"state":"idle"}}
"""#)
assert(!blocked.canBuild && blocked.blocked!.contains("#144"))
assert(blocked.summary == "main has moved ahead. Rebuild to take it.")
assert(blocked.running == nil)

assert(HostProbe.reason(#"{"detail":"a build is already running on this hub"}"#)
       == "a build is already running on this hub")
assert(HostProbe.reason("not json at all") == "not json at all")
print("source row contract passed")

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
var picked: [String] = []
var rebuilds = 0
let window = HostInfoWindow()
window.render(HostInfoForm(machine: "Lab Mac", status: "Running", version: "0.75.0",
    source: "abcdef", hubSource: current, sourceBusy: false,
    follow: { picked.append($0) }, rebuild: { rebuilds += 1 },
    app: InfoAppSnapshot(), updateStatus: "No update published", error: nil,
    localCommand: nil, machines: [], commands: [:], allCommand: nil,
    open: {}, download: {}))
let hosting = window.contentView as! NSHostingView<HostInfoForm>
assert(hosting.rootView.hubSource?.ref == "dev")
hosting.rootView.follow("stable")
hosting.rootView.rebuild()
assert(picked == ["stable"] && rebuilds == 1)
print("source row renders")
''')
    binary = tmp_path / "source-row"
    built = subprocess.run(["swiftc", "-D", "JSTACK_MENUBAR_TEST", str(main), "-o", str(binary)],
                           capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, built.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60,
                            env={**os.environ, "JREMOTE_STATE_DIR": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    assert "source row contract passed" in result.stdout
    assert "source row renders" in result.stdout
