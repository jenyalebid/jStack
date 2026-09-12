"""What the embedded host's marker has to carry, and who reads it.

The marker is the only record an embedded host leaves — no LaunchAgent, no
plist. Every fact the menu bar needs about such a host therefore has to be in
here, because the alternative is what actually happened twice: the install
pinned whatever the installing shell had exported, and a later reinstall from
a shell that exported less silently took a control off the menu.
"""

import json
import re
from pathlib import Path

from jstack_host import embed

MENUBAR_INSTALL = Path(__file__).resolve().parents[1] / "menubar" / "install.sh"


def _declare(monkeypatch, tmp_path, label):
    monkeypatch.setenv("XPC_SERVICE_NAME", label)
    monkeypatch.setenv("JREMOTE_EMBED_MARKER", str(tmp_path / "embedded.json"))
    path = embed.declare(port=9090, server="the dashboard", root=str(tmp_path))
    return json.loads(Path(path).read_text())


def test_the_marker_records_the_launchd_job_the_host_runs_under(tmp_path, monkeypatch):
    """An embedded host has no agent of its own and is not unmanaged — the
    server it is mounted into has one, and that job is what Restart and Shut
    Down act on. Without it the menu bar resolves the package default
    `com.jremote.host`, finds no plist, reads the hub as not installed, and
    hides both controls on the Mac that runs the hub."""
    record = _declare(monkeypatch, tmp_path, "com.acme.dashboard")
    assert record["agent_label"] == "com.acme.dashboard"


def test_a_hand_started_server_offers_no_agent_rather_than_a_placeholder(
        tmp_path, monkeypatch):
    """launchd hands a process it did not job-start a placeholder of this
    shape. It names no plist, so pinning it puts the menu straight back to
    hunting an agent that cannot exist — and a hub started from a terminal is
    genuinely stopped by that terminal, not by a button."""
    assert _declare(monkeypatch, tmp_path, "0x0-0x1f2a3b")["agent_label"] == ""
    assert _declare(monkeypatch, tmp_path, "")["agent_label"] == ""


def test_the_menubar_installer_backfills_only_keys_the_marker_carries(
        tmp_path, monkeypatch):
    """The two halves of one seam, checked against each other.

    `install.sh` reads the marker with `sed`, so a key it names and `declare`
    stopped writing yields nothing, the install carries on unpinned, and the
    only symptom is a control missing from a menu weeks later. Drift between
    these two lists is the bug class itself, not a style question."""
    script = MENUBAR_INSTALL.read_text()
    loop = re.search(r"for pair in (.+?); do", script, re.S)
    assert loop, "the marker backfill loop moved — this test guards its keys"
    keys = re.findall(r'"JREMOTE_[A-Z_]+:([a-z_]+)"', loop.group(1))
    assert keys, "the loop names no marker keys"

    record = _declare(monkeypatch, tmp_path, "com.acme.dashboard")
    missing = [k for k in keys if k not in record]
    assert not missing, f"install.sh backfills keys declare() never writes: {missing}"


def test_the_agent_label_survives_the_install_that_pins_it():
    """`ENV_VARS` is what the installer is allowed to write into the agent's
    environment. A variable backfilled from the marker but absent from that
    sweep is read and then dropped on the floor."""
    script = MENUBAR_INSTALL.read_text()
    env_vars = re.search(r"ENV_VARS=\"(.+?)\"", script, re.S)
    assert env_vars, "ENV_VARS moved"
    assert "JREMOTE_AGENT_LABEL" in env_vars.group(1).split()
