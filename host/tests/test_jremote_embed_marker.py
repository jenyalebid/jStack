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


def test_the_marker_records_the_source_the_server_loaded(tmp_path, monkeypatch):
    """declare() runs in the serving process at its startup — the one moment
    the loaded bytes and the tree are still the same thing. An embedded host
    has no /api/health of its own, so this field is the only place a doctor
    can learn which source is actually answering requests."""
    from jstack_host import sourcestamp
    monkeypatch.setattr(sourcestamp, "_stamp",
                        {"sha": "c" * 40, "dirty": True, "root": "/repo"})
    record = _declare(monkeypatch, tmp_path, "com.acme.dashboard")
    assert record["source"] == {"sha": "c" * 40, "dirty": True, "root": "/repo"}
