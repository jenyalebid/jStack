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
                     "scheduler", "allowance", "codex hooks", "hook owners",
                     "activation", "repos",
                     "service", "source", "app", "files"]
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
    monkeypatch.setattr(doctor, "CHECKS", (lambda: doctor._check("token", doctor.FAIL, "no"),))
    assert doctor.report(io.StringIO()) == 2
    monkeypatch.setattr(doctor, "CHECKS", (lambda: doctor._check("d", doctor.OK, "yes"),))
    assert doctor.report(io.StringIO()) == 0


def test_a_failed_optional_surface_does_not_claim_the_host_cannot_serve_chats(machine, monkeypatch):
    """#90. The closing line is read from WHICH checks failed. A Files-share
    finding on a serving host used to print the chat-outage sentence; now it
    names the surface, says chats stand, and still exits 2."""
    monkeypatch.setattr(doctor, "CHECKS", (
        lambda: doctor._check("token", doctor.OK, "present"),
        lambda: doctor._check("files", doctor.FAIL, "undeclared SMB share point(s): x",
                              "run `jstack-host files status`"),
    ))
    out = io.StringIO()
    assert doctor.report(out) == 2
    text = out.getvalue()
    assert "cannot serve chats" not in text
    assert "files failed" in text and "chats are unaffected" in text


def test_a_failed_serve_blocking_check_names_itself_in_the_verdict(machine, monkeypatch):
    monkeypatch.setattr(doctor, "CHECKS", (
        lambda: doctor._check("files", doctor.FAIL, "drifted"),
        lambda: doctor._check("token", doctor.FAIL, "missing"),
    ))
    out = io.StringIO()
    assert doctor.report(out) == 2
    assert "the host cannot serve chats until the failures above are fixed (token)" in out.getvalue()


def test_every_serve_blocking_name_is_a_real_check():
    """A name in SERVE_BLOCKING that no check produces is a gate that never
    fires — pin the set to the checks that exist."""
    names = {fn.__name__.removeprefix("check_").replace("_", " ") for fn in doctor.CHECKS}
    assert doctor.SERVE_BLOCKING <= names, doctor.SERVE_BLOCKING - names


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


# ── one owner per mechanism ──
#
# The duplicate this check exists for cost session 244ca668 two compactions on
# one delivered turn (2026-09-24): the shipped stop hook and the hand-wired copy
# of it held different lock names, so each believed it was the only one.

def _plugin(tmp_path, monkeypatch, shipped, owners):
    plugin = tmp_path / "plugins" / "jstack"
    (plugin / "hooks").mkdir(parents=True)
    (plugin / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {
        "Stop": [{"hooks": [{"type": "command",
                             "command": "${CLAUDE_PLUGIN_ROOT}/hooks/" + name}
                            for name in shipped]}]}}))
    (plugin / "hooks" / "owners.json").write_text(json.dumps({"mechanisms": owners}))
    from jstack_host import plugin_paths
    monkeypatch.setattr(plugin_paths, "jstack_root", lambda: plugin)
    return plugin


def _settings(tmp_path, monkeypatch, commands):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": c} for c in commands]}]}}))
    monkeypatch.setenv("HOME", str(home))
    return home


OWNERS = [{"mechanism": "compact on delivery", "ships": "stop-compact-delivery.sh",
           "replaces": ["compact_on_delivery.py"]}]


def test_a_hand_wired_copy_of_a_shipped_hook_is_named(machine, monkeypatch):
    _plugin(machine, monkeypatch, ["stop-compact-delivery.sh"], OWNERS)
    _settings(machine, monkeypatch,
              ["~/ops/assistant/hooks/compact_on_delivery.py"])
    r = doctor.check_hook_owners()
    assert r["grade"] == doctor.WARN
    assert "compact on delivery (Stop)" in r["detail"]
    assert "settings.json" in r["hint"]


def test_the_shipped_copy_is_not_its_own_duplicate(machine, monkeypatch):
    plugin = _plugin(machine, monkeypatch, ["stop-compact-delivery.sh"], OWNERS)
    # Both spellings of reaching the shipped hook: the variable and the resolved path.
    _settings(machine, monkeypatch, ["${CLAUDE_PLUGIN_ROOT}/hooks/stop-compact-delivery.sh",
                                     f"{plugin}/hooks/stop-compact-delivery.sh"])
    r = doctor.check_hook_owners()
    assert r["grade"] == doctor.OK and "1 shipped hooks" in r["detail"]


