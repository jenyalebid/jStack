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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import attention  # noqa: E402 — sibling hook, path set above

#: One switch for all three moments. Whatever a person mutes, they mute
#: because the injection is in their way, and a floor without its corrections
#: is a stale floor — worse than silence.
KILL_SWITCH = "JSTACK_ENV_INJECT_DISABLED"

DEFAULT_CACHE_ROOT = Path("/tmp/jstack-rule-cache")

_rules = None


def disabled() -> bool:
    return bool(os.environ.get(KILL_SWITCH))


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
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "host"))
    from jstack_host import environment
    return environment


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
    """
    root = os.environ.get("JSTACK_CACHE_ROOT")
    base = Path(root).expanduser() if root else DEFAULT_CACHE_ROOT
    path = base / path_rules()._safe_dir_name(session_id or "_unknown")
    path.mkdir(parents=True, exist_ok=True)
    return path


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
