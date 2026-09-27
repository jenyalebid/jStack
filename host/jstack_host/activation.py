"""Convergence engine for `activation` declarations — jenyalebid/jStack#134.

A shipped system and an ACTIVE one are different facts: user-scope config
(a Claude `statusLine`, a Codex MCP entry) is never written back by an
install alone. A system in `systems.json` names what it needs under
`activation`, in a CLOSED vocabulary (`KINDS` below) — never a command, so a
bad declaration can misname a kind, never run one.

`plan()` is the pure core: (desired, observed) in, actions out, nothing else
touched. Anything that looks at a live machine is an observer kept OUTSIDE
it, so convergence itself needs no machine to test against.
"""

from __future__ import annotations

#: The whole vocabulary an `activation` block may name. Extraction refuses an
#: unknown key rather than skip it — the failure mode for a typo must be loud.
KINDS = ("claude_settings", "codex_mcp", "codex_hooks", "codex_shell_env",
         "shell_path", "user_links")


def _flatten(entries: list[dict]):
    for entry in entries:
        yield entry
        yield from _flatten(entry.get("subsystems") or [])


def desired_from_systems(systems: list[dict]) -> dict[str, dict[str, dict]]:
    """`{kind: {system_id: config}}` from every declared `activation` block.

    Subsystems carry their own (push -> session-files already nests this way
    for tests), so this walks the same tree `manifest.sh` flattens. A kind
    outside the closed vocabulary raises, naming the system that misspelled
    it — reading it as "nothing declared" would hide the exact drift this
    engine exists to catch.
    """
    desired: dict[str, dict[str, dict]] = {k: {} for k in KINDS}
    for entry in _flatten(systems):
        activation = entry.get("activation")
        if not activation:
            continue
        system_id = entry.get("id") or "<unnamed>"
        for kind, config in activation.items():
            if kind not in KINDS:
                raise ValueError(
                    f"{system_id}: unknown activation kind {kind!r} — "
                    f"must be one of {', '.join(KINDS)}")
            desired[kind][system_id] = config
    return {kind: systems_ for kind, systems_ in desired.items() if systems_}


def plan(desired: dict[str, dict[str, dict]],
         observed: dict[str, dict[str, dict]]) -> list[dict]:
    """One action per declared (kind, system) not already converged.

    Pure: no I/O, no machine. `observed` is expected to carry an entry for
    every `(kind, system_id)` pair `desired` does — a caller that skipped
    observing one reads as "not converged" (`current` is `None`), never as
    silently fine. Anything `desired` does not name is never read from
    `observed` at all, which is the whole safety property: an engine that
    walked `observed` instead could act on a system nobody declared.
    """
    actions = []
    for kind, systems in desired.items():
        for system_id, config in systems.items():
            current = (observed.get(kind) or {}).get(system_id)
            if current == config:
                continue
            actions.append({"kind": kind, "system": system_id,
                            "observed": current, "desired": config})
    return actions


def observe_claude_settings(desired: dict[str, dict],
                            settings: dict) -> dict[str, dict]:
    """What an already-parsed `~/.claude/settings.json` holds, for every
    system that declares a `claude_settings` activator.

    Only `statusLine` is interpreted today — the one key `claude_settings.py`
    can converge (jStack #133). A system that declares anything else under
    `claude_settings` (`hooks`, say) is reported un-converged rather than
    silently true, since nothing here observes it yet.
    """
    from . import claude_settings as cs
    wired, _ = cs.statusline_state_of(settings)
    observed = {}
    for system_id, config in desired.items():
        if isinstance(config, dict) and config.get("statusLine"):
            observed[system_id] = {"statusLine": wired}
        else:
            observed[system_id] = {}
    return observed


#: Kind -> its observer, `(desired_for_kind, **context) -> observed_for_kind`.
#: A kind in `KINDS` with no entry here is real vocabulary with no live
#: observer yet — `check_activation` reports it as declared-but-unobservable
#: rather than silently converged.
OBSERVERS = {
    "claude_settings": lambda desired, **ctx: observe_claude_settings(
        desired, ctx["settings"]),
}


def observe(desired: dict[str, dict[str, dict]], **context) -> dict[str, dict]:
    """Run every kind in `desired` through its observer.

    A kind with no observer contributes no entries for its systems, so
    `plan()` reports every one of them as un-converged (`current is None`)
    rather than raising — a doctor check must survive a vocabulary ahead of
    its own observers.
    """
    observed: dict[str, dict] = {}
    for kind, systems in desired.items():
        fn = OBSERVERS.get(kind)
        observed[kind] = fn(systems, **context) if fn else {}
    return observed