def test_a_hook_that_declares_no_mechanism_cannot_be_policed_and_says_so(machine, monkeypatch):
    _plugin(machine, monkeypatch, ["stop-compact-delivery.sh", "stop-new-thing.py"], OWNERS)
    _settings(machine, monkeypatch, [])
    r = doctor.check_hook_owners()
    assert r["grade"] == doctor.WARN
    assert "stop-new-thing.py" in r["detail"] and "owners.json" in r["hint"]


def test_a_machine_with_no_user_settings_has_nothing_to_double(machine, monkeypatch):
    _plugin(machine, monkeypatch, ["stop-compact-delivery.sh"], OWNERS)
    monkeypatch.setenv("HOME", str(machine / "bare"))
    r = doctor.check_hook_owners()
    assert r["grade"] == doctor.OK


# ── activation: declared systems.json wiring actually converges ──
#
# jStack#134 — a shipped system and an active one are different facts, and
# nothing used to say which systems still need a hand. check_activation reads
# the real systems.json (or a fixture standing in for it, here) and grades
# whether every declared `activation` block matches the machine.

def _systems_json(tmp_path, monkeypatch, systems):
    plugin = tmp_path / "plugins" / "jstack"
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "systems.json").write_text(json.dumps({"systems": systems}))
    from jstack_host import plugin_paths
    monkeypatch.setattr(plugin_paths, "jstack_root", lambda: plugin)
    return plugin


def test_no_declared_activation_is_ok_not_a_hole(machine, monkeypatch):
    _systems_json(machine, monkeypatch, [{"id": "path-rule-injection"}])
    r = doctor.check_activation()
    assert r["grade"] == doctor.OK
    assert "no system declares an activation" in r["detail"]


def test_an_unknown_activation_kind_is_named_not_swallowed(machine, monkeypatch):
    _systems_json(machine, monkeypatch, [
        {"id": "typo-system", "activation": {"claude_setings": {"statusLine": True}}}])
    r = doctor.check_activation()
    assert r["grade"] == doctor.WARN
    assert "typo-system" in r["detail"] and "claude_setings" in r["detail"]


def test_a_converged_claude_settings_activation_is_ok(machine, monkeypatch):
    _systems_json(machine, monkeypatch, [
        {"id": "allowance-sampler",
         "activation": {"claude_settings": {"statusLine": True}}}])
    from jstack_host import claude_settings
    home = machine / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({
        "statusLine": {"type": "command", "command": claude_settings.SAMPLER}}))
    monkeypatch.setenv("HOME", str(home))
    r = doctor.check_activation()
    assert r["grade"] == doctor.OK and "1 declared, all converged" in r["detail"]


def test_a_missing_statusline_is_a_warn_naming_the_system(machine, monkeypatch):
    _systems_json(machine, monkeypatch, [
        {"id": "allowance-sampler",
         "activation": {"claude_settings": {"statusLine": True}}}])
    monkeypatch.setenv("HOME", str(machine / "bare-home"))
    r = doctor.check_activation()
    assert r["grade"] == doctor.WARN
    assert "allowance-sampler (claude_settings)" in r["detail"]


def test_a_kind_with_no_observer_is_reported_not_silently_converged(machine, monkeypatch):
    _systems_json(machine, monkeypatch, [
        {"id": "job-monitor", "activation": {"codex_mcp": {"name": "job_monitor"}}}])
    monkeypatch.setenv("HOME", str(machine / "bare-home"))
    r = doctor.check_activation()
    assert r["grade"] == doctor.WARN
    assert "job-monitor (codex_mcp)" in r["detail"]
    assert "no observer yet for: codex_mcp" in r["hint"]


def test_activation_declared_in_a_subsystem_is_still_read(machine, monkeypatch):
    _systems_json(machine, monkeypatch, [
        {"id": "parent", "subsystems": [
            {"id": "child", "activation": {"claude_settings": {"statusLine": True}}}]}])
    monkeypatch.setenv("HOME", str(machine / "bare-home"))
    r = doctor.check_activation()
    assert r["grade"] == doctor.WARN and "child (claude_settings)" in r["detail"]


def test_an_unreadable_systems_json_is_a_warn_not_a_crash(machine, monkeypatch):
    plugin = machine / "plugins" / "jstack"
    plugin.mkdir(parents=True)
    (plugin / "systems.json").write_text("not json")
    from jstack_host import plugin_paths
    monkeypatch.setattr(plugin_paths, "jstack_root", lambda: plugin)
    r = doctor.check_activation()
    assert r["grade"] == doctor.WARN
    assert "cannot read the systems registry" in r["detail"]


