"""The convergence engine itself — issue 134.

`plan()` is pure and `desired_from_systems()` only ever reads what a system
declares, so both are tested here without a doctor, a fixture machine, or any
file on disk beyond what each test builds by hand.
"""

import pytest

from jstack_host import activation


def test_extracts_activation_from_a_flat_system():
    systems = [{"id": "a", "activation": {"claude_settings": {"statusLine": True}}},
              {"id": "b"}]
    assert activation.desired_from_systems(systems) == {
        "claude_settings": {"a": {"statusLine": True}}}


def test_extracts_activation_from_a_nested_subsystem():
    systems = [{"id": "parent", "subsystems": [
        {"id": "child", "activation": {"shell_path": {"dir": "~/.local/bin"}}}]}]
    assert activation.desired_from_systems(systems) == {
        "shell_path": {"child": {"dir": "~/.local/bin"}}}


def test_a_system_with_no_activation_contributes_nothing():
    assert activation.desired_from_systems([{"id": "a"}]) == {}


def test_an_unknown_kind_raises_naming_the_system_and_the_kind():
    systems = [{"id": "bad", "activation": {"not_a_real_kind": {}}}]
    with pytest.raises(ValueError, match="bad.*not_a_real_kind"):
        activation.desired_from_systems(systems)


def test_plan_is_empty_when_observed_matches_desired():
    desired = {"claude_settings": {"a": {"statusLine": True}}}
    observed = {"claude_settings": {"a": {"statusLine": True}}}
    assert activation.plan(desired, observed) == []


def test_plan_reports_a_mismatch_as_one_action():
    desired = {"claude_settings": {"a": {"statusLine": True}}}
    observed = {"claude_settings": {"a": {"statusLine": False}}}
    actions = activation.plan(desired, observed)
    assert actions == [{"kind": "claude_settings", "system": "a",
                        "observed": {"statusLine": False},
                        "desired": {"statusLine": True}}]


def test_plan_reports_none_observed_as_a_mismatch_not_a_crash():
    desired = {"claude_settings": {"a": {"statusLine": True}}}
    actions = activation.plan(desired, {})
    assert len(actions) == 1 and actions[0]["observed"] is None


def test_plan_never_inspects_a_system_desired_does_not_name():
    # observed carries an extra system under a declared kind; plan must not
    # act on it, because desired is the only thing that licenses an action.
    desired = {"claude_settings": {"a": {"statusLine": True}}}
    observed = {"claude_settings": {"a": {"statusLine": True}, "z": {"anything": 1}}}
    assert activation.plan(desired, observed) == []


def test_plan_never_inspects_a_kind_desired_does_not_name():
    desired = {"claude_settings": {"a": {"statusLine": True}}}
    observed = {"claude_settings": {"a": {"statusLine": True}},
                "codex_mcp": {"z": {"anything": 1}}}
    assert activation.plan(desired, observed) == []


def test_observe_claude_settings_true_when_sampler_is_wired(monkeypatch):
    from jstack_host import claude_settings as cs
    desired = {"a": {"statusLine": True}}
    settings = {"statusLine": {"type": "command", "command": cs.SAMPLER}}
    assert activation.observe_claude_settings(desired, settings) == {
        "a": {"statusLine": True}}


def test_observe_claude_settings_false_when_absent():
    desired = {"a": {"statusLine": True}}
    assert activation.observe_claude_settings(desired, {}) == {
        "a": {"statusLine": False}}


def test_observe_claude_settings_ignores_a_system_declaring_something_else():
    # Nothing observes a claude_settings config that is not `statusLine` yet
    # (jStack#134's `hooks` half) — reported as un-converged, never silently ok.
    desired = {"a": {"hooks": ["something"]}}
    assert activation.observe_claude_settings(desired, {}) == {"a": {}}


def test_observe_dispatches_per_kind_and_defaults_unknown_observers_to_empty():
    desired = {"claude_settings": {"a": {"statusLine": True}},
               "codex_mcp": {"b": {"name": "job_monitor"}}}
    observed = activation.observe(desired, settings={})
    assert observed == {"claude_settings": {"a": {"statusLine": False}},
                        "codex_mcp": {}}
    # A kind with no observer reads as un-converged, not converged by accident.
    assert activation.plan(desired, observed) == [
        {"kind": "claude_settings", "system": "a",
         "observed": {"statusLine": False}, "desired": {"statusLine": True}},
        {"kind": "codex_mcp", "system": "b",
         "observed": None, "desired": {"name": "job_monitor"}},
    ]
