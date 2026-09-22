"""The Adopt panel's own layout, measured rather than read.

`test_menubar_adopt_steering.py` proves which button is live for a given name.
It cannot see the panel, and the panel is what shipped broken: the accessory is
an NSStackView, which is Auto Layout, so the frame the code set on it said
nothing about the views inside it. With a leading alignment and no width of
their own, both dropped to their intrinsic size and the name field rendered a
few points wide — a dialog asking for a name with nowhere to type one.

Measured by compiling the real function and reading the frames back, because
every other way of checking this ("the code sets width 280") is the check that
passed while the field was invisible.
"""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "menubar" / "JStackHostBar.swift"


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc"),
                    reason="the menu bar uses macOS AppKit")
def test_the_name_field_is_as_wide_as_the_panel(tmp_path):
    main = tmp_path / "main.swift"
    main.write_text(SOURCE.read_text() + "\n" + r'''
let probe = NSApplication.shared
probe.setActivationPolicy(.accessory)
let (stack, field, note) = StatusController.adoptAccessory()
stack.layoutSubtreeIfNeeded()
assert(field.frame.width == 280,
       "the name field is \(field.frame.width) wide, not 280 — nothing to type into")
assert(stack.frame.width == 280, "the accessory is \(stack.frame.width) wide, not 280")
// The note is reserved room, and room reserved and left blank is a band of
// dead space under the field — the panel looking broken in its own way.
assert(!note.stringValue.isEmpty, "the reserved note band is empty")
// `>=` and not `==`: a wrapping label is laid out two points wider than its
// text on each side, so 284 here is 280 of sentence.
assert(note.frame.width >= 280, "the note does not span the panel")
// The field sits above the note, not beside it — a horizontal stack would
// measure the same total width and read as two columns.
assert(field.frame.minY > note.frame.maxY - 1, "the note is not under the field")
// Reserved for the longer sentence: the panel lays out once, so a band sized
// around the shorter one clips the other the moment a typed name earns it.
assert(stack.frame.height >= field.frame.height + note.fittingSize.height,
       "the note would clip at \(stack.frame.height)")
print("adopt accessory contract passed")
''')
    binary = tmp_path / "adopt-accessory"
    built = subprocess.run(["swiftc", "-D", "JSTACK_MENUBAR_TEST",
                            str(main), "-o", str(binary)], capture_output=True,
                           text=True, timeout=600)
    assert built.returncode == 0, built.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "adopt accessory contract passed" in result.stdout
