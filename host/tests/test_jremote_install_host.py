"""install_host — making a Mac a jRemote host without admin on it.

The installer is the whole client/host split on the host side: the app ships
client-only, and this is what Settings runs to turn the machine it is on into
an instance. It has to work without a password, which is why everything here
is a *user* LaunchAgent, and why the tests below care about domain and paths
as much as about behaviour.

Nothing here touches real launchd. `_launchctl` is the seam; the live probe
that proves launchd actually accepts the plist is run by hand and recorded in
`~/Systems/jremote/SYSTEM.md`.
"""

import os
import plistlib
import stat
from pathlib import Path

import pytest

from jstack_host import hostenv, install_host


@pytest.fixture(autouse=True)
def _clean_profile():
    hostenv.reset_profile()
    yield
    hostenv.reset_profile()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway HOME, so `~/Library/LaunchAgents` is under tmp."""
    h = tmp_path / "home"
    (h / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    return h


@pytest.fixture
def launchctl(monkeypatch):
    """Record every launchctl call instead of making one.

    It keeps one bit of the domain's state — whether the label is bootstrapped
    — because `print` is how `is_loaded` asks, and a stub that answers yes to a
    label nothing ever loaded makes the installer wait for a job to go away
    that was never there.
    """
    calls = []
    loaded = set()

    class Result:
        def __init__(self, returncode=0):
            self.returncode, self.stdout, self.stderr = returncode, "", ""

    def fake(*args):
        calls.append(list(args))
        verb, target = args[0], args[-1]
        label = target.rsplit("/", 1)[-1].removesuffix(".plist")
        if verb == "print":
            return Result(0 if label in loaded else 1)
        if verb == "bootout":
            loaded.discard(label)
        if verb == "bootstrap":
            loaded.add(label)
        return Result()

    monkeypatch.setattr(install_host, "_launchctl", fake)
    return calls


@pytest.fixture
def standalone(tmp_path, monkeypatch):
    """A host with its own state dir and no embedding tree assumptions."""
    state = tmp_path / "state"
    monkeypatch.setenv("JREMOTE_HOST_PROFILE", "default")
    monkeypatch.setenv("JREMOTE_STATE_DIR", str(state))
    monkeypatch.setenv("JREMOTE_INSTANCE_ROOT", str(tmp_path / "Agents"))
    (tmp_path / "Agents").mkdir()
    hostenv.reset_profile()
    return state


# ── the agent ──

def test_the_agent_is_a_user_agent(home):
    """No root, no admin, nothing outside the user's own tree.

    The single fact that makes an unattended install possible. If this path
    ever becomes /Library/LaunchDaemons, installing there prompts for a
    password, and an installer that can be refused is not an installer.
    """
    path = install_host.plist_path()
    assert path == home / "Library" / "LaunchAgents" / "com.jremote.host.plist"
    assert install_host._domain() == f"gui/{os.getuid()}"


def test_the_agent_runs_the_host_module_with_no_shell():
    """Args are a list, so a path with a space stays one argument."""
    job = plistlib.loads(install_host.render_plist(
        port=9191, bind="0.0.0.0", interpreter="/opt/py/bin/python3",
        working_dir=Path("/Users/x/My Tree"), logs=Path("/tmp/l")))

    assert job["ProgramArguments"] == [
        "/opt/py/bin/python3", "-m", "jstack_host.server",
        "--host", "0.0.0.0", "--port", "9191"]
    assert job["WorkingDirectory"] == "/Users/x/My Tree"
    assert job["Label"] == "com.jremote.host"


def test_the_agent_comes_back_by_itself(tmp_path):
    """A host nobody can reach after a reboot is not an instance."""
    job = plistlib.loads(install_host.render_plist(logs=tmp_path))
    assert job["RunAtLoad"] is True
    assert job["KeepAlive"] is True


def test_the_agent_is_not_a_job_launchd_may_throttle(tmp_path):
    """The host carries keystrokes and terminal frames, so launchd must not
    treat it as background work.

    The default class is `Standard`, which launchd is free to throttle to keep
    the foreground responsive — on a Mac mid-Xcode-build, which is the one time
    anyone notices. Asserting the literal and not merely "a ProcessType is
    set", because `Adaptive` and `Background` would both satisfy the weaker
    check and both leave the throttling in place.
    """
    job = plistlib.loads(install_host.render_plist(logs=tmp_path))
    assert job["ProcessType"] == "Interactive"


def test_the_agent_can_find_the_binaries_it_spawns(tmp_path):
    """launchd hands a job almost no PATH, and the host spawns `claude`.

    Off the one seam, never a list written in the installer. Six hand-copied
    PATHs is exactly what went down on 2026-07-09 when the binary moved to
    ~/.local/bin — see tests/test_spawn_path.py, which also refuses a literal
    here.
    """
    job = plistlib.loads(install_host.render_plist(logs=tmp_path))
    assert job["EnvironmentVariables"]["PATH"] == hostenv.spawn_path()


def test_the_agent_logs_where_the_host_keeps_its_state(standalone, tmp_path):
    job = plistlib.loads(install_host.render_plist())
    assert job["StandardErrorPath"] == str(standalone / "logs" / "host.err")
    assert job["StandardOutPath"] == str(standalone / "logs" / "host.out")


def test_the_agent_resolves_the_way_the_installer_did(monkeypatch, tmp_path):
    """A launchd job inherits nothing from the shell that installed it.

    Provision with JREMOTE_TOKEN_PATH exported and the installer writes the
    token there — then the agent starts with a bare environment, resolves a
    different path, finds no token, and serves an instance nobody can log into.
    """
    monkeypatch.setenv("JREMOTE_TOKEN_PATH", str(tmp_path / "tok"))
    monkeypatch.setenv("JREMOTE_HOST_NAME", "Laptop")
    monkeypatch.setenv("SOME_OTHER_THING", "not ours")

    env = plistlib.loads(install_host.render_plist(
        logs=tmp_path))["EnvironmentVariables"]

    assert env["JREMOTE_TOKEN_PATH"] == str(tmp_path / "tok")
    assert env["JREMOTE_HOST_NAME"] == "Laptop"
    assert "SOME_OTHER_THING" not in env


def test_an_explicit_state_dir_wins_over_the_inherited_one(monkeypatch, tmp_path):
    monkeypatch.setenv("JREMOTE_STATE_DIR", str(tmp_path / "inherited"))
    env = plistlib.loads(install_host.render_plist(
        state_dir=tmp_path / "asked", logs=tmp_path))["EnvironmentVariables"]
    assert env["JREMOTE_STATE_DIR"] == str(tmp_path / "asked")


@pytest.mark.parametrize("explicit", [False, True])
def test_session_environment_never_becomes_daemon_configuration(monkeypatch, tmp_path, explicit):
    transient = {"JREMOTE_SID": "session", "JREMOTE_COMPOSE_DIR": "/tmp/session",
                 "JREMOTE_PHONE_CLIENT": "1", "JREMOTE_TMUX_SOCK": "test-socket",
                 "JREMOTE_PAYLOAD__": "payload"}
    durable = {"JREMOTE_HOST_NAME": "Laptop", "JSTACK_ROOT": str(tmp_path / "root"),
               "WG_ENDPOINT": "example.test:51820"}
    for key, value in {**transient, **durable}.items():
        monkeypatch.setenv(key, value)
    rendered = install_host.render_plist(logs=tmp_path,
        environment={**transient, **durable} if explicit else None)
    env = plistlib.loads(rendered)["EnvironmentVariables"]
    assert all(key not in env for key in transient)
    assert all(env[key] == value for key, value in durable.items())
    path = tmp_path / "old.plist"
    path.write_bytes(plistlib.dumps({"EnvironmentVariables": {**transient, **durable}}))
    assert install_host.installed_environment(path) == durable
    embedded = {**transient, **durable, "PYTHONPATH": "/embedding", "APP_CONFIG": "/config"}
    assert install_host.upgraded_environment(embedded) == {
        **durable, "PYTHONPATH": "/embedding", "APP_CONFIG": "/config"}


def test_the_state_dir_is_pinned_even_when_nobody_asked_for_one(monkeypatch, tmp_path):
    """The default is a resolution, and both sides must not do it twice.

    Nothing is passed and nothing is exported, so the plist could leave this
    out and let the job work it out at launch. It must not: launchd builds
    HOME from the user record, not from the shell that installed, and any
    disagreement produces a host that comes up, answers `/api/health`, and
    401s every route as unprovisioned — while the token sits on disk exactly
    where the installer put it and printed it. Caught by installing a payload
    under a throwaway HOME, which is also how the second Mac gets tested.
    """
    monkeypatch.delenv("JREMOTE_STATE_DIR", raising=False)
    env = plistlib.loads(install_host.render_plist(
        logs=tmp_path))["EnvironmentVariables"]
    assert env["JREMOTE_STATE_DIR"] == str(hostenv.state_dir())


def test_the_agents_root_is_pinned_so_the_daemon_does_not_scan_home(monkeypatch, tmp_path):
    """The blank-thread bug, at its source. launchd hands the daemon none of
    the installing shell's `$JSTACK_ROOT`, so an unpinned agents root fell
    through to `$HOME/Agents` — absent on a `--root` install — and the daemon
    read the whole home directory as agents. The plist must carry the root the
    installer resolved, so the daemon sees the same tree doctor just did."""
    monkeypatch.delenv("JREMOTE_INSTANCE_ROOT", raising=False)
    monkeypatch.setenv("JSTACK_ROOT", str(tmp_path / "jstack-root"))
    hostenv.reset_profile()
    env = plistlib.loads(install_host.render_plist(
        logs=tmp_path))["EnvironmentVariables"]
    assert env["JREMOTE_INSTANCE_ROOT"] == str(tmp_path / "jstack-root" / "Agents")


def test_an_explicit_instance_root_wins_over_the_derived_one(monkeypatch, tmp_path):
    monkeypatch.setenv("JSTACK_ROOT", str(tmp_path / "jstack-root"))
    monkeypatch.setenv("JREMOTE_INSTANCE_ROOT", str(tmp_path / "asked" / "Agents"))
    hostenv.reset_profile()
    env = plistlib.loads(install_host.render_plist(
        logs=tmp_path))["EnvironmentVariables"]
    assert env["JREMOTE_INSTANCE_ROOT"] == str(tmp_path / "asked" / "Agents")


# ── the token ──

def test_the_token_is_readable_only_by_its_owner(tmp_path):
    path = tmp_path / "deep" / "api-token"
    token, minted = install_host.mint_token(path)

    assert minted and len(token) >= 32
    assert path.read_text() == token
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_reinstalling_does_not_lock_out_the_phone(tmp_path):
    """The token on disk is the one every paired device already carries.

    Re-mint it on a second install and every device that was working a second
    earlier is refused — and it reads as the install having broken the host.
    """
    path = tmp_path / "api-token"
    first, _ = install_host.mint_token(path)
    again, minted = install_host.mint_token(path)

    assert again == first
    assert not minted


def test_an_empty_token_file_is_no_token(tmp_path):
    path = tmp_path / "api-token"
    path.write_text("  \n")
    token, minted = install_host.mint_token(path)
    assert minted and token.strip() == token and token


# ── install ──

def test_it_refuses_to_land_on_a_port_already_answering(
        home, standalone, launchctl, monkeypatch, capsys):
    """The dashboard serves this same API on 9090 on this Mac.

    Refusing here rather than letting `server.acquire_lock` refuse a moment
    later is the difference between no change and a LaunchAgent that exists,
    is loaded, and crash-loops forever under KeepAlive.

    It answers the same `service` a host does, so what tells it apart is the
    label: the dashboard runs under its own, not `com.jremote.host`.
    """
    monkeypatch.setattr(install_host, "port_answers", lambda *a, **k: True)
    monkeypatch.setattr(install_host, "health",
                        lambda *a, **k: {"service": "jremote-host"})
    monkeypatch.setattr(install_host, "is_loaded", lambda *a, **k: False)

    assert install_host.install(port=9090) == 1
    assert not install_host.plist_path().exists()
    assert launchctl == []
    assert "jremote-host" in capsys.readouterr().err


def test_re_running_on_a_host_upgrades_it_in_place(
        home, standalone, launchctl, monkeypatch, capsys):
    """A Mac already a host is the only Mac this installer ever upgrades.

    Its own agent is loaded and answering, which is exactly the state the
    refusal above used to reject — so every payload machine was stuck on the
    code it was first installed with, and `Install jRemote Host.command` could
    not keep the upgrade promise in its own header.
    """
    monkeypatch.setattr(install_host, "port_answers", lambda *a, **k: True)
    monkeypatch.setattr(install_host, "health",
                        lambda *a, **k: {"service": "jremote-host"})
    loaded = iter([True, False])
    monkeypatch.setattr(install_host, "is_loaded",
                        lambda *a, **k: next(loaded, False))
    monkeypatch.setattr(install_host, "wait_for_health",
                        lambda *a, **k: {"profile": "default"})

    assert install_host.install(port=9090) == 0
    assert [c[0] for c in launchctl] == ["bootout", "bootstrap", "kickstart"]
    assert "upgrading" in capsys.readouterr().out


def test_bootstrap_is_retried_through_the_bootout_gap(
        home, standalone, monkeypatch):
    """`bootout` returns queued, not done, and the gap is a hard error.

    On a Mac that is already a host the first bootstrap is the one most likely
    to hit it, because the job being replaced is the one answering.
    """
    calls = []

    class Result:
        def __init__(self, rc):
            self.returncode, self.stdout, self.stderr = rc, "", "5: Input/output error"

    def fake(*args):
        calls.append(list(args))
        if args[0] != "bootstrap":
            return Result(0)
        return Result(0 if len([c for c in calls if c[0] == "bootstrap"]) > 2 else 1)

    monkeypatch.setattr(install_host, "_launchctl", fake)
    monkeypatch.setattr(install_host, "port_answers", lambda *a, **k: False)
    monkeypatch.setattr(install_host, "is_loaded", lambda *a, **k: False)
    monkeypatch.setattr(install_host, "wait_for_health",
                        lambda *a, **k: {"profile": "default"})
    monkeypatch.setattr(install_host.time, "sleep", lambda *_: None)

    assert install_host.install(port=9191) == 0
    assert [c[0] for c in calls].count("bootstrap") == 3


def test_a_failed_upgrade_puts_the_old_host_back(
        home, standalone, monkeypatch, capsys):
    """Never leave a reachable Mac with no agent.

    The old code booted the running host out, wrote a plist launchd would not
    take, then deleted it — down now, and still down after the next login, on
    the one machine nobody here can walk over to.
    """
    calls = []

    class Result:
        def __init__(self, rc=0):
            self.returncode, self.stdout, self.stderr = rc, "", "nope"

    monkeypatch.setattr(install_host, "port_answers", lambda *a, **k: False)
    monkeypatch.setattr(install_host, "is_loaded", lambda *a, **k: False)
    monkeypatch.setattr(install_host.time, "sleep", lambda *_: None)

    path = install_host.plist_path()
    path.write_bytes(b"<plist>the one that was working</plist>")

    def fake(*args):
        calls.append(list(args))
        # Only the new plist is refused; putting the old one back works.
        refuse = args[0] == "bootstrap" and path.read_bytes().startswith(b"<?xml")
        return Result(1 if refuse else 0)

    monkeypatch.setattr(install_host, "_launchctl", fake)

    assert install_host.install(port=9191) == 1
    assert path.read_bytes() == b"<plist>the one that was working</plist>"
    assert [c[0] for c in calls][-1] == "kickstart"
    assert "put back" in capsys.readouterr().err


def test_force_lands_anyway(home, standalone, launchctl, monkeypatch):
    monkeypatch.setattr(install_host, "port_answers", lambda *a, **k: True)
    monkeypatch.setattr(install_host, "health", lambda *a, **k: {"service": "x"})
    monkeypatch.setattr(install_host, "wait_for_health",
                        lambda *a, **k: {"profile": "default"})

    assert install_host.install(port=9090, force=True) == 0
    assert install_host.plist_path().exists()


def test_installing_loads_the_agent_and_waits_for_it(
        home, standalone, launchctl, monkeypatch, capsys):
    monkeypatch.setattr(install_host, "port_answers", lambda *a, **k: False)
    monkeypatch.setattr(install_host, "wait_for_health",
                        lambda *a, **k: {"profile": "default", "provisioned": True})

    assert install_host.install(port=9191) == 0

    verbs = [c[0] for c in launchctl]
    # bootout first: without it a re-install bootstraps onto the job already
    # loaded and the plist just written is never read. The `print` between is
    # the wait asking whether it has actually gone.
    assert verbs == ["bootout", "print", "bootstrap", "kickstart"]
    assert launchctl[2][2] == str(install_host.plist_path())

    job = plistlib.loads(install_host.plist_path().read_bytes())
    assert job["ProgramArguments"][-1] == "9191"
    assert (standalone / "logs").is_dir()
    assert hostenv.token_path().exists()
    assert hostenv.token_path().read_text() in capsys.readouterr().out


def test_a_host_that_never_answers_is_a_failed_install(
        home, standalone, launchctl, monkeypatch, capsys):
    """Reported as failed, not as done.

    launchctl bootstrap succeeds against a plist whose interpreter is missing
    — the job is loaded and dies on every spawn. Only the health poll can tell
    the difference, so its verdict is the install's verdict.
    """
    monkeypatch.setattr(install_host, "port_answers", lambda *a, **k: False)
    monkeypatch.setattr(install_host, "wait_for_health", lambda *a, **k: None)

    assert install_host.install(port=9191) == 1
    assert "host.err" in capsys.readouterr().err


def test_a_refused_bootstrap_leaves_nothing_to_load_at_next_login(
        home, standalone, monkeypatch, capsys):
    class Refused:
        returncode = 1
        stdout = ""
        stderr = "Load failed: 5: Input/output error"

    monkeypatch.setattr(install_host, "port_answers", lambda *a, **k: False)
    # Nothing is loaded — the bootstrap is what keeps being refused — so
    # `print` says so rather than making the wait sit out its whole budget.
    monkeypatch.setattr(install_host, "_launchctl",
                        lambda *a: Refused() if a[0] in ("bootstrap", "print")
                        else _ok())
    monkeypatch.setattr(install_host.time, "sleep", lambda *_: None)

    assert install_host.install(port=9191) == 1
    # Anything in ~/Library/LaunchAgents is loaded at the next login whether
    # bootstrap took it or not. A failed install that starts the host hours
    # later is worse than one that starts it now.
    assert not install_host.plist_path().exists()


def _ok():
    class R:
        returncode = 0
        stdout = ""
        stderr = ""
    return R()


# ── uninstall / status ──

def test_uninstall_keeps_the_machine_it_was(home, standalone, launchctl, capsys):
    """Off the mesh for an afternoon, back as the same instance.

    Delete the token here and every paired device is silently re-keyed; delete
    the state and the host comes back with a new `host-id`, which the app reads
    as a different machine at the same address.
    """
    install_host.plist_path().write_bytes(install_host.render_plist())
    token_path = hostenv.token_path()
    install_host.mint_token(token_path)
    host_id = hostenv.host_id()

    assert install_host.uninstall() == 0
    assert not install_host.plist_path().exists()
    assert [c[0] for c in launchctl] == ["bootout"]
    assert token_path.exists()
    assert hostenv.host_id() == host_id


def test_uninstalling_nothing_is_not_an_error(home, standalone, launchctl):
    assert install_host.uninstall() == 0


def test_status_never_prints_the_token(home, standalone, monkeypatch, capsys):
    monkeypatch.setattr(install_host, "health", lambda *a, **k: None)
    monkeypatch.setattr(install_host, "is_loaded", lambda *a, **k: False)
    token, _ = install_host.mint_token(hostenv.token_path())

    assert install_host.status() == 0
    out = capsys.readouterr().out
    assert token not in out
    assert str(hostenv.token_path()) in out
    assert "not installed" in out


def test_status_says_when_a_host_is_serving_without_a_token(
        home, standalone, monkeypatch, capsys):
    """`provisioned: false` is a host that will refuse every request.

    Worth saying out loud — from the app it looks exactly like a bad token on
    the phone, and that sends anyone debugging it to the wrong machine.
    """
    monkeypatch.setattr(install_host, "health",
                        lambda *a, **k: {"service": "jremote-host",
                                         "profile": "default",
                                         "provisioned": False})
    monkeypatch.setattr(install_host, "is_loaded", lambda *a, **k: True)

    install_host.status(port=9191)
    assert "NO TOKEN" in capsys.readouterr().out


def test_status_refuses_to_call_a_stranger_on_the_port_this_host(
        home, standalone, monkeypatch, capsys):
    """Something answering is not the same as our host answering.

    The port is a default, so an unrelated program holding it is ordinary. On
    the machine this was found on, the dashboard answered 9090 and `status`
    printed `serving 9090` — sending the reader looking for a fault in a host
    that was not there at all.
    """
    monkeypatch.setattr(install_host, "health",
                        lambda *a, **k: {"error": "unauthorized"})
    # No jRemote router on the port either — without this the test reaches the
    # real loopback and an embedded host on the developer's own machine answers.
    monkeypatch.setattr(install_host, "api_answers", lambda *a, **k: False)
    monkeypatch.setattr(install_host, "is_loaded", lambda *a, **k: True)

    install_host.status(port=9090)
    out = capsys.readouterr().out
    assert "NOT THIS HOST" in out
    assert "9090" in out


def test_status_reports_on_the_port_the_agent_was_installed_with(
        home, standalone, monkeypatch, capsys):
    """`--port 9099` at install time means status probes 9099, not the default.

    `installed_port` was written for this and `status` never called it, so a
    host on a non-default port was graded by whatever held the default one.
    """
    install_host.plist_path().write_bytes(install_host.render_plist(port=9099))
    probed: list[int] = []

    def _health(port, *a, **k):
        probed.append(port)
        return {"service": "jremote-host", "profile": "default",
                "provisioned": True}

    monkeypatch.setattr(install_host, "health", _health)
    monkeypatch.setattr(install_host, "is_loaded", lambda *a, **k: True)

    install_host.status()
    assert probed == [9099]
    assert "serving    9099" in capsys.readouterr().out


# ── the command line ──

def test_it_binds_the_mesh_by_default(home, standalone, monkeypatch):
    """A host bound to loopback is reachable only from the Mac it runs on.

    The mesh arrives on a tunnel interface, so 127.0.0.1 would make every
    remote instance unreachable — with a host that looks perfectly healthy
    from the machine you are standing at.
    """
    seen = {}
    monkeypatch.setattr(install_host, "install",
                        lambda **kw: seen.update(kw) or 0)
    assert install_host.main(["install"]) == 0
    assert seen["bind"] == "0.0.0.0"
    assert seen["port"] == 9090
    assert seen["force"] is False


def test_the_state_dir_flag_reaches_the_install(home, tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(install_host, "install",
                        lambda **kw: seen.update(kw) or 0)
    install_host.main(["install", "--state-dir", str(tmp_path / "s"),
                       "--port", "9191", "--label", "com.jremote.host.test"])
    assert seen["state_dir"] == tmp_path / "s"
    assert seen["label"] == "com.jremote.host.test"
    assert seen["port"] == 9191


def test_the_wait_ends_when_the_label_goes(monkeypatch):
    """The whole point of waiting on the fact instead of sleeping a guess."""
    seen = iter([True, True, False])
    monkeypatch.setattr(install_host, "is_loaded", lambda *a, **k: next(seen))
    monkeypatch.setattr(install_host.time, "sleep", lambda *_: None)
    assert install_host.wait_unloaded("com.jremote.host") is True


def test_the_wait_gives_up_rather_than_hanging(monkeypatch):
    """A job that never leaves must not hold the installer forever."""
    monkeypatch.setattr(install_host, "is_loaded", lambda *a, **k: True)
    monkeypatch.setattr(install_host.time, "sleep", lambda *_: None)
    assert install_host.wait_unloaded("com.jremote.host", seconds=0.0) is False


def test_status_names_an_embedded_host_instead_of_calling_it_absent(
        home, standalone, monkeypatch, capsys):
    """A host embedded in another server has no LaunchAgent by design.

    `status` used to print `not installed` and `nothing answered` about exactly
    that machine — read as "the host is gone" on a Mac whose host was up and
    serving all day. `/api/health` belongs to the embedding app there, so the
    bearer-gated router path is the only honest evidence.
    """
    monkeypatch.setattr(install_host, "health",
                        lambda *a, **k: {"dashboard": {"status": "up"}})
    monkeypatch.setattr(install_host, "api_answers", lambda *a, **k: True)
    monkeypatch.setattr(install_host, "is_loaded", lambda *a, **k: False)

    install_host.status(port=9090)
    out = capsys.readouterr().out
    assert "the jRemote API answers here" in out
    assert "NOT THIS HOST" not in out
    assert "nothing answered" not in out


def test_api_answers_reads_a_401_as_proof_the_router_is_mounted(monkeypatch):
    """The bearer gate replying IS the positive answer.

    Treating 401 as a failure is what made an embedded host look like an
    unrelated program holding the port.
    """
    import urllib.error

    def _raise(*a, **k):
        raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(install_host.urllib.request, "urlopen", _raise)
    assert install_host.api_answers(9090) is True

    def _raise_404(*a, **k):
        raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)

    monkeypatch.setattr(install_host.urllib.request, "urlopen", _raise_404)
    assert install_host.api_answers(9090) is False


def test_adopt_falls_through_to_the_embedded_marker(tmp_path, monkeypatch):
    """A host embedded in another server has no plist, so the marker is the
    only record of it — and before this was wired, `adopt` read the plist,
    found nothing, and left every command resolving package defaults against a
    host that was up and serving (#34)."""
    import json

    marker = tmp_path / "embedded.json"
    marker.write_text(json.dumps({
        "server": "the dashboard", "port": 9090, "root": "",
        "profile_module": "jstack_host_no_such_profile", "profile": "jj",
        "state_dir": str(tmp_path / "live"),
        "token_path": str(tmp_path / "live" / "api-token")}))
    # The whole environment, swapped for a copy this test owns. `adopt` calls
    # `os.environ.setdefault`, and `delenv(raising=False)` on a variable that is
    # not currently set records nothing to restore — so the value `adopt` then
    # creates survives teardown. It leaked `JREMOTE_STATE_DIR` and
    # `JREMOTE_TOKEN_PATH` at a tmp_path that pytest deletes, every later test
    # that shells out inherited them, and six subprocess tests in
    # test_jremote_standalone.py failed against a state dir that no longer
    # existed — naming neither this test nor the variable. Replacing the mapping
    # is what makes a setdefault this test cannot see in advance still die here.
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setenv("JREMOTE_EMBED_MARKER", str(marker))
    monkeypatch.setenv("JREMOTE_PROFILE_MODULE", "jstack_host_no_such_profile")
    monkeypatch.delenv("JREMOTE_STATE_DIR", raising=False)
    monkeypatch.delenv("JREMOTE_TOKEN_PATH", raising=False)

    install_host.adopt_installed_environment(tmp_path / "no-such.plist")

    assert hostenv.state_dir() == tmp_path / "live"
    assert hostenv.token_path() == tmp_path / "live" / "api-token"


def test_the_plist_outranks_the_marker_and_an_export_outranks_both(tmp_path, monkeypatch):
    """Precedence, in one place. A machine carrying both records has an agent
    of its own, and the agent is the installed host; the marker answers for the
    machine that has no plist. An explicit export still beats the pair."""
    import json

    marker = tmp_path / "embedded.json"
    marker.write_text(json.dumps({"server": "s", "port": 9090,
                                  "state_dir": str(tmp_path / "marked")}))
    monkeypatch.setenv("JREMOTE_EMBED_MARKER", str(marker))
    monkeypatch.setenv("JREMOTE_PROFILE_MODULE", "jstack_host_no_such_profile")
    monkeypatch.delenv("JREMOTE_STATE_DIR", raising=False)

    plist = tmp_path / "com.jremote.host.plist"
    plist.write_bytes(install_host.render_plist(state_dir=tmp_path / "installed"))
    install_host.adopt_installed_environment(plist)
    assert hostenv.state_dir() == tmp_path / "installed", "the plist outranks the marker"

    monkeypatch.setenv("JREMOTE_STATE_DIR", str(tmp_path / "exported"))
    hostenv.reset_profile()
    install_host.adopt_installed_environment(plist)
    assert hostenv.state_dir() == tmp_path / "exported", "the export outranks both"
