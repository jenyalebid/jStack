"""The session environment's three layers, and its silence at rest.

The failure worth pinning is not "does a string round-trip". It is a setting
that speaks when it has nothing to say: every default maps to an empty
instruction, so a host where nobody has set anything must inject literally
nothing into any session. Two tests below exist only for that, and they are the
ones to read first when a setting is added.

The rest pin the layer walk (a session value shadows the agent's, clearing it
hands back to the agent and NOT to the default — the difference between "no
opinion here" and "off", which a row holding `''` would collapse) and the two
refusals, each checked for having written nothing rather than merely raised.

Then `announced` — a filesystem fact, not a store one — whose failure is
reporting a sentence as delivered that nothing emitted: a default, or a value
since flipped away from. The last two hold its spelling to the hooks' own copy.
"""

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

from jstack_host import environment as env
from jstack_host import markers, store
from jstack_host.store import SessionStore

SESSION = "sess-1"
AGENT = "ops-chat"
PLUGIN = Path(__file__).resolve().parents[2] / "plugins/jstack"


@pytest.fixture(autouse=True)
def env_store(tmp_path, monkeypatch):
    """A store of this test's own, through the package's own injection point.

    `store._store` is what `get_store` consults first, which conftest's
    isolation also defers to — so this wins without fighting it, and the
    module under test resolves its store the same way in the test as on the
    machine.
    """
    monkeypatch.setattr(store, "_store", SessionStore(db_path=tmp_path / "env.sqlite"))


@pytest.fixture(autouse=True)
def marker_root(tmp_path, monkeypatch):
    """The marker root, moved off the machine's.

    Autouse and not opt-in: `announced` reads a filesystem path, and a test
    that forgot this would answer from whatever the real sessions of this Mac
    have left in /tmp — passing or failing for a reason no one wrote.
    """
    monkeypatch.setenv("JSTACK_CACHE_ROOT", str(tmp_path / "cache"))


def _announce(session_id: str, key: str, value: str) -> None:
    """Write a marker the way `env-announce.py` does — through the host helper.

    The offset written is what the hook writes (transcript bytes at the moment
    it spoke); nothing here reads it, and `announced` must answer on the file's
    existence rather than its contents, because a marker written at offset 0 is
    still a sentence that was said.
    """
    marker = env.announce_marker(session_id, key, value)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("0")


@pytest.fixture
def session_row():
    """The session→agent link `resolve` walks when no agent is passed."""
    with store.get_store().conn() as db:
        db.execute("INSERT INTO sessions (session_id, agent_id) VALUES (?,?)",
                   (SESSION, AGENT))


def test_three_layers(session_row):
    assert env.resolve(SESSION)["delivery_method"] == ("none", "default")

    env.set_value("delivery_method", "build", agent_id=AGENT)
    assert env.resolve(SESSION)["delivery_method"] == ("build", "agent")

    env.set_value("delivery_method", "distribute", session_id=SESSION)
    assert env.resolve(SESSION)["delivery_method"] == ("distribute", "session")
    assert env.get("delivery_method", agent_id=AGENT) == "build"


def test_clearing_a_session_value_falls_back_to_the_agent(session_row):
    env.set_value("sim_verify", "off", agent_id=AGENT)
    env.set_value("sim_verify", "on", session_id=SESSION)
    assert env.resolve(SESSION)["sim_verify"] == ("on", "session")

    env.set_value("sim_verify", None, session_id=SESSION)
    assert env.get("sim_verify", session_id=SESSION) == ""
    assert env.resolve(SESSION)["sim_verify"] == ("off", "agent")


def test_unknown_session_resolves_to_defaults_without_an_agent():
    """A hook firing on a session the index has not written yet must not raise."""
    assert env.resolve("never-indexed") == {
        s.key: (s.default, "default") for s in env.SETTINGS}


