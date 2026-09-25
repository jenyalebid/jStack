"""Fixtures for the host package's own tests.

Two of these are autouse and both exist for the same reason: this package's
modules resolve their state at *call* time, against the real machine, and a
test that authenticates or opens a session would otherwise write into the
running host's live registry. The tests all pass either way — the damage lands
on the machine, not in the report, which is why the isolation is autouse and
not something each test opts into.

`app` is the standalone application, `server.create_app()`. When the host is
embedded in a larger process (this package is designed to be mountable) that
process builds its own; the package's own tests exercise the one the package
ships, so a route that only works when someone else mounts it fails here.
"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

#: The state dir for this whole run, and the reason it is a module statement
#: rather than a fixture.
#:
#: THE SUITE WAS WRITING INTO WHICHEVER STATE DIR THE MACHINE RESOLVED. Not a
#: fixture's tmp_path — the real one, the one a host is serving out of. The
#: four isolations below cover four call-time seams each found the hard way,
#: and `hostenv.state_dir()` is the seam under all of them: run from a checkout
#: with no profile module importable it answers `~/.local/state/jremote`, so
#: every run minted a `host-id` there, scanned this Mac's whole
#: `~/.claude/projects` into a 734K `token_usage/cache.json`, and appended its
#: `"not a clock"` fixture to `allowance_rejects.jsonl`. A directory that is
#: nobody's host then holds a second `host-id` for this machine, which the app
#: reads as a different host at the same address (#45). Run from a tree where
#: the profile *does* import — the embedding host's own checkout — the same
#: writes land in the live host's state dir instead. One hole, both dirs.
#:
#: A FIXTURE CANNOT CLOSE IT. Seven modules name a file in the state dir as a
#: module constant (`spend.CACHE`, `board._TURN_DIR`, …), bound the moment the
#: module is imported — which for a test module is collection, before any
#: fixture has run. An autouse fixture would redirect the call-time readers and
#: leave the constants pointing at the machine, which is worse than either
#: answer alone: half the suite isolated, half not, and nothing saying which.
#: pytest imports this file before it collects anything, so setting the
#: variable here is the one moment that is ahead of every binding.
#:
#: One directory for the session rather than one per test, for the same reason:
#: a constant binds once, so per-test dirs would be a promise only the
#: call-time half could keep. `test_jremote_isolation` pins both halves.
_STATE_DIR = Path(tempfile.mkdtemp(prefix="jstack-host-tests-state-"))
os.environ["JREMOTE_STATE_DIR"] = str(_STATE_DIR)


def pytest_sessionfinish(session, exitstatus):
    """Take the run's state dir away with it. Best-effort: a leftover temp
    directory is untidy, and failing the run over one would be worse."""
    shutil.rmtree(_STATE_DIR, ignore_errors=True)


@pytest.fixture(scope="session")
def shipped_security_alert():
    """`hostenv.security_alert` as the package ships it, read before the sink
    below replaces it — the only handle a test has on the real function.

    Every other test wants the sink; the one test that has to prove the
    *shipped* path refuses to deliver cannot use it, and by the time any test
    body runs the module attribute is already the sink. Ordering is not
    incidental: `_isolated_security_alerts` takes this as an argument so it
    cannot be set up first.
    """
    from jstack_host import hostenv

    return hostenv.security_alert


@pytest.fixture(scope="session", autouse=True)
def _isolated_security_alerts(shipped_security_alert):
    """Test alerts stay in memory, never in the machine's real notifier.

    Session scope also covers delayed alert threads between test fixtures.
    Tests can still replace this sink to assert their exact alert payload.

    Belt to `hostenv.in_test_process`'s braces: that guard is the one a stale
    checkout cannot opt out of, this one keeps the payloads inspectable.
    """
    from jstack_host import hostenv

    captured = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(hostenv, "security_alert", captured.append)
        yield captured


# ── The suite's scheduler is an empty install ───────────────────────────────
#
# Module scope, not a fixture, because the thing this protects binds at
# *import* and a test module is imported before any fixture of its own runs.
#
# `tests/test_codex_parity.py` imports jStack's scheduler package in-process
# (`from scheduler import runner`). That package bootstraps the machine's
# `python_path` onto `sys.path` as it imports — by design, so a live daemon can
# load an install's own workspace resolver — and on a machine that runs one,
# the entry is the embedding tree. From that moment `jremote_host_profile` is
# importable to the whole pytest process, so the next `hostenv.reset_profile()`
# (there are twenty of them, in eight files) re-resolves `auto` to the
# machine's *live* profile and caches it. Every later test that reaches a
# profile method touching the embedding dashboard then dies inside it — ten
# tests in two unrelated files, green alone, red in a full run, naming neither
# the scheduler nor the profile.
#
# One `sys.path.append` in a shared interpreter is not undoable, so the fix is
# to leave nothing to append: point the scheduler at a home it has never been
# installed into. Its config reader answers built-in defaults for a missing
# file, which is what a package's own tests should be asserting against anyway
# — a unit test whose answer depends on the operator's `scheduler.json` is
# already reporting on the wrong machine.
#
# A fresh temporary root cannot inherit another test process's install.
# The explicit overrides go too — setting
# the home alone leaves an exported `SCHEDULER_CONFIG_DIR` still pointing at
# the live install.
for _override in ("SCHEDULER_CONFIG_DIR", "SCHEDULER_STATE_DIR",
                  "SCHEDULER_CREDENTIALS_DIR", "SCHEDULER_INSTALL_FILE"):
    os.environ.pop(_override, None)
_scheduler_home = tempfile.TemporaryDirectory(prefix="jstack-host-test-scheduler-")
os.environ["SCHEDULER_HOME"] = _scheduler_home.name


@pytest.fixture(autouse=True)
def _no_live_fileshare_audit(monkeypatch):
    """A TestClient lifespan must never inspect the developer's share points."""
    import asyncio
    from jstack_host import fileshare

    async def idle():
        await asyncio.Event().wait()

    monkeypatch.setattr(fileshare, "audit_loop", idle)


