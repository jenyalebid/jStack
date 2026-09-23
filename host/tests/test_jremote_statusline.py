"""The allowance sampler and the settings entry that runs it — jStack #133.

The reader (`allowance.py`) shipped in every release and its writer did not,
so a fresh hub drew no Usage bars and a hub where someone had run `/usage`
drew a number that went stale fifteen minutes later. These pin the writer: it
records what the status line hands it, it is silent and harmless on every
failure path, it does not rewrite a settings file that is already right, and
it never takes a `statusLine` the user set themselves.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from jstack_host import allowance, claude_settings, statusline

SCRIPT = Path(claude_settings.checkout_root()) / claude_settings.SAMPLER


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(allowance, "STATE", tmp_path / "allowance.json")
    monkeypatch.setattr(allowance, "LOCK", tmp_path / ".lock")
    monkeypatch.setattr(statusline, "STAMP", tmp_path / "stamp")
    return tmp_path


PAYLOAD = {"rate_limits": {
    "five_hour": {"used_percentage": 12.5, "resets_at": "2026-09-24T03:20:00+00:00"},
    "seven_day": {"used_percentage": 34, "resets_at": "2026-09-26T10:53:20+00:00"}}}


# ---------------------------------------------------------------- the sample

def test_a_render_becomes_a_reading(state):
    statusline.record(statusline.windows(PAYLOAD["rate_limits"]))
    claude = allowance.read()["providers"]["claude"]
    assert claude["source"] == "statusline"
    assert claude["stale"] is False
    assert [(w["id"], w["pct"]) for w in claude["windows"]] == [
        ("five_hour", 12.5), ("seven_day", 34.0)]


def test_a_window_without_a_percentage_is_dropped_not_zeroed(state):
    """"Not measured" and "nothing used" are different answers, and a meter
    that renders the first as the second is the failure this store exists to
    prevent."""
    got = statusline.windows({"five_hour": {"resets_at": "2026-09-24T03:20:00+00:00"},
                              "seven_day": {"used_percentage": 34}})
    assert [w["id"] for w in got] == ["seven_day"]


def test_window_order_is_fixed_not_payload_order(state):
    got = statusline.windows({"seven_day": {"used_percentage": 1},
                              "five_hour": {"used_percentage": 2}})
    assert [w["id"] for w in got] == ["five_hour", "seven_day"]


def test_the_throttle_is_shared_by_every_session(state):
    """The stamp is per-host, not per-process: twelve panes rendering at once
    must not take the allowance lock twelve times for the same number."""
    statusline.record(statusline.windows(PAYLOAD["rate_limits"]))
    first = allowance.read()["providers"]["claude"]["sampled_at"]
    statusline.record([{"id": "five_hour", "label": "x", "pct": 99, "resets_at": None}])
    assert allowance.read()["providers"]["claude"]["sampled_at"] == first


def test_nothing_measured_records_nothing(state):
    statusline.record(statusline.windows({}))
    assert allowance.read()["providers"]["claude"] is None


# ------------------------------------------------- silence on every path

@pytest.mark.parametrize("stdin", ['{"rate_limits": {"five_hour": {"used_percentage": 3}}}',
                                   "not json at all", "", "[]",
                                   '{"rate_limits": "wrong shape"}'])
def test_the_command_is_always_silent_and_always_exits_zero(stdin, tmp_path):
    """A status-line command that prints or fails taxes every session on the
    machine, so the contract is the same on the good path and the bad one."""
    done = subprocess.run([sys.executable, str(SCRIPT)], input=stdin, text=True,
                          capture_output=True,
                          env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
                               "JREMOTE_STATE_DIR": str(tmp_path / "state"),
                               "JSTACK_STATUSLINE_STAMP": str(tmp_path / "stamp")})
    assert done.returncode == 0
    assert done.stdout == ""


def test_the_command_records_through_the_state_dir_it_is_given(tmp_path):
    state = tmp_path / "state"
    subprocess.run([sys.executable, str(SCRIPT)], text=True,
                   input=json.dumps(PAYLOAD), check=True,
                   env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
                        "JREMOTE_STATE_DIR": str(state),
                        "JSTACK_STATUSLINE_STAMP": str(tmp_path / "stamp")})
    stored = json.loads((state / "allowance.json").read_text())
    assert stored["providers"]["claude"]["source"] == "statusline"


# ---------------------------------------------------------------- the wiring

def test_a_machine_with_no_settings_gets_the_sampler(tmp_path):
    path = tmp_path / "settings.json"
    claude_settings.install(path=path, checkout=Path("/opt/jstack"), state_dir="")
    assert json.loads(path.read_text())["statusLine"] == {
        "type": "command", "command": f"/opt/jstack/{claude_settings.SAMPLER}"}


def test_the_users_own_status_line_is_never_taken(tmp_path):
    path = tmp_path / "settings.json"
    mine = {"statusLine": {"type": "command", "command": "~/bin/my-prompt"}}
    path.write_text(json.dumps(mine))
    note = claude_settings.install(path=path, checkout=Path("/opt/jstack"), state_dir="")
    assert json.loads(path.read_text()) == mine
    assert "left your own status line alone" in note
    assert claude_settings.statusline_state(path)[0] is False


def test_an_already_wired_machine_is_not_rewritten(tmp_path):
    path = tmp_path / "settings.json"
    claude_settings.install(path=path, checkout=Path("/opt/jstack"), state_dir="")
    before = path.stat().st_mtime_ns
    note = claude_settings.install(path=path, checkout=Path("/opt/jstack"), state_dir="")
    assert path.stat().st_mtime_ns == before
    assert note == "status line already samples the allowance"


def test_our_own_entry_is_repointed_when_the_install_moves(tmp_path):
    """A release stage replaces the checkout under a running machine; the
    sampler is recognised by the script it ends in, not by its root."""
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"statusLine": {
        "type": "command", "command": f"/old/stage/{claude_settings.SAMPLER}"}}))
    claude_settings.install(path=path, checkout=Path("/opt/jstack"), state_dir="")
    assert json.loads(path.read_text())["statusLine"]["command"] == \
        f"/opt/jstack/{claude_settings.SAMPLER}"


def test_other_settings_survive_the_edit(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"model": "opus", "hooks": {"Stop": []}}))
    claude_settings.install(path=path, checkout=Path("/opt/jstack"), state_dir="")
    got = json.loads(path.read_text())
    assert got["model"] == "opus" and got["hooks"] == {"Stop": []}


def test_a_non_default_state_dir_is_carried_into_the_command(monkeypatch):
    """An embedded host resolves its own state root. A sampler that recorded
    into the default would be faithful and unread."""
    monkeypatch.setenv("JREMOTE_STATE_DIR", "/srv/dash/state")
    assert claude_settings.statusline_command(Path("/opt/jstack")) == \
        f"JREMOTE_STATE_DIR=/srv/dash/state /opt/jstack/{claude_settings.SAMPLER}"


def test_the_default_state_dir_is_not_carried(monkeypatch):
    from jstack_host import hostenv
    monkeypatch.setenv("JREMOTE_STATE_DIR", str(hostenv.profile().state_dir()))
    assert claude_settings.statusline_command(Path("/opt/jstack")) == \
        f"/opt/jstack/{claude_settings.SAMPLER}"


def test_check_writes_nothing(tmp_path):
    path = tmp_path / "settings.json"
    claude_settings.install(path=path, checkout=Path("/opt/jstack"), state_dir="",
                            dry_run=True)
    assert not path.exists()


# ---------------------------------------------------------------- the doctor

def test_the_doctor_reports_a_live_reading_rather_than_the_cli_cache(state, monkeypatch):
    """It used to read only the CLI's cache, so a host whose bars were being
    kept fresh by the sampler was told "no usage reading cached yet"."""
    from jstack_host import doctor
    monkeypatch.setattr(claude_settings, "SETTINGS", state / "settings.json")
    claude_settings.install(path=state / "settings.json")
    statusline.record(statusline.windows(PAYLOAD["rate_limits"]))
    got = doctor.check_allowance()
    assert got["grade"] == "ok"
    assert "statusline" in got["detail"] and "34%" in got["detail"]


def test_the_doctor_warns_when_nothing_samples(state, monkeypatch):
    from jstack_host import doctor
    monkeypatch.setattr(claude_settings, "SETTINGS", state / "settings.json")
    got = doctor.check_allowance()
    assert got["grade"] == "warn"
    assert "nothing on this machine samples" in got["detail"]
    assert "claude_setup.py" in got["hint"]
