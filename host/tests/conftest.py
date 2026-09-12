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

import pytest


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
