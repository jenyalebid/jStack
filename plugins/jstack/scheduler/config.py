"""Scheduler paths, install config, and job defaults.

The package is IDENTICAL on every machine — installs differ by config only.
Two layers, and they answer different questions:

  scheduler.json  (this module, `install()`)  — what this MACHINE is: its
      timezone, what env a spawn carries, how an agent id becomes a workspace.
      Read once at import; a change needs a daemon restart.

  schedule.json   (`load_defaults()`, registry) — what the JOBS are: the
      registry plus the defaults/categories a job's settings resolve through.
      Reloaded live on mtime change.

Data dirs resolve, highest wins: the SCHEDULER_*_DIR override (the e2e sandbox
points a real daemon at tmp dirs with these), then `SCHEDULER_HOME`, then the
`JSTACK_ROOT` derivation (root.py), then `~/.scheduler` — never the package
tree; see `_guarded_path`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

try:
    import root
except ImportError:
    # An older install can carry this package without root.py alongside it —
    # a supported state, not an error. Everything below degrades to the legacy
    # chain (SCHEDULER_* env, else ~/.scheduler) exactly as it stood before
    # the root derivation existed.
    root = None


def _shipping_tree_roots() -> "tuple[Path, ...]":
    """The trees no scheduler data may resolve into: the checkout shipping this file.

    This package is distributed inside a PUBLIC git repository. A config/,
    state/ or Credentials/ dir rooted in that checkout puts `feed-token`'s
    live secret one `git add -A` from being published — so resolving there is
    an error, never a fallback. Forbidden: the package's parent tree always
    (a plugin-cache install has no .git, but is still wiped on update), plus
    the nearest enclosing git checkout. The walk stops before $HOME so a user
    whose home directory is itself a repo (dotfiles) keeps ~ usable.
    """
    pkg = Path(__file__).resolve().parent
    roots = [pkg.parent]
    home = Path.home()
    for candidate in pkg.parents:
        if candidate == home or candidate == candidate.parent:
            break
        if (candidate / ".git").exists():
            if candidate not in roots:
                roots.append(candidate)
            break
    return tuple(roots)


_FORBIDDEN_ROOTS = _shipping_tree_roots()


def _guarded_path(env_var: str, default: Path) -> Path:
    """Env-or-default, refusing any path inside the shipping checkout.

    Failing loudly beats silently choosing the repo: a secret written to a
    wrong-but-safe place is recoverable; one written into a public working
    tree is not. An env value pointing into the checkout fails the same way —
    the repo is not a valid home even on purpose.
    """
    path = Path(os.environ.get(env_var, str(default)))
    probe = Path(os.path.expanduser(str(path))).resolve()
    for root in _FORBIDDEN_ROOTS:
        if probe == root or root in probe.parents:
            hint = ("SCHEDULER_HOME" if env_var == "SCHEDULER_HOME"
                    else f"SCHEDULER_HOME (or the {env_var} override)")
            raise RuntimeError(
                f"{env_var}={path} resolves inside the checkout that ships this "
                f"package ({root}) — a public git tree. Scheduler config, state "
                f"and credentials must live outside it: set {hint} to a "
                f"directory outside the repository, e.g. ~/.scheduler."
            )
    return path


def _derived_default(legacy: Path, derive) -> Path:
    """The default a SCHEDULER_*_DIR falls back to: the JSTACK_ROOT derivation
    when the operator has declared one, else the legacy shape.

    Two gates, both deliberate. A present SCHEDULER_HOME keeps the legacy shape
    whatever JSTACK_ROOT says — the live daemon roots its dirs there, and its
    state must never move because a shell exported a new variable. And the
    derivation is consulted only when $JSTACK_ROOT is actually set: root()
    falls back to $HOME, so consulting it unconditionally would silently move
    an unconfigured install from ~/.scheduler to ~/Config — a breaking change
    wearing a derivation's clothes.
    """
    if root is None or "SCHEDULER_HOME" in os.environ or not os.environ.get("JSTACK_ROOT"):
        return legacy
    return derive()


HOME = _guarded_path("SCHEDULER_HOME", Path.home() / ".scheduler")

CONFIG_DIR = _guarded_path("SCHEDULER_CONFIG_DIR", _derived_default(
    HOME / "config", lambda: root.config_dir()))
# The derived state keeps its `scheduler` leaf: State/ under the root is
# shared with the other subsystems, and the scheduler does not own the dir.
STATE_DIR = _guarded_path("SCHEDULER_STATE_DIR", _derived_default(
    HOME / "state" / "scheduler", lambda: root.state_dir() / "scheduler"))
CREDENTIALS_DIR = _guarded_path("SCHEDULER_CREDENTIALS_DIR", _derived_default(
    HOME / "Credentials", lambda: root.credentials_dir()))

SCHEDULE_FILE = CONFIG_DIR / "schedule.json"
INSTALL_FILE = Path(os.environ.get("SCHEDULER_INSTALL_FILE", str(CONFIG_DIR / "scheduler.json")))
STATE_FILE = STATE_DIR / "state.json"
RUNS_DIR = STATE_DIR / "runs"
LOGS_DIR = STATE_DIR / "logs"
LOCK_FILE = STATE_DIR / ".registry.lock"
FEED_TOKEN_FILE = CREDENTIALS_DIR / "scheduler-feed-token"

API_PORT = int(os.environ.get("SCHEDULER_API_PORT", "9091"))
TICK_SECONDS = float(os.environ.get("SCHEDULER_TICK_SECONDS", "20"))

LOG_RETENTION_DAYS = 14
# An occurrence later than this behind now is a miss (catch-up semantics);
# within it is a normal same-tick fire.
MISFIRE_THRESHOLD_SECONDS = 120


def _local_tz_name() -> str:
    """The machine's IANA zone name.

    A scheduler whose timezone is wrong fires everything at the wrong hour, so
    guess only from sources that carry a real zone NAME: $TZ, then the
    /etc/localtime symlink (…/zoneinfo/America/Los_Angeles on macOS and Linux
    alike). Never fall back to a fixed offset — UTC is the honest last resort
    and an install that cares states `timezone` outright.
    """
    tz = os.environ.get("TZ")
    if tz:
        return tz
    try:
        target = os.readlink("/etc/localtime")
        marker = "zoneinfo/"
        if marker in target:
            return target.split(marker, 1)[1]
    except OSError:
        pass
    return "UTC"


# Per-machine seams. Every value here is something an install legitimately
# differs on; nothing here is a job-level setting (those live in schedule.json).
BUILTIN_INSTALL = {
    # Zone for wall-clock job times, the ics feed, and reading the reset clock
    # out of a usage-limit message.
    "timezone": None,  # None → _local_tz_name()
    # Name subscribers see for the ics calendar.
    "calendar_name": "Schedule",
    # Env every spawned run carries, on top of the daemon's own environment.
    "spawn_env": {},
    # Dirs prepended to the child PATH (agent-facing tools live here).
    "spawn_path_prepend": [],
    # Canonical binary search path for daemon-spawned children. launchd hands a
    # daemon a stripped PATH, so a spawn cannot inherit its way to `claude`.
    "spawn_dirs": [
        "~/.local/bin",  # claude native installer
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ],
    # "module:function" called with the job dict to produce a workspace path.
    # Absent → the built-in agent_root / agent_registry resolution.
    "workspace_resolver": None,
    # "module:function" called when a job that opted in (notify_on_failure)
    # ends in a TERMINAL non-ok finish — one nothing in-band will recover.
    # Receives a dict (job_id, agent_id, status, error, consecutive_errors,
    # session_id) and delivers it however this machine reaches a human. jStack
    # ships only the seam: absent → a failure is journaled but pushed nowhere,
    # which is exactly the silence #17 is about. A notifier that raises is
    # logged and swallowed, never allowed to fault the daemon.
    "failure_notifier": None,
    # Extra sys.path entries so a workspace_resolver's or failure_notifier's
    # module is importable.
    "python_path": [],
    # Fallback workspace resolution: the agent's directory under the install's
    # agents dir (root.agents_dir), plus an optional registry mapping agent
    # id → workspace. None means "derive from the root at the point of use";
    # a path stated in scheduler.json pins it. Not resolved here: a constant
    # captured at import answers with the environment of a process start
    # nobody remembers — five tests once errored at setup exactly that way.
    "agent_root": None,
    "agent_registry": None,
    # Sub-seat redirection: an agent id ending in `suffix` runs in the named
    # subdir when that subdir has its own AGENTS.md.
    "seat_rules": [],
}

BUILTIN_DEFAULTS = {
    "model": "opus",
    "timeout_seconds": 1800,
    "stall_timeout_seconds": 900,
    "ttft_timeout_seconds": 180,
    "max_concurrent_runs": 6,
    "catch_up_grace_seconds": 43200,
    "claude_bin": "claude",
    # An unattended run has nobody to answer a permission prompt, so bypass is
    # what makes it autonomous at all. It resolves through the same
    # job → category → default chain as any other setting, so a single job or a
    # whole category can be narrowed without touching the daemon's default.
    "permission_mode": "bypassPermissions",
    # A terminal non-ok finish calls the install's failure_notifier. Off by
    # default — an install with no notifier, or a job whose failure nobody
    # needs pushed, stays silent. Resolves job → category → default.
    "notify_on_failure": False,
}

_install: "dict|None" = None


def install() -> dict:
    """The machine's scheduler.json merged over built-ins. Cached: these are
    daemon-lifetime facts, and re-reading per spawn would let a half-written
    file change a run's environment mid-flight."""
    global _install
    if _install is None:
        merged = dict(BUILTIN_INSTALL)
        try:
            raw = json.loads(INSTALL_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            raw = {}
        for k, v in raw.items():
            if v is not None:
                merged[k] = v
        if not merged.get("timezone"):
            merged["timezone"] = _local_tz_name()
        _install = merged
    return _install


def reset_install_cache() -> None:
    """Drop the cached install config (tests point the daemon at tmp dirs)."""
    global _install
    _install = None


def default_tz() -> str:
    return install()["timezone"]


# Back-compat alias: modules and tests that read a module-level constant. This
# is evaluated at import, so SCHEDULER_INSTALL_FILE must be set before import
# (the daemon's launch env does; tests use default_tz()).
DEFAULT_TZ = default_tz()


def expand(path: str) -> Path:
    return Path(os.path.expanduser(str(path)))


def load_defaults() -> dict:
    """Registry `defaults` merged over built-ins (registry may omit keys)."""
    merged = dict(BUILTIN_DEFAULTS)
    try:
        raw = json.loads(SCHEDULE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return merged
    for k, v in (raw.get("defaults") or {}).items():
        if v is not None:
            merged[k] = v
    return merged