# ── the timeline check must not report agreement it cannot observe ──

def _fake_log_event(tmp_path, prints: str) -> Path:
    binary = tmp_path / "log_event"
    binary.write_text("#!/bin/sh\n" + prints + "\n")
    binary.chmod(0o755)
    return binary


def test_a_split_timeline_is_a_failure_not_a_fresh_install(machine, monkeypatch, tmp_path):
    """The defect this check exists for, from a machine rooted at ~/Alpine.

    The host derived `$HOME/Logs/Timeline` and `log_event` wrote the declared
    root's. Both sides worked. The doctor printed "no store yet — the first
    session that logs an entry creates it" beside a populated database, because
    an empty store IS a legitimate state and it had no second opinion to weigh
    it against. Now it asks the writer, and a disagreement is a fail.
    """
    from jstack_host import timeline as tl
    writer_db = tmp_path / "Alpine" / "Logs" / "Timeline" / "timeline.db"
    binary = _fake_log_event(tmp_path, f"echo {writer_db}")
    monkeypatch.setattr(tl, "log_event_bin", lambda: binary)
    monkeypatch.setattr(hostenv, "timeline_db", lambda: tmp_path / "home" / "Logs" / "Timeline" / "timeline.db")
    result = doctor.check_timeline()
    assert result["grade"] == doctor.FAIL
    assert str(writer_db) in result["detail"] and "this host reads" in result["detail"]


def test_an_agreeing_timeline_says_so(machine, monkeypatch, tmp_path):
    from jstack_host import timeline as tl
    db = tmp_path / "Logs" / "Timeline" / "timeline.db"
    db.parent.mkdir(parents=True)
    db.write_text("")
    binary = _fake_log_event(tmp_path, f"echo {db}")
    monkeypatch.setattr(tl, "log_event_bin", lambda: binary)
    monkeypatch.setattr(hostenv, "timeline_db", lambda: db)
    result = doctor.check_timeline()
    assert result["grade"] == doctor.OK and "log_event agrees" in result["detail"]


def test_an_unaskable_writer_is_not_graded_as_agreement(machine, monkeypatch, tmp_path):
    """An older plugin has no `where` and exits 2 on it. That is a missing
    probe, not a disagreement — and equally not a cross-check that passed."""
    from jstack_host import timeline as tl
    db = tmp_path / "Logs" / "Timeline" / "timeline.db"
    db.parent.mkdir(parents=True)
    db.write_text("")
    binary = _fake_log_event(tmp_path, "echo 'usage: log_event ...' >&2; exit 2")
    monkeypatch.setattr(tl, "log_event_bin", lambda: binary)
    monkeypatch.setattr(hostenv, "timeline_db", lambda: db)
    result = doctor.check_timeline()
    assert result["grade"] == doctor.OK
    assert "could not be cross-checked" in result["hint"]


def test_codex_hooks_reads_the_plugin_the_machine_runs(tmp_path, monkeypatch):
    """The check reads the manifest from the plugin root, not beside the package.

    An installed Hub imports the package from inside the app bundle, where no
    plugins/ sits beside it; the plugin it should grade is the one Claude and
    Codex run, which plugin_paths resolves. Until 2026-09-27 the check looked
    beside the package and warned on every installed hub.
    """
    from jstack_host import codex_hooks, plugin_paths

    plugin = tmp_path / "cache" / "jstack"
    (plugin / "hooks").mkdir(parents=True)
    bundle = tmp_path / "Hub.app" / "Contents" / "Resources" / "packages"
    bundle.mkdir(parents=True)
    managed = tmp_path / "managed_config.toml"
    managed.write_text("[[hooks.x]]\n")
    seen = []

    def fake_managed_config(root, manifest_path=None):
        seen.append(Path(root))
        return "[[hooks.x]]\n", []

    monkeypatch.setattr(doctor, "_which", lambda name: "/usr/local/bin/codex")
    monkeypatch.setattr(hostenv, "package_root", lambda: bundle)
    monkeypatch.setattr(plugin_paths, "jstack_root", lambda: plugin)
    monkeypatch.setattr(codex_hooks, "managed_config", fake_managed_config)
    monkeypatch.setattr(codex_hooks, "MANAGED_CONFIG", managed)

    result = doctor.check_codex_hooks()

    assert seen == [plugin]
    assert result["grade"] == doctor.OK, result
