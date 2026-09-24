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
"""

import pytest

from jstack_host import environment as env
from jstack_host import store
from jstack_host.store import SessionStore

SESSION = "sess-1"
AGENT = "ops-chat"


@pytest.fixture(autouse=True)
def env_store(tmp_path, monkeypatch):
    """A store of this test's own, through the package's own injection point.

    `store._store` is what `get_store` consults first, which conftest's
    isolation also defers to — so this wins without fighting it, and the
    module under test resolves its store the same way in the test as on the
    machine.
    """
    monkeypatch.setattr(store, "_store", SessionStore(db_path=tmp_path / "env.sqlite"))


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
