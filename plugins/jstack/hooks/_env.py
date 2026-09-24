"""What the three session-environment hooks share.

Entry, delta and announce are three moments of ONE mechanism — the floor, the
correction, the reinforcement — so the parts they have in common are exactly
the parts that must not drift: which store the settings come from, where a
session's markers live, and how a path glob is decided. A second copy of any
of those is a second answer to the same question, and the copy is the one that
goes stale.

These hooks import the host, which the stdlib-only hooks may not. That is
affordable here and not there: `attention.py` runs beside every tool call of
every session, while these are scoped to a prompt or a matcher, and the
measured cost of `from jstack_host import environment` is ~30ms over bare
Python. The alternative is a second copy of the setting registry living in the
plugin, which is the drift above with higher stakes.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import attention  # noqa: E402 — sibling hook, path set above

#: One switch for all three moments. Whatever a person mutes, they mute
#: because the injection is in their way, and a floor without its corrections
#: is a stale floor — worse than silence.
KILL_SWITCH = "JSTACK_ENV_INJECT_DISABLED"

_rules = None


def disabled() -> bool:
    return bool(os.environ.get(KILL_SWITCH))


def _host_importable() -> None:
    """Put the host package where an import can find it, checkout or install.

    Only the checkout case needs the insert; where the host is installed the
    import each caller then makes resolves from site-packages and this line is
    inert. Both loaders below go through it so neither can be the one that
    forgot.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "host"))


def host_environment():
    """`jstack_host.environment`, against the store this machine's Hub serves.

    A Hub mounted into another server declares its state directory in the
    embed marker precisely so that a process which knows nothing can find it,
    and a hook is such a process — the plugin ships to machines whose Hub keeps
    state where this checkout cannot see it. Read through `attention.py`
    because that is the file which owns where the marker lives.
    """
    if not os.environ.get("JREMOTE_STATE_DIR"):
        declared = attention.state_dir()
        if declared:
            os.environ["JREMOTE_STATE_DIR"] = declared
    _host_importable()
    from jstack_host import environment
    return environment


def host_markers():
    """`jstack_host.markers` — the host's marker convention, and only that.

    Not reached through `host_environment()`, which is the expensive door: that
    one resolves the state directory and imports the store, ~30ms where this
    costs nothing measurable, and `plan-mode-watch.py` asks for a marker path on
    every prompt and every Stop of every session before it knows whether it
    wants the host at all. Nothing here touches a store — a marker path is an
    env var and a filename — so nothing here needs the state directory either.
    """
    _host_importable()
    from jstack_host import markers
    return markers


def path_rules():
    """`inject-path-rules.py`, imported by path — its filename has dashes.

    Its glob matcher and its marker helpers are the machinery this mechanism
    shares with the path rules, deliberately: both answer "has this session
    been told this already", and one answer drifting from the other shows up
    as an injection that fires forever or never. Imported lazily so the hooks
    that need neither never pay for it.
    """
    global _rules
    if _rules is None:
        import importlib.util  # noqa: PLC0415 — only on the path that needs it
        path = Path(__file__).resolve().parent / "inject-path-rules.py"
        spec = importlib.util.spec_from_file_location("_jstack_path_rules", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _rules = module
    return _rules


def review_config() -> dict:
    """The host's review config, read where session-start-inject.py reads it.

    Only `agent_root` is wanted, and only so the seat this resolves is the seat
    the timeline injector resolves from the same cwd: two SessionStart hooks
    naming different agents for one directory would put an agent's settings and
    an agent's history in the same block under different owners.
    """
    path = Path(os.environ.get(
        "JSTACK_REVIEW_CONFIG",
        str(Path.home() / ".claude" / "jstack" / "review.json"))).expanduser()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def seat_agent(cwd: str) -> str:
    """The agent id owning `cwd`, in the casing the store's `agent_id` holds.

    THE AGENT LAYER IS UNREACHABLE WITHOUT THIS. `resolve` discovers the agent
    by looking the session up in `sessions`, and the indexer builds that row
    from a transcript — which at SessionStart has no lines written yet. So a
    cold start resolves no agent, every setting falls back to its default, and
    `state_line` is empty precisely for the settings entry exists to carry: an
    agent-level value is the only kind that CAN be in force before a session
    runs, since nobody can set a session value on a session that does not
    exist. Passing it explicitly is what makes entry a floor instead of a
    resume feature.

    `root.py` answers it because that file is stdlib-only by its own contract,
    imports nothing from the package, and holds the single definition of what
    an agent is — the one `session-start-inject.py` already defers to.
    """
    if not cwd:
        return ""
    import root  # noqa: PLC0415 — sibling, and only this path needs it
    import repo_seat  # noqa: PLC0415
    cfg = review_config()
    base = root.agents_dir(cfg)
    agent, _ = root.seat_of(Path(cwd), cfg, base)
    if not agent:
        # An IDE fixes cwd to the checkout, which is nowhere under the agents
        # dir; the registry is the only thing that knows who owns a repo.
        agent, _ = repo_seat.seat_for(
            Path(cwd), base, repo_seat.registry_path_for(cfg, base))
    return (agent or "").lower()


def moved(env, session_id: str) -> dict[str, str]:
    """`{key: value}` for the settings this session holds off their default.

    Non-defaults only, which is both what a snapshot needs to be comparable
    (`delta_lines` reads a missing key as that key's default) and the whole
    safety property of the module: a value at its default has nothing to say.
    """
    resolved = env.resolve(session_id)
    return {s.key: resolved[s.key][0] for s in env.SETTINGS
            if s.key in resolved and resolved[s.key][0] != s.default}


def session_dir(session_id: str) -> Path:
    """The per-session marker dir, the same one the path rules mark in.

    One dir per session rather than one per mechanism, so that whatever
    reclaims it reclaims all of it — a second root would be a second thing to
    remember to clean, found years later full of a machine's session history.

    The path is the host's answer, not one composed here: the app reads these
    files back through `environment.announced`, so the spelling has a reader
    outside this plugin and a second spelling of it would be a screen that
    reports nothing while the hooks announce normally. What this adds is the
    mkdir, which the readers must not do.
    """
    path = host_markers().session_cache(session_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def announce_marker(env, session_id: str, key: str, value: str) -> Path:
    """One setting's marker, in a directory the write can land in.

    `env` is passed rather than fetched because every caller is already holding
    it — the hook that writes a marker has just resolved what is in force out of
    the same module.
    """
    session_dir(session_id)
    return env.announce_marker(session_id, key, value)


def reinject_bytes() -> int:
    """Transcript growth after which a line is worth saying again.

    The path rules' threshold and env var, not one of our own: both are
    answers to the same decay, and a session tuned to hear its rules more often
    means the same about its environment.
    """
    rules = path_rules()
    try:
        return int(os.environ.get("JSTACK_RULE_REINJECT_BYTES",
                                  rules.DEFAULT_REINJECT_BYTES))
    except ValueError:
        return rules.DEFAULT_REINJECT_BYTES


def emit(event: str, text: str) -> None:
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": event, "additionalContext": text}}))
    sys.stdout.flush()