def test_unknown_key_raises_and_writes_nothing(session_row):
    with pytest.raises(ValueError):
        env.set_value("sim_verfiy", "off", session_id=SESSION)
    with store.get_store().conn() as db:
        assert db.execute("SELECT count(*) FROM session_env").fetchone()[0] == 0


def test_value_outside_the_enum_raises_and_leaves_the_old_value(session_row):
    env.set_value("delivery_method", "testflight", session_id=SESSION)
    with pytest.raises(ValueError):
        env.set_value("delivery_method", "carrier_pigeon", session_id=SESSION)
    assert env.get("delivery_method", session_id=SESSION) == "testflight"


def test_a_default_says_nothing_at_all(session_row):
    for s in env.SETTINGS:
        assert env.announce(s.key, s.default) == ""
    assert env.announce("sim_verify", "no-such-value") == ""
    assert env.announce("no_such_key", "on") == ""
    assert env.state_line(env.resolve(SESSION)) == ""


def test_state_line_carries_only_what_moved(session_row):
    env.set_value("sim_verify", "off", session_id=SESSION)
    env.set_value("use_subagents", "on", agent_id=AGENT)
    assert env.state_line(env.resolve(SESSION)) == (
        "SESSION ENVIRONMENT: sim_verify=off · use_subagents=on")


def test_delta_lines_one_per_moved_key():
    before = {"delivery_method": "none", "sim_verify": "on", "use_subagents": "off"}
    after = dict(before, delivery_method="distribute", sim_verify="off")
    assert env.delta_lines(before, after) == [
        "DELIVERY METHOD SWITCHED: DISTRIBUTE",
        "SIM VERIFY TURNED OFF",
    ]
    assert env.delta_lines(before, before) == []
    assert env.delta_lines(after, after) == []


def test_every_non_default_value_has_a_sentence():
    """A value the app can pick and the hook cannot announce is a dead switch."""
    for s in env.SETTINGS:
        assert set(s.instruction) == set(s.values)
        assert s.default in s.values
        for value in s.values:
            assert bool(s.instruction[value]) == (value != s.default)


def test_prefs_describes_the_registry():
    assert env.prefs() == [
        {"key": s.key, "label": s.label, "kind": s.kind,
         "values": list(s.values), "default": s.default} for s in env.SETTINGS]


def test_an_unusable_value_reads_as_silence_at_both_readers():
    """`get` and `resolve` must agree about the same row.

    A value outside the setting's current `values` is the residue of an enum
    that lost a member while rows holding it stayed. `resolve` already drops it.
    If `get` handed it back raw, the two readers would answer differently about
    one row and every caller would have to know which it was holding — and the
    one reading raw would surface a mode to the model that no instruction
    exists for.
    """
    with store.get_store().conn() as db:
        db.execute("INSERT INTO session_env (session_id, key, value, updated_at)"
                   " VALUES (?,?,?,?)", (SESSION, "delivery_method", "carrier_pigeon", 0))

    assert env.get("delivery_method", session_id=SESSION) == ""
    resolved = env.resolve(SESSION)
    assert resolved["delivery_method"] == ("none", "default")
    assert env.state_line(resolved) == ""


def test_a_usable_value_still_comes_back_from_get():
    """The filter must not swallow the values it is there to let through."""
    env.set_value("delivery_method", "distribute", session_id=SESSION)
    assert env.get("delivery_method", session_id=SESSION) == "distribute"
    assert env.get("delivery_method", agent_id=AGENT) == ""


def test_a_marker_makes_a_moved_setting_announced(session_row):
    """The claim the Work screen makes: this one has actually been said."""
    env.set_value("sim_verify", "off", session_id=SESSION)
    assert env.announced(SESSION)["sim_verify"] is False

    _announce(SESSION, "sim_verify", "off")
    assert env.announced(SESSION)["sim_verify"] is True
    assert env.announced(SESSION)["delivery_method"] is False