#: launchctl verbs that change what is loaded. `print`/`list` stay allowed.
LAUNCHCTL_MUTATORS = {"bootout", "bootstrap", "kickstart", "load", "unload", "remove",
                      "enable", "disable", "submit", "stop", "start"}

#: This is the home machine when the production Hub is installed on it. A test
#: that exercises a destructive install path for real (uninstall, purge,
#: bootout, unregister) is refused here outright — the proof of such an
#: operation runs in a lab guest (`vm.sh`), never on the Hub the fleet
#: depends on. Mark such a test `@destructive`.
HOME_MACHINE = Path("/Applications/jStack Hub.app").exists()
destructive = pytest.mark.skipif(
    HOME_MACHINE, reason="destructive install test: never runs on the home machine, only in a lab guest")


@pytest.fixture(autouse=True)
def _no_live_desktop_launches(monkeypatch):
    """A missing mock must fail here, never open the developer's real app."""
    import os
    import shlex
    import shutil
    import subprocess

    original = subprocess.Popen

    def guarded(args, *positional, **kwargs):
        argv = shlex.split(args) if isinstance(args, str) else args
        if argv:
            program = os.fsdecode(kwargs.get("executable") or argv[0])
            env = kwargs.get("env") or os.environ
            resolved = shutil.which(program, path=env.get("PATH"))
            if resolved and os.path.realpath(resolved) == "/usr/bin/open":
                raise AssertionError("Test attempted to launch a real desktop app; mock this boundary")
            # The same rule for the service layer. On 2026-09-24 a test that
            # stubbed `control` but not `subprocess` ran a real `launchctl
            # bootout` of every live.jstack.hub role on the home machine — the
            # production Hub — and the updater, menu bar and host were gone
            # until someone re-registered them by hand. A missing mock fails
            # the test; it never reaches launchd, the Hub binary or sudo.
            base = os.path.basename(os.path.realpath(resolved)) if resolved else os.path.basename(program)
            verbs = {str(a) for a in argv[1:]}
            if base == "launchctl" and verbs & LAUNCHCTL_MUTATORS:
                raise AssertionError(
                    f"Test attempted a real `launchctl {' '.join(map(str, argv[1:]))}`; mock this boundary "
                    "— destructive service tests never run on the home machine")
            if base in {"JStackHub", "JStackRuntime", "tccutil", "sudo"}:
                raise AssertionError(
                    f"Test attempted to run the real {base}; mock this boundary "
                    "— destructive service tests never run on the home machine")
        return original(args, *positional, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded)


@pytest.fixture
def app():
    """The standalone host app, built fresh per test.

    Fresh because `create_app()` closes over module state that other fixtures
    monkeypatch — a module-level singleton would capture whichever test built
    it first.
    """
    from jstack_host.server import create_app
    return create_app()


