"""How a run reaches its binary, its environment, and its workspace.

All three are per-machine facts, so all three come from scheduler.json rather
than from edits to this file. A daemon under launchd inherits a stripped PATH
and no login environment, so nothing here may assume the daemon's own env is
usable as a child's.
"""

from __future__ import annotations

import importlib
import json
import shutil
import sys
from pathlib import Path

from . import config

try:
    import root
except ImportError:
    # An older install can carry this package without root.py alongside it —
    # a supported state, not an error. The workspace fallback degrades to the
    # legacy blind join this module shipped with.
    root = None


def _bootstrap_python_path() -> None:
    """Put the install's `python_path` on sys.path so a workspace_resolver
    living outside this package is importable.

    Appended, never inserted at the front. The install's `python_path` is an
    absolute machine path (the host install's own tree), so inserting it at 0
    outranked whatever the caller had already put there — and a process running
    out of a git worktree got the home checkout's `lib`/`dashboard` the moment
    anything imported this package. Making a module reachable is the whole job
    here; outranking the caller's own tree never was.
    """
    for entry in config.install().get("python_path") or []:
        p = str(config.expand(entry))
        if p not in sys.path:
            sys.path.append(p)


def spawn_dirs() -> list:
    return [str(config.expand(d)) for d in config.install().get("spawn_dirs") or []]


def spawn_path(*prepend: str, inherit: "str|None" = None) -> str:
    """PATH string for a daemon subprocess. `prepend` dirs win over the
    install's canonical list; `inherit` (an existing PATH) is appended last."""
    parts = [*prepend, *spawn_dirs()]
    if inherit:
        parts.append(inherit)
    return ":".join(p for p in parts if p)


def run_path(inherit: "str|None" = None) -> str:
    """The PATH a scheduled run gets: the install's tool dirs, then the
    canonical binary dirs, then whatever the daemon itself inherited."""
    prepend = [str(config.expand(d))
               for d in config.install().get("spawn_path_prepend") or []]
    return spawn_path(*prepend, inherit=inherit)


def claude_resolvable(path: "str|None" = None) -> "str|None":
    """Absolute path of the claude binary under the given PATH (default: the
    canonical spawn path), or None — the honest 'can we even spawn' probe."""
    return shutil.which("claude", path=path or spawn_path())


def run_env(base: dict) -> dict:
    """`base` (the daemon's environment) plus the install's spawn_env and a
    PATH a child can actually resolve binaries against."""
    env = dict(base)
    for k, v in (config.install().get("spawn_env") or {}).items():
        env[str(k)] = str(v)
    env["PATH"] = run_path(inherit=base.get("PATH", ""))
    return env


# --------------------------------------------------------------- workspaces


def _load_hook(spec: str):
    """"module:function" → the callable. Import errors are fatal on purpose:
    a resolver that silently fell back to the generic path would run every job
    in the wrong workspace, which reads as the agent losing its mind rather
    than as a config error."""
    mod_name, _, fn_name = spec.partition(":")
    if not mod_name or not fn_name:
        raise ValueError(f"workspace_resolver must be 'module:function', got {spec!r}")
    _bootstrap_python_path()
    return getattr(importlib.import_module(mod_name), fn_name)


def _from_registry(agent_id: str) -> "Path|None":
    """Look the agent up in the install's agent registry ({agent: {workspace}})."""
    reg_path = config.install().get("agent_registry")
    if not reg_path:
        return None
    try:
        raw = json.loads(config.expand(reg_path).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    entry = raw.get(agent_id)
    if isinstance(entry, dict) and entry.get("workspace"):
        return config.expand(entry["workspace"])
    return None


def _apply_seat_rules(agent_id: str, workspace: Path) -> Path:
    """An agent id ending in a rule's suffix runs in that rule's subdir — but
    only when the subdir is a real seat (has its own AGENTS.md). Without that
    check a matching id would cd into a directory with no context and the run
    would start blind."""
    for rule in config.install().get("seat_rules") or []:
        suffix, seat = rule.get("agent_id_suffix"), rule.get("seat")
        if not suffix or not seat or not agent_id.endswith(suffix):
            continue
        candidate = workspace / seat
        if any((candidate / _n).exists() for _n in ("AGENTS.md", "CLAUDE.md")):
            return candidate
    return workspace


def _from_agents_dir(agent_id: str) -> Path:
    """The built-in fallback: a real directory under agents_dir().

    Two bars, and they are not the same bar. `root.resolve_agent` answers
    "which agent is this", so it requires a AGENTS.md — but a spawn only needs
    somewhere real to run, and a directory that exists is a legitimate
    workspace before anything has declared it a seat. So: resolve the agent
    when there is one (that is what buys the case and -/_ tolerance), else
    accept the literal join when it names a directory that exists.

    What does NOT survive is the join to a path that is not there. It used to
    be returned anyway, and the run then died inside the spawn with an errno
    against a path nobody recognised, an hour after the typo that caused it.
    Raising here names the id and the ids that ARE there, at the point of the
    mistake."""
    inst = config.install()
    if root is None:
        # Older install without root.py: the legacy join, unchanged.
        return config.expand(inst.get("agent_root") or "~/Agents") / agent_id
    resolved = root.resolve_agent(agent_id, inst)
    if resolved is not None:
        return resolved
    literal = root.agents_dir(inst) / agent_id
    if literal.is_dir():
        return literal
    known = root.agents(inst)
    raise ValueError(
        f"agent {agent_id!r} has no workspace under {root.agents_dir(inst)} — "
        f"known agents: {', '.join(known) if known else '(none)'}. Fix the "
        f"job's agent_id, or set `workspace` on the job to pin one outright."
    )


def resolve_workspace(job: dict) -> Path:
    """Where this job's run is spawned.

    Job `workspace` override wins outright — a resume wake pins it to the
    calling session's cwd so the forked transcript is found under the same
    project slug. Otherwise the install decides: its resolver hook if it has
    one, else its agent registry, else the agent's directory under the
    install's agents dir.
    """
    ws = job.get("workspace")
    if ws:
        return Path(ws)

    agent_id = job.get("agent_id", "")
    spec = config.install().get("workspace_resolver")
    if spec:
        return _apply_seat_rules(agent_id, Path(_load_hook(spec)(agent_id)))

    resolved = _from_registry(agent_id)
    if resolved is None:
        resolved = _from_agents_dir(agent_id)
    return _apply_seat_rules(agent_id, resolved)