def test_a_marker_for_the_old_value_does_not_announce_the_new_one(session_row):
    """A flip re-arms the line, and until it fires nobody has been told.

    The hook keys its marker on the value for exactly this reason. Read on the
    setting alone, the marker the first value left would report the second as
    delivered — in the session that has heard the first and nothing since.
    """
    env.set_value("delivery_method", "distribute", session_id=SESSION)
    _announce(SESSION, "delivery_method", "distribute")
    assert env.announced(SESSION)["delivery_method"] is True

    env.set_value("delivery_method", "build", session_id=SESSION)
    assert env.announced(SESSION)["delivery_method"] is False

    _announce(SESSION, "delivery_method", "build")
    assert env.announced(SESSION)["delivery_method"] is True


def test_a_default_is_never_announced_whatever_is_on_disk(session_row):
    """A default has no sentence, so nothing about it can have been said.

    The marker here is residue: a value set, announced, then cleared. The
    setting is back at its default, `instruction[default]` is `""`, and a hook
    would never write this file for it again — reporting it as announced would
    mark a session as having been told something no renderer ever emitted.
    """
    env.set_value("sim_verify", "off", session_id=SESSION)
    _announce(SESSION, "sim_verify", "off")
    env.set_value("sim_verify", None, session_id=SESSION)

    assert env.resolve(SESSION)["sim_verify"] == ("on", "default")
    assert env.announced(SESSION)["sim_verify"] is False
    assert env.announced(SESSION) == {s.key: False for s in env.SETTINGS}


def test_announced_covers_every_setting_and_an_unindexed_session():
    """Every key, always — the same contract `resolve` is held to, for the same
    reason: a caller cannot tell a missing key from an absent feature."""
    assert env.announced("never-indexed") == {s.key: False for s in env.SETTINGS}


def _path_rules():
    """`inject-path-rules.py`, loaded by path — its filename has dashes.

    Loaded here to read its private copies, which is the point: that hook is
    stdlib-only by its own contract and may not import the host, so it keeps its
    own `_cache_root` and `_safe_dir_name`. This is the parity that stops one
    side moving alone.
    """
    loader = SourceFileLoader("jstack_path_rules_parity",
                              str(PLUGIN / "hooks/inject-path-rules.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_the_marker_path_is_the_one_the_hooks_compose():
    """One convention, checked against the copy that cannot import it.

    A drift here is invisible from either side: the hooks would write markers
    into one directory and `announced` would look in another, so every setting
    would read as never announced while the injection worked perfectly. The
    names are composed from the hook's own helpers rather than from a literal,
    because a literal would agree with itself forever.
    """
    rules = _path_rules()
    for name in ("sess-1", "", "ops/chat weird:id", "x" * 200):
        assert markers.safe_dir_name(name) == rules._safe_dir_name(name)
    assert env.cache_root() == rules._cache_root()

    sid, key, value = "3eee625d-6976", "delivery_method", "distribute"
    assert env.session_cache(sid) == rules._cache_root() / rules._safe_dir_name(sid)
    assert env.announce_marker(sid, key, value) == (
        env.session_cache(sid)
        / f"env-{rules._safe_dir_name(key)}-{rules._safe_dir_name(value)}.marker")


def test_the_plugins_session_dir_is_the_hosts_session_cache():
    """`_env.session_dir` is where all three env hooks put a marker, and
    `plan-mode-watch.py` its mode snapshot. It must be the host's answer: the
    dir the app reads back from, plus the mkdir a reader must never do."""
    loader = SourceFileLoader("jstack_env_shared_parity",
                              str(PLUGIN / "hooks/_env.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    shared = importlib.util.module_from_spec(spec)
    loader.exec_module(shared)

    sid = "sess-parity"
    assert shared.session_dir(sid) == env.session_cache(sid)
    assert env.session_cache(sid).is_dir()
    assert shared.announce_marker(env, sid, "sim_verify", "off") == (
        env.announce_marker(sid, "sim_verify", "off"))
