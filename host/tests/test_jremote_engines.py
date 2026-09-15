"""Per-agent engine + model defaults, and how a spawn resolves them.

The thing worth guarding here is not that a picker stores a string. It is that
**there is exactly one fallback point**. A spawn arrives from four places (the
app's new-chat tap, its long-press menu, the share sheet, a desk-side spawn)
and only `engines.resolve` is allowed to turn "unspecified" into a concrete
engine and model. A second fallback anywhere else means two answers to the
same question, and the one that loses is the one the user actually set.

The other half is that a NAMED-but-unknown value refuses. Falling back there
would spawn a session on a different model than the caller asked for, and
nothing downstream would ever say so — the model is passed on the command line
and, for an idle session, recorded in exactly one place (the open registry).

Every model id in the roster was verified by live probe against the real CLIs
before it was listed; `test_roster_ids_are_the_probed_set` pins that set so a
future edit adding an unprobed id has to change this file on purpose.
"""

import json

import pytest

import jstack_host.engines as engines
import jstack_host.managed as managed


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Never touch the real preferences file — these tests write."""
    monkeypatch.setattr(engines, "_STATE", tmp_path / "jremote_engines.json")


# ── The roster ───────────────────────────────────────────────────────────────

def test_roster_ids_are_the_probed_set():
    """Live-probed 2026-08-26 with `claude -p --model X` and `codex exec -m X`.
    `gpt-5.6-pro` appears in the Codex binary's own strings and is REFUSED by
    it — which is why nothing here is taken from a strings dump alone."""
    got = {e["id"]: [m["id"] for m in e["models"]] for e in engines.ENGINES}
    assert got == {
        # No `[1m]` rows: Opus 5 and Sonnet 5 carry the 1M context window
        # natively and the CLI strips the suffix, so a second row offered a
        # choice that resolved to the identical session.
        "claude": ["claude-opus-5", "claude-sonnet-5", "claude-fable-5",
                   "claude-haiku-4-5-20251001"],
        "codex": ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
                  "gpt-5.5", "gpt-5.4"],
    }


def test_fleet_default_is_claude_opus_5():
    assert engines.FLEET_ENGINE == "claude"
    assert engines._BY_ID["claude"]["default_model"] == "claude-opus-5"
    assert engines._BY_ID["codex"]["default_model"] == "gpt-5.6-sol"


def test_every_engine_default_model_is_in_its_own_list():
    for e in engines.ENGINES:
        assert e["default_model"] in [m["id"] for m in e["models"]], e["id"]


def test_roster_carries_the_fleet_default_for_a_cold_device():
    r = engines.roster()
    assert r["default_engine"] == "claude"
    assert [e["id"] for e in r["engines"]] == ["claude", "codex"]


# ── Defaults, stored and unstored ────────────────────────────────────────────

def test_unset_agent_resolves_to_the_fleet_default():
    d = engines.defaults("wren")
    assert d["engine"] == "claude"
    assert d["models"] == {"claude": "claude-opus-5", "codex": "gpt-5.6-sol"}


def test_defaults_answer_for_every_engine_not_just_the_chosen_one():
    """The long-press menu can launch either engine, so an agent defaulting to
    Claude still needs a Codex model waiting."""
    engines.set_defaults("atlas", engine="claude",
                         models={"codex": "gpt-5.6-luna"})
    d = engines.defaults("atlas")
    assert d["engine"] == "claude"
    assert d["models"]["claude"] == "claude-opus-5"   # untouched, still default
    assert d["models"]["codex"] == "gpt-5.6-luna"     # ready if picked


def test_preferences_key_on_the_agent_not_the_seat():
    """Same rule the notification mutes use — every seat of an agent shares
    the choice, so setting it from one seat's page is not a surprise on
    another."""
    engines.set_defaults("nova-chat", engine="codex")
    assert engines.defaults("nova")["engine"] == "codex"
    assert engines.defaults("nova-service-call")["engine"] == "codex"


def test_partial_write_leaves_the_other_half_alone():
    engines.set_defaults("bryn", engine="codex",
                         models={"claude": "claude-sonnet-5"})
    engines.set_defaults("bryn", models={"codex": "gpt-5.6-terra"})
    d = engines.defaults("bryn")
    assert d["engine"] == "codex"
    assert d["models"]["claude"] == "claude-sonnet-5"
    assert d["models"]["codex"] == "gpt-5.6-terra"


def test_a_choice_equal_to_the_default_is_still_written():
    """Otherwise a later edit to ENGINES silently re-decides something the user
    decided by hand."""
    engines.set_defaults("iris", engine="claude",
                         models={"claude": "claude-opus-5"})
    raw = json.loads(engines._STATE.read_text())
    assert raw["agents"]["iris"]["engine"] == "claude"
    assert raw["agents"]["iris"]["models"]["claude"] == "claude-opus-5"


def test_a_model_dropped_from_the_roster_falls_back_rather_than_spawning():
    """A stored id that no longer exists must not reach a command line. The
    agent reverts to that engine's default and comes up."""
    engines._save({"agents": {"orin": {"engine": "claude",
                                        "models": {"claude": "claude-opus-4"}}}})
    assert engines.defaults("orin")["models"]["claude"] == "claude-opus-5"


