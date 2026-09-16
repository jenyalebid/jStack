"""Compile the menu bar's adopt steering without starting a desktop app — #61.

The dialog offers both adopt routes on one panel, and the name they apply to is
typed into that same panel. So the decision the bar has to get right is not
"what happened after the button was pressed" — it is which button was live
while the operator was reading. That decision is `MeshRoster`, and it is
compiled and exercised here rather than clicked.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc"),
                    reason="the menu bar uses macOS AppKit")
def test_the_offline_route_is_withheld_only_from_machines_on_the_mesh(tmp_path):
    from jstack_host import enrolment

    source = Path(__file__).resolve().parents[1] / "menubar" / "JStackHostBar.swift"
    main = tmp_path / "main.swift"
    # The slug rule exists twice — `enrolment.peer_name` mints the peer, and the
    # bar has to reproduce it to compare a live keystroke against the names the
    # hub reported. So the expectations are GENERATED from the Python one rather
    # than typed out here: a second copy of a rule that nothing compares is a
    # copy that drifts, and the drift would show up as a carried file quietly
    # offered again for a machine on the mesh.
    names = ["Work Mac", "work-mac", "  Ana's  Mac!! ", 'MacBook Pro (16")',
             "---", "", "-leading", "trailing-", "Ünïcode Mac", "9to5",
             "a" * 40, "x" * 31 + "-y"]

    def swift(text: str) -> str:
        return json.dumps(text, ensure_ascii=False)

    agreed = "\n".join(
        f"assert(MeshRoster.peerName({swift(name)}) "
        f"== {swift(enrolment.peer_name(name))}, "
        f"{swift(f'the bar and the hub disagree on {name!r}')})"
        for name in names)

    main.write_text(source.read_text() + "\n" + agreed + "\n" + r'''
import Foundation

let payload = """
[{"key":"a","name":"Work Mac","peer":"work-mac","online":true},
 {"key":"b","name":"Old Mac","peer":"old-mac","online":false},
 {"key":"c","name":"Studio","peer":"studio","online":null}]
"""
guard let roster = MeshRoster(json: payload) else {
    fatalError("the roster the adopt dialog reads did not parse")
}

// The reported defect: a Mac already answering over the mesh must not be
// offered a file to carry, under either the name it was adopted as or the peer
// name that was slugged out of it.
assert(roster.holds("Work Mac"), "a Mac on the mesh was offered the carried file")
assert(roster.holds("work-mac"))

// And the machines the carried file is genuinely for. `online: false` is a Mac
// whose peer this hub still holds while the machine itself is gone — wiped,
// reinstalled — and `null` is a hub that could not read its own peer table.
// Both must keep the route, because neither has another one.
assert(!roster.holds("Old Mac"), "a wiped Mac lost the only route it has left")
assert(!roster.holds("Studio"), "a guess was made where nothing was known")
assert(!roster.holds("New Mac"))
assert(!roster.holds(""))

// `HostControl.run` folds stderr into stdout, so a warning ahead of the
// payload must not read as "this hub adopted nothing" — which would silently
// re-offer the carried file to every machine on the mesh.
let noisy = "warning: something\n" + payload.split(separator: "\n").joined()
assert(MeshRoster(json: noisy)?.holds("Work Mac") == true)
assert(MeshRoster(json: "not json at all") == nil)

// A hub too old to report `peer`/`online` answers an array of plain rows: it
// parses, and it claims nothing about anybody.
assert(MeshRoster(json: #"[{"key":"a","name":"Work Mac"}]"#)?.live.isEmpty == true)

// The placeholder that used to sit inside a line meant to be copied and run.
let addressless = #"{"code":"ABC123","name":"Work Mac","port":9090,"addresses":[]}"#
guard let minted = MintedPairing(json: addressless) else {
    fatalError("a minted code stopped parsing")
}
assert(minted.attachCommand == nil, "a command with a blank in it was drawn")
let onMesh = #"""
{"code":"ABC123","name":"Work Mac","port":9090,
 "addresses":[{"kind":"mesh","url":"http://10.66.0.1:9090"}]}
"""#
assert(MintedPairing(json: onMesh)?.attachCommand
       == "jstack-host attach ABC123 --parent http://10.66.0.1:9090")

print("adopt steering contract passed")
''')
    binary = tmp_path / "adopt-steering"
    built = subprocess.run(["swiftc", "-D", "JSTACK_MENUBAR_TEST",
                            str(main), "-o", str(binary)], capture_output=True,
                           text=True, timeout=120)
    assert built.returncode == 0, built.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "adopt steering contract passed" in result.stdout