@pytest.fixture(autouse=True)
def _isolate_devices(tmp_path, monkeypatch):
    """Every test authenticates against its own device table, never the real
    store's — auth reads (and on first use writes) the `devices` table, and a
    TestClient request must not fold a test token into this machine's live
    registry. Lazy: only tests that actually authenticate pay for a store."""
    from jstack_host import auth, devices

    holder = {}

    def _test_store():
        if "s" not in holder:
            from jstack_host.store import SessionStore
            holder["s"] = SessionStore(db_path=tmp_path / "devices-probe.sqlite")
        return holder["s"]

    monkeypatch.setattr(devices, "_store", _test_store)
    devices.reset_for_tests()
    auth.reset_limiter()


@pytest.fixture(autouse=True)
def _isolate_embed_marker(tmp_path, monkeypatch):
    """No test reads the embedded-host marker this machine actually has.

    `adopt_installed_environment` consults it, so every command that adopts —
    which is all of them — now resolves against whatever host is embedded on
    the developer's Mac. On a machine running one, that silently swapped the
    profile mid-suite and `tunnel.WG_DIR` stopped matching the default the
    package had already bound at import: one failure, in an unrelated file,
    naming neither the marker nor the test that triggered it.

    The fourth call-time seam, isolated for the same reason as the three
    around it. A path inside `tmp_path` rather than one known-absent: a test
    that means to exercise a marker writes it here and gets a real file, and
    the suite still never touches the one on the machine.
    """
    monkeypatch.setenv("JREMOTE_EMBED_MARKER", str(tmp_path / "embedded.json"))


@pytest.fixture(autouse=True)
def _isolate_open_registry(tmp_path, monkeypatch):
    """Point the open-session registry at a temp file for every test.

    `managed._reg_mutate` is load-modify-SAVE: any test that reaches
    `record_open`/`record_close` — even via a monkeypatched `_reg_load` —
    rewrites the whole file at `_REG`. Against the real path that wipe is
    invisible to the tests and lands on whoever is using the host: every open
    session's registry entry is gone, so the board demotes them all to
    watch-only windows while their sessions run on untouched."""
    from jstack_host import managed
    monkeypatch.setattr(managed, "_REG", tmp_path / "jremote_open.json")


@pytest.fixture(autouse=True)
def _isolate_session_index(tmp_path, monkeypatch):
    """Every test reads its own session index, never the machine's.

    The third call-time seam, and the one that bit: `messages._find_session_file`
    tries four sources in order, and the live index sits ahead of the codex
    root a test can patch. So a test that pointed `codex_transcript.root` at
    `tmp_path` still got a real answer — the index knew that session id and
    handed back the running machine's own transcript, thirteen megabytes of it.
    It read as a passing test anywhere that session had never existed.

    Lazy, like `_isolate_devices`: only tests that actually reach the index pay
    for a store. Every `get_store` caller in the package imports it inside the
    function, so patching the module attribute catches all of them.

    It defers to an injected `_store` first because that is what the real
    `get_store` does, and `test_jremote_store` hands itself a store that way —
    isolation that quietly ignored the package's own injection point would be
    swapping one wrong answer for another.
    """
    from jstack_host import store

    holder = {}

    def _test_store():
        if store._store is not None:
            return store._store
        if "s" not in holder:
            holder["s"] = store.SessionStore(db_path=tmp_path / "index-probe.sqlite")
        return holder["s"]

    monkeypatch.setattr(store, "get_store", _test_store)
    monkeypatch.setattr(store, "_store", None)


def embedding_tree() -> object | None:
    """The host tree this package is embedded in, or None.

    Some tests here are integration tests against *that* tree — the machine's
    own agent registry, its dashboard app. They are real tests and they pass
    where the tree exists; from a standalone checkout there is nothing for
    them to integrate with, and skipping is the honest answer. Deleting them
    would be the dishonest one: the coupling they pin is still a coupling.
    """
    try:
        from lib import agents
        return agents
    except Exception:
        return None


needs_embedding_tree = pytest.mark.skipif(
    embedding_tree() is None,
    reason="no embedding tree on this machine — integration test, see conftest")


@pytest.fixture(autouse=True)
def _isolate_codex_rollouts(tmp_path, monkeypatch):
    from jstack_host import allowance, codex_transcript
    monkeypatch.setattr(allowance, "CODEX_SESSIONS", tmp_path / "codex-sessions")
    monkeypatch.setattr(codex_transcript, "root", lambda: tmp_path / "codex-sessions")


@pytest.fixture(autouse=True)
def _isolate_turn_markers(tmp_path, monkeypatch):
    from jstack_host import board
    monkeypatch.setattr(board, "_TURN_DIR", tmp_path / "turn-markers")
