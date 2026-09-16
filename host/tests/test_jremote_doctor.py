"""The setup validator — a green doctor and a working host are the same fact.

Pins that every check reports through the same seams the host runs on (a
binary the shell can see but the spawn path cannot is a FAIL), that a
crashing check is a failure rather than a hidden one, and that the report's
exit status is the worst grade.
"""

import io
import json
import os
from pathlib import Path

import pytest

from jstack_host import doctor, hostenv


@pytest.fixture
def machine(tmp_path, monkeypatch):
    agents = tmp_path / "Agents"
    (agents / "Ops" / "chat").mkdir(parents=True)
    (agents / "agents.json").write_text(json.dumps({"ops": {"emoji": "🛠️"}}))
    monkeypatch.setenv("JREMOTE_HOST_PROFILE", "default")
    monkeypatch.setenv("JREMOTE_INSTANCE_ROOT", str(agents))
    monkeypatch.setenv("JREMOTE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("JSTACK_AGENT_REGISTRY", raising=False)
    hostenv.reset_profile()
    yield tmp_path
    hostenv.reset_profile()


def test_every_check_answers_with_a_grade(machine):
    results = doctor.checks()
    names = [r["name"] for r in results]
    assert names == ["python", "claude", "tmux", "websocket", "open files", "token",
                     "profile", "agents", "registry", "timeline", "transcripts",
                     "scheduler", "allowance", "repos", "service", "source", "app"]
    assert all(r["grade"] in (doctor.OK, doctor.WARN, doctor.FAIL) for r in results)
    by = {r["name"]: r for r in results}
    assert by["token"]["grade"] == doctor.FAIL, "no token minted in this state dir"
    assert by["agents"]["grade"] == doctor.OK and "1 with an emoji" in by["agents"]["detail"]
    assert by["registry"]["grade"] == doctor.OK


def test_a_binary_the_spawn_path_cannot_see_is_a_failure(machine, monkeypatch):
    empty = machine / "empty-bin"
    empty.mkdir()
    monkeypatch.setattr(hostenv, "spawn_path", lambda *a, **k: str(empty))
    by = {r["name"]: r for r in doctor.checks()}
    assert by["tmux"]["grade"] == doctor.FAIL and "brew install tmux" in by["tmux"]["hint"]
    assert by["claude"]["grade"] == doctor.FAIL and str(empty) in by["claude"]["hint"]


def test_a_crashing_check_is_a_failure_not_a_hole(machine, monkeypatch):
    def boom():
        raise RuntimeError("kaput")
    boom.__name__ = "check_open_files"
    monkeypatch.setattr(doctor, "CHECKS", (boom,))
    (r,) = doctor.checks()
    assert r["grade"] == doctor.FAIL and "kaput" in r["detail"] and r["name"] == "open files"


def test_the_report_exits_with_the_worst_grade(machine, monkeypatch):
    monkeypatch.setattr(doctor, "CHECKS", (
        lambda: doctor._check("a", doctor.OK, "fine"),
        lambda: doctor._check("b", doctor.WARN, "later", "do this"),
    ))
    out = io.StringIO()
    assert doctor.report(out) == 1
    text = out.getvalue()
    assert "warn  b" in text and "→ do this" in text and "some screens wait" in text
    monkeypatch.setattr(doctor, "CHECKS", (lambda: doctor._check("c", doctor.FAIL, "no"),))
    assert doctor.report(io.StringIO()) == 2
    monkeypatch.setattr(doctor, "CHECKS", (lambda: doctor._check("d", doctor.OK, "yes"),))
    assert doctor.report(io.StringIO()) == 0


# ── the source check: does the running host serve the bytes the tree holds ──
#
# The serving side is the stamp the process recorded at its own startup (the
# embed marker here — written into the isolated path conftest points every
# test at); the tree side is monkeypatched through sourcestamp's own cache,
# because these tests are about the comparison, not about git.


def _tree(monkeypatch, sha, dirty=False):
    from jstack_host import sourcestamp
    monkeypatch.setattr(sourcestamp, "_stamp",
                        {"sha": sha, "dirty": dirty, "root": "/repo"})


def _marker(source):
    record = {"server": "the dashboard"}
    if source is not None:
        record["source"] = source
    Path(os.environ["JREMOTE_EMBED_MARKER"]).write_text(json.dumps(record))


def test_a_host_serving_the_tree_it_came_from_is_ok(machine, monkeypatch):
    _tree(monkeypatch, "a" * 40)
    _marker({"sha": "a" * 40, "dirty": False})
    r = doctor.check_source()
    assert r["grade"] == doctor.OK and "in step" in r["detail"]


def test_serving_uncommitted_bytes_is_named_exactly_that(machine, monkeypatch):
    """The failure the stamp exists for: the running code is not any commit,
    so no sha anywhere names it and no other machine can reproduce it."""
    _tree(monkeypatch, "a" * 40)
    _marker({"sha": "a" * 40, "dirty": True})
    r = doctor.check_source()
    assert r["grade"] == doctor.WARN and "UNCOMMITTED" in r["detail"]
    assert "commit" in r["hint"]


def test_a_stale_serving_host_warns_with_the_gap_and_the_action(machine, monkeypatch):
    _tree(monkeypatch, "b" * 40)
    _marker({"sha": "a" * 40, "dirty": False})
    monkeypatch.setattr(doctor, "_behind", lambda root, old, new: "3")
    r = doctor.check_source()
    assert r["grade"] == doctor.WARN and "3 commit(s) not deployed" in r["detail"]
    assert "restart" in r["hint"]


def test_a_dirty_tree_behind_a_clean_host_warns_about_the_next_restart(machine, monkeypatch):
    """Shas agree, the process is clean — but the tree has uncommitted edits
    in the served package, so the NEXT restart ships bytes nobody committed.
    Saying it now is the difference between a policy and an autopsy."""
    _tree(monkeypatch, "a" * 40, dirty=True)
    _marker({"sha": "a" * 40, "dirty": False})
    r = doctor.check_source()
    assert r["grade"] == doctor.WARN and "next" in r["detail"]


def test_a_serving_process_without_a_stamp_is_told_to_restart(machine, monkeypatch):
    _tree(monkeypatch, "a" * 40)
    _marker(None)
    r = doctor.check_source()
    assert r["grade"] == doctor.WARN and "predates" in r["detail"]
    assert "restart" in r["hint"]


def test_no_host_on_the_machine_means_nothing_to_drift(machine, tmp_path, monkeypatch):
    from jstack_host import install_host
    _tree(monkeypatch, "a" * 40)
    monkeypatch.setattr(install_host, "plist_path",
                        lambda *a, **k: tmp_path / "absent.plist")
    r = doctor.check_source()
    assert r["grade"] == doctor.OK and "nothing serving" in r["detail"]


# ── the app check: the fourth copy, graded only where the feed lives ────────


def _install_app(tmp_path, monkeypatch, build):
    import plistlib
    from jstack_host import desk
    contents = tmp_path / "jRemote.app" / "Contents"
    contents.mkdir(parents=True)
    with open(contents / "Info.plist", "wb") as fh:
        plistlib.dump({"CFBundleVersion": str(build),
                       "CFBundleShortVersionString": "1.4"}, fh)
    monkeypatch.setattr(desk, "APP", str(tmp_path / "jRemote.app"))


def test_no_app_installed_is_not_a_finding(machine, tmp_path, monkeypatch):
    from jstack_host import desk
    monkeypatch.setattr(desk, "APP", str(tmp_path / "jRemote.app"))
    assert doctor.check_app()["grade"] == doctor.OK


def test_an_app_behind_the_feed_it_updates_from_warns(machine, tmp_path, monkeypatch):
    from jstack_host import releases
    _install_app(tmp_path, monkeypatch, 60)
    monkeypatch.setattr(releases, "publishes", lambda: True)
    monkeypatch.setattr(releases, "latest", lambda: {"build": 67})
    r = doctor.check_app()
    assert r["grade"] == doctor.WARN and "behind its own feed" in r["detail"]


def test_an_app_ahead_of_the_feed_is_the_unaccounted_copy(machine, tmp_path, monkeypatch):
    """A build newer than anything published came out of somebody's Xcode and
    exists on exactly one machine — the copy no version census can explain."""
    from jstack_host import releases
    _install_app(tmp_path, monkeypatch, 99)
    monkeypatch.setattr(releases, "publishes", lambda: True)
    monkeypatch.setattr(releases, "latest", lambda: {"build": 67})
    r = doctor.check_app()
    assert r["grade"] == doctor.WARN and "AHEAD" in r["detail"]


def test_a_non_publishing_mac_reports_its_build_without_a_verdict(machine, tmp_path, monkeypatch):
    from jstack_host import releases
    _install_app(tmp_path, monkeypatch, 67)
    monkeypatch.setattr(releases, "publishes", lambda: False)
    r = doctor.check_app()
    assert r["grade"] == doctor.OK and "build 67" in r["detail"]


def test_status_and_doctor_adopt_the_installed_agents_environment(machine, tmp_path, monkeypatch):
    """Typed into a shell, `doctor` has none of the plist's environment. It
    reads the plist so it grades the host that is installed, not one the
    shell would have resolved on its own — and an explicit export still wins."""
    from jstack_host import install_host
    plist = tmp_path / "com.jremote.host.plist"
    plist.write_bytes(install_host.render_plist(
        state_dir=tmp_path / "state",
        environment={"JREMOTE_INSTANCE_ROOT": str(tmp_path / "Agents"),
                     "JREMOTE_HOST_PROFILE": "default"}))
    assert install_host.installed_environment(plist) == {
        "JREMOTE_INSTANCE_ROOT": str(tmp_path / "Agents"),
        "JREMOTE_HOST_PROFILE": "default",
        "JREMOTE_STATE_DIR": str(tmp_path / "state")}
    monkeypatch.delenv("JREMOTE_INSTANCE_ROOT")
    monkeypatch.setenv("JREMOTE_STATE_DIR", str(tmp_path / "elsewhere"))
    install_host.adopt_installed_environment(plist)
    assert os.environ["JREMOTE_INSTANCE_ROOT"] == str(tmp_path / "Agents")
    assert os.environ["JREMOTE_STATE_DIR"] == str(tmp_path / "elsewhere"), "the shell's export wins"
    assert install_host.installed_environment(tmp_path / "missing.plist") == {}