def test_a_stored_engine_that_no_longer_exists_falls_back():
    engines._save({"agents": {"orin": {"engine": "gemini"}}})
    assert engines.defaults("orin")["engine"] == "claude"


# ── Refusals ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kwargs", [
    {"engine": "gemini"},
    {"models": {"gemini": "whatever"}},
    {"models": {"claude": "gpt-5.6-sol"}},      # right id, wrong engine
    {"models": {"codex": "claude-opus-5"}},     # and the mirror of it
    {"models": {"codex": "gpt-5.6-pro"}},       # looks real, CLI refuses it
])
def test_set_defaults_refuses_anything_unspawnable(kwargs):
    with pytest.raises(ValueError):
        engines.set_defaults("wren", **kwargs)


def test_a_refused_write_stores_nothing():
    with pytest.raises(ValueError):
        engines.set_defaults("wren", engine="claude",
                             models={"claude": "nope"})
    assert not engines._STATE.exists() or \
        "wren" not in json.loads(engines._STATE.read_text())["agents"]


@pytest.mark.parametrize("engine,model", [
    ("gemini", None),
    ("claude", "gpt-5.6-sol"),
    ("codex", "claude-opus-5"),
])
def test_resolve_refuses_a_named_unknown_rather_than_falling_back(engine, model):
    with pytest.raises(ValueError):
        engines.resolve("wren", engine, model)


# ── Resolution: the one fallback point ───────────────────────────────────────

def test_resolve_with_nothing_named_is_the_agents_default():
    engines.set_defaults("finch", engine="codex",
                         models={"codex": "gpt-5.6-terra"})
    assert engines.resolve("finch") == ("codex", "gpt-5.6-terra")


def test_resolve_with_an_engine_named_uses_that_engines_model():
    """The long-press case. The user picks Codex on a Claude-default agent; the
    model must be the one chosen FOR Codex, never the Claude one and never the
    CLI's own."""
    engines.set_defaults("finch", engine="claude",
                         models={"claude": "claude-fable-5",
                                 "codex": "gpt-5.6-luna"})
    assert engines.resolve("finch", "codex") == ("codex", "gpt-5.6-luna")
    assert engines.resolve("finch") == ("claude", "claude-fable-5")


def test_resolve_honours_an_explicit_model_over_the_stored_one():
    engines.set_defaults("finch", models={"claude": "claude-sonnet-5"})
    assert engines.resolve("finch", "claude", "claude-opus-5") \
        == ("claude", "claude-opus-5")


def test_resolve_is_case_and_space_tolerant_on_the_wire():
    assert engines.resolve("finch", "  CODEX ") == ("codex", "gpt-5.6-sol")


# ── The command line it produces ─────────────────────────────────────────────

def test_model_flag_uses_each_clis_own_spelling():
    assert engines.model_flag("claude", "claude-opus-5") == "--model 'claude-opus-5'"
    assert engines.model_flag("codex", "gpt-5.6-sol") == "-m 'gpt-5.6-sol'"


def test_model_flag_quotes_ids_with_shell_metacharacters():
    """Unquoted, `claude-opus-5[1m]` is a glob pattern to the pane's shell.
    It matches no file, so zsh errors and the session never starts; bash
    passes it through and it happens to work — a difference that would show up
    as 'sometimes that model doesn't launch'. No id in the roster carries
    brackets today, but a model id is vendor text we don't control, so the
    quoting stays and stays tested."""
    flag = engines.model_flag("claude", "claude-opus-5[1m]")
    assert flag == "--model 'claude-opus-5[1m]'"


def test_no_model_means_no_flag_so_old_callers_are_unchanged():
    assert engines.model_flag("claude", "") == ""


def test_inner_command_pins_the_model_for_both_engines():
    claude = managed._inner_command("SID", resume=False, model="claude-sonnet-5")
    assert "claude --session-id SID" in claude
    assert "--model 'claude-sonnet-5'" in claude

    codex = managed._inner_command("SID", resume=False, engine="codex",
                                   model="gpt-5.6-terra")
    assert "-m 'gpt-5.6-terra'" in codex
    # The trust flags are load-bearing and must survive the addition.
    assert "--dangerously-bypass-hook-trust" in codex
    assert "-c bypass_hook_trust=true" in codex


def test_inner_command_without_a_model_is_byte_identical_to_before():
    """Every caller predating the picker — the desk-side spawn, the handoff,
    a takeover resume — must produce the exact command it always did."""
    assert managed._inner_command("SID", resume=False) == \
        "exec claude --session-id SID --permission-mode bypassPermissions"
    assert managed._inner_command("SID", resume=True) == \
        "exec claude --resume SID --permission-mode bypassPermissions"


def test_model_flag_precedes_extra_so_a_caller_can_still_override():
    """`extra` is spliced verbatim for the desk-side spawn's own args. A
    caller passing its own --model there must win, which only works if ours
    comes first."""
    cmd = managed._inner_command("SID", resume=False, extra="--model opus",
                                 model="claude-sonnet-5")
    assert cmd.index("claude-sonnet-5") < cmd.index("--model opus")
