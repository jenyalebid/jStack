"""The roster is pinned ids, so something has to notice when they go stale.

What is worth guarding here is not that a subprocess can be faked. It is the
two directions of a lie: a probe that cannot reach its CLI must NOT read as
"nothing changed", and a roster that has fallen behind must name which row and
why. Both were the actual failure — the Claude rows sat a generation behind
for weeks while every check on this host stayed green, because nothing was
asking the question.
"""

import json
import subprocess

import pytest

import jstack_host.modelprobe as mp

ROSTER = [
    {"id": "claude", "default_model": "claude-opus-5-5",
     "models": [{"id": "claude-opus-5-5"}, {"id": "claude-sonnet-5-5"},
                {"id": "claude-fable-5-1"}, {"id": "claude-haiku-4-5-20251001"}]},
    {"id": "codex", "default_model": "gpt-6-astra",
     "models": [{"id": "gpt-6.1-sol"}, {"id": "gpt-6-astra"},
                {"id": "gpt-6-sol"}, {"id": "gpt-6-luna"}]},
]

CURRENT = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5",
           "fable": "claude-fable-5-1", "haiku": "claude-haiku-4-5-20251001"}

CATALOG = [{"slug": "gpt-6.1-sol", "priority": 1, "visibility": "list"},
           {"slug": "gpt-6-astra", "priority": 2, "visibility": "list"},
           {"slug": "gpt-6-sol", "priority": 3, "visibility": "list"},
           {"slug": "gpt-6-luna", "priority": 4, "visibility": "list"},
           {"slug": "gpt-reserve", "priority": 4, "visibility": "hide"},
           {"slug": "gpt-5.5", "priority": 13, "visibility": "list"}]


def fake_cli(claude_by_alias=CURRENT, catalog=CATALOG, claude_rc=0, codex_rc=0):
    """A runner that answers like the two real CLIs do."""
    def run(argv, capture_output=None, text=None, timeout=None, cwd=None):
        if argv[0] == "claude":
            alias = argv[argv.index("--model") + 1]
            body = json.dumps({"modelUsage": {claude_by_alias[alias]: {}}})
            return subprocess.CompletedProcess(argv, claude_rc, body, "boom")
        return subprocess.CompletedProcess(
            argv, codex_rc, json.dumps({"models": catalog}), "boom")
    return run


# ── Reading the CLIs ─────────────────────────────────────────────────────────

def test_claude_alias_resolves_to_the_model_that_answered():
    assert mp.claude_latest(runner=fake_cli()) == CURRENT


def test_codex_catalog_is_priority_ordered_and_drops_hidden_rows():
    got = [m["slug"] for m in mp.codex_catalog(runner=fake_cli())]
    assert got == ["gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.5"]
    assert "gpt-reserve" not in got


def test_a_cli_that_fails_raises_rather_than_reporting_no_drift():
    with pytest.raises(mp.ProbeError):
        mp.claude_latest(runner=fake_cli(claude_rc=1))
    with pytest.raises(mp.ProbeError):
        mp.codex_catalog(runner=fake_cli(codex_rc=1))


def test_an_unreachable_binary_raises_probe_error():
    def explode(*a, **k):
        raise FileNotFoundError("no such file: claude")
    with pytest.raises(mp.ProbeError):
        mp.claude_latest(runner=explode)


def test_a_turn_no_model_answered_is_not_an_id():
    def run(argv, **k):
        return subprocess.CompletedProcess(argv, 0, json.dumps({"modelUsage": {}}), "")
    with pytest.raises(mp.ProbeError):
        mp.claude_latest(runner=run)


def test_an_empty_catalog_is_a_failure_not_an_empty_roster():
    with pytest.raises(mp.ProbeError):
        mp.codex_catalog(runner=fake_cli(catalog=[]))


# ── The comparison ───────────────────────────────────────────────────────────

def test_a_current_roster_has_no_drift():
    assert mp.drift(CURRENT, CATALOG, ROSTER) == []


def test_a_new_claude_generation_names_the_alias_and_the_id():
    moved = dict(CURRENT, opus="claude-opus-5-6")
    lines = mp.drift(moved, CATALOG, ROSTER)
    assert any("claude-opus-5-6" in l and "opus" in l for l in lines)
    # And the default is called out separately — the picker and the fallback
    # are two edits, and shipping one of them is the likelier miss.
    assert any("default is claude-opus-5-5" in l for l in lines)


def test_a_roster_id_no_alias_points_at_is_named_stale():
    stale = [{"id": "claude", "default_model": "claude-opus-5-5",
              "models": [{"id": "claude-opus-5-5"}, {"id": "claude-fable-5"}]}]
    lines = mp.drift(CURRENT, CATALOG, stale)
    assert any("claude-fable-5" in l and "no family alias" in l for l in lines)


def test_a_new_top_codex_model_is_drift():
    catalog = [{"slug": "gpt-7", "priority": 0, "visibility": "list"}] + CATALOG
    lines = mp.drift(CURRENT, catalog, ROSTER)
    assert any("gpt-7" in l and "top 4" in l for l in lines)


def test_a_retired_codex_model_still_in_the_roster_is_drift():
    catalog = [m for m in CATALOG if m["slug"] != "gpt-6-sol"]
    lines = mp.drift(CURRENT, catalog, ROSTER)
    assert any("gpt-6-sol" in l and "no longer offers it" in l for l in lines)


def test_a_reordered_catalog_below_the_cut_is_not_drift():
    """Legacy rows shuffling priority is the vendor's business, not ours."""
    catalog = CATALOG[:4] + [{"slug": "gpt-5.5", "priority": 99, "visibility": "list"}]
    assert mp.drift(CURRENT, catalog, ROSTER) == []


# ── The command ──────────────────────────────────────────────────────────────

def test_exit_codes_separate_drift_from_an_unanswerable_probe(monkeypatch, capsys):
    monkeypatch.setattr(mp, "report", lambda: {"claude": {}, "codex": [], "drift": []})
    assert mp.main([]) == 0
    monkeypatch.setattr(mp, "report", lambda: {"claude": {}, "codex": [],
                                               "drift": ["claude: moved"]})
    assert mp.main([]) == 1

    def boom():
        raise mp.ProbeError("codex debug models: exit 1")
    monkeypatch.setattr(mp, "report", boom)
    assert mp.main([]) == 2
    assert "codex debug models" in capsys.readouterr().err


def test_the_live_roster_obeys_the_rules_the_probe_enforces():
    """`drift` is only meaningful if the shipped roster is shaped for it: one
    row per Claude family, and a Codex list the catalog's top rows can fill."""
    import jstack_host.engines as engines
    claude = [e for e in engines.ENGINES if e["id"] == "claude"][0]
    assert len(claude["models"]) == len(mp.CLAUDE_ALIASES)
    families = [m["id"].split("-")[1] for m in claude["models"]]
    assert sorted(families) == sorted(mp.CLAUDE_ALIASES)
