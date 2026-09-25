"""What this host knows about agents and workspaces — the one machine seam.

Everything else in this package is generic: it reads transcripts, scans
processes, drives tmux. What it cannot answer on its own is *whose* session it
is looking at — which agents exist on this machine, where each one's workspace
lives, which agent owns a given Claude project dir — *how this machine runs an
engine*: the binary search path a spawned session gets, and the model it
defaults to — and *what this machine calls things*: the labels its user has
hung on individual sessions. Those answers differ on every instance.

So they come through here, and only here. Scattered machine-specific imports
across the package would make the host API silently single-machine: a second
Mac could serve every endpoint except the ones that name an agent, and nothing
would say which those were. One module makes the coupling enumerable — and
makes any particular machine a profile, not a fork.

Two sources:

- **external** — a module named `jremote_host_profile`, importable at the
  host's import root (`JREMOTE_PROFILE_MODULE` overrides the name), supplies
  the profile via `make_profile()`. This is how an instance with its own agent
  plumbing — a registry, a spawner, an alert channel — teaches the host its
  dialect without forking the package. The package never ships one; the
  machine does, and the profile does not take the machine's modules, it takes
  their *answers*.
- **default** — no such module: agents are the directories under the instance
  root, a sub-mode is a directory inside one, and a project dir is decoded by
  reversing Claude Code's own path encoding against that root. A host with
  nothing configured still names its sessions.

`JREMOTE_HOST_PROFILE` (`external` | `default` | `auto`, default `auto`)
forces the choice; `JREMOTE_INSTANCE_ROOT` names the default profile's root.
Resolution is lazy and cached — importing this module must never require the
external module, or the standalone host would fail at import for the sake of a
profile it does not use.

**The default profile is not a stub.** A profile that answered "no agents" for
an unconfigured host would make every board row anonymous and every workspace
lookup raise, which reads as a broken host rather than an unconfigured one. It
derives real answers from the filesystem, and says so.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import socket
import sys
import uuid
from pathlib import Path

HOME = Path.home()

# The sealed Hub's tmux carries Homebrew's static ncurses, which looks for
# terminfo in a prefix a clean Mac doesn't have; a launchd-spawned host has no
# TERMINFO_DIRS either, so every tmux client it runs prints "can't find
# terminfo database" straight into the session. Anchor the whole host process
# — and every child it spawns — to the system database, here in the module
# every host entrypoint imports.
os.environ.setdefault("TERMINFO_DIRS",
                      "/usr/share/terminfo:/opt/homebrew/share/terminfo")

# Directories that live inside an agent's root but are never a sub-mode. Same
# list as lib.agents' — a seat's pad and content buckets are not spawnable,
# and `git` is the seat's save folder. This set is mode discovery only here,
# so `git` sits in it; lib.agents excludes it at the loop instead, because
# there the same set also prunes subtrees from agent_files().
_NON_MODE_DIRS = {"missions", "memory", "active", ".claude", ".handoff",
                  "scratch", "concepts", "pad", "git"}

# Where a spawned engine looks for binaries when no host says otherwise. An
# external profile may front-load machinery of its own (shims, wrappers);
# this is the portable remainder.
_DEFAULT_SPAWN_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin",
                       "/usr/bin", "/bin", "/usr/sbin", "/sbin")


# --------------------------------------------------------------------------
# Profiles
# --------------------------------------------------------------------------

class DefaultProfile:
    """The filesystem is the roster.

    Every direct child directory of the instance root is an agent; every
    directory inside one is a sub-mode. That is the same shape `lib.agents`
    describes, minus the registry — which is exactly what a machine that has
    never had a registry can still observe about itself.
    """

    name = "default"

    def __init__(self, root: Path):
        self.root = root
        self._reg_cache: tuple[tuple[int, int], dict] | None = None

    # -- the registry: jStack's agents.json, beside the roster ---------------

    def registry_path(self) -> Path:
        """Where jStack keeps the agent registry: `{agent_root}/agents.json`,
        `JSTACK_AGENT_REGISTRY` overriding — the same two answers jStack's own
        tools (`repo-seat`, `day-audit`) resolve, so one file describes the
        fleet to both."""
        env = os.environ.get("JSTACK_AGENT_REGISTRY", "").strip()
        return Path(env).expanduser() if env else self.root / "agents.json"

    def _registry(self) -> dict[str, dict]:
        """The registry, keyed by lowercase agent id — `{}` on a host without
        one. Cached by (mtime, size) so a board poll costs one stat.

        The filesystem stays the roster; the registry says how to draw it —
        the name, the emoji, the role — and where a bare id opens, which is
        the seat the entry's `workspace` names. A directory with no entry is
        still an agent (a fresh host has a tree before it has a registry) and
        an entry with no directory is not one (the registry can describe a
        machine this is not). `"active": false` hides the card, the same
        reading `lib.agents` gives it at home.
        """
        path = self.registry_path()
        try:
            st = path.stat()
        except OSError:
            self._reg_cache = None
            return {}
        key = (st.st_mtime_ns, st.st_size)
        if self._reg_cache and self._reg_cache[0] == key:
            return self._reg_cache[1]
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            raw = {}
        reg = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(v, dict) and not str(k).startswith("_"):
                    reg[str(k).lower()] = v
        self._reg_cache = (key, reg)
        return reg

    # -- roster --------------------------------------------------------

    def _agent_dirs(self) -> dict[str, Path]:
        out: dict[str, Path] = {}
        try:
            children = sorted(self.root.iterdir())
        except OSError:
            return out
        for child in children:
            if not child.is_dir() or child.name.startswith("."):
                continue
            if child.name in _NON_MODE_DIRS:
                continue
            out[child.name.lower()] = child
        return out

    def active_agents(self):
        reg = self._registry()
        out = {}
        for base, path in self._agent_dirs().items():
            entry = reg.get(base) or {}
            if entry.get("active") is False:
                continue
            out[base] = {
                "name": str(entry.get("name") or path.name),
                "description": str(entry.get("description") or ""),
                "emoji": str(entry.get("emoji") or ""),
                "roles": [str(r) for r in (entry.get("roles") or [])],
                "repos": [str(r) for r in (entry.get("repos") or [])],
                "active": True,
                "workspace": str(path),
            }
        return out

    # -- ids and workspaces --------------------------------------------

    def split_id(self, agent_id: str):
        if "-" not in agent_id:
            return agent_id, None
        dirs = self._agent_dirs()
        parts = agent_id.split("-")
        for i in range(1, len(parts)):
            base = "-".join(parts[:i])
            mode = "-".join(parts[i:])
            root = dirs.get(base)
            if root is None:
                continue
            if (root / mode).is_dir():
                return base, mode
            nested = _resolve_nested(root, mode)
            if nested:
                return base, nested
        return agent_id, None

    def workspace(self, agent_id: str) -> Path:
        base, mode = self.split_id(agent_id)
        root = self._agent_dirs().get(base)
        if root is None:
            raise KeyError(f"agent {agent_id!r}: base {base!r} not under {self.root}")
        if mode:
            sub = root / mode
            # An unmade sub-mode dir under a real agent root is still that
            # agent's seat — the same call lib.agents answers for an umbrella.
            return sub
        # A bare id opens where the registry says the agent works. jStack's
        # entries name the seat (`…/Ops/chat`), and a session started at the
        # umbrella root instead would sit outside every rule glob and timeline
        # seat the fleet has — the one place nobody meant a chat to land.
        seat = str((self._registry().get(base) or {}).get("workspace") or "")
        if seat:
            seat_path = Path(seat.replace("~", str(HOME), 1)
                             if seat.startswith("~") else seat)
            if seat_path.is_dir():
                return seat_path
        return root

    def submode_dirs(self, base: str):
        root = self._agent_dirs().get(base)
        if root is None:
            return []
        out = []
        try:
            children = sorted(root.iterdir())
        except OSError:
            return out
        for child in children:
            if not child.is_dir() or child.name.startswith("."):
                continue
            if child.name in _NON_MODE_DIRS:
                continue
            out.append(child.name)
        return out

    def umbrella_dir_name(self, base: str):
        root = self._agent_dirs().get(base)
        return root.name if root else None

    # -- how this host runs an engine ----------------------------------

    def spawn_path(self, *prepend: str, inherit: str | None = None) -> str:
        parts = [*prepend, *(str(Path(d).expanduser()) for d in _DEFAULT_SPAWN_DIRS)]
        if inherit:
            parts.append(inherit)
        return ":".join(p for p in parts if p)

    def default_model(self) -> str:
        return "opus"

    def project_dir_agent_overrides(self) -> dict[str, str]:
        """No history to explain — a fresh host's transcripts name their own
        agent, and inventing an override would mislabel somebody else's."""
        return {}

    def session_labels(self) -> dict[str, str]:
        """Nobody has named a session on a host with no label store.

        Empty is the honest answer and the board already handles it: a row with
        no label falls back to what it can derive from the process itself.
        """
        return {}

    # -- where this host keeps jRemote's own state -------------------------

    def state_dir(self) -> Path:
        """`~/.local/state/jremote/`, beside the transcript cache in `~/.cache`.

        Not a directory relative to the package. Every one of these files used
        to resolve as `<package>/../state`, which on a host where the package
        is not inside a dashboard does not fail — it silently picks whatever
        directory happens to sit there, and the host comes up with an empty
        board and no error. An absolute path under the user's own tree is the
        one answer that cannot be quietly wrong.
        """
        return HOME / ".local" / "state" / "jremote"

    # -- what this host records about itself (feed, spend, allowance) --------

    def repo_root(self) -> Path:
        """Where this machine keeps its checkouts: `JSTACK_REPO_ROOT`, else the
        parent of the agents root — jStack's own default for `repo_root`, the
        layout `Agents/` sits beside the repos it works on."""
        env = os.environ.get("JSTACK_REPO_ROOT", "").strip()
        return Path(env).expanduser() if env else self.root.parent

    def repos(self) -> list[Path]:
        """Every git checkout under the repo root, main worktrees only.

        Found, not declared: the registry's `repos` lists are names, and a
        checkout the registry never mentions still shipped commits today.
        Build trees are pruned — a package checkout under SourcePackages/
        is SwiftPM's, not ours — and so is jStack's own clone, which is the
        tool rather than the work. See `_own_checkout`.
        """
        return [r for r in _git_checkouts(self.repo_root())
                if not _own_checkout(r)]

    def repo_agent(self, repo: Path) -> str:
        """The agent the registry says owns this checkout, or ''. Names are
        folded the way jStack's `repo_seat` folds them, so `ProjectName_iOS`
        and `ProjectName-iOS` are one repo whichever spelling the entry
        used."""
        want = _fold(repo.name)
        for base, entry in self._registry().items():
            if any(_fold(str(r).rsplit("/", 1)[-1]) == want
                   for r in entry.get("repos") or []):
                return base
        return ""

    def repo_project(self, repo: Path) -> str:
        """Which project a checkout belongs to — '' on a host with no project
        registry, which files its commits under no project rather than under a
        guessed one."""
        return ""

    def control_module(self) -> str:
        """Dotted module for the host's control tier, '' when it has none.
        Daemon relaunches are the embedding host's own machinery — only its
        profile can name the module that drives them."""
        return ""

    def scheduler_dir(self) -> Path:
        """Legacy scheduler home; split directory accessors handle JSTACK_ROOT."""
        env = os.environ.get("SCHEDULER_HOME", "").strip()
        return Path(env).expanduser() if env else HOME / ".scheduler"

    def scheduler_config_dir(self) -> Path:
        if os.environ.get("SCHEDULER_CONFIG_DIR"):
            return Path(os.environ["SCHEDULER_CONFIG_DIR"]).expanduser()
        if "SCHEDULER_HOME" not in os.environ and os.environ.get("JSTACK_ROOT"):
            return Path(os.environ.get("JSTACK_CONFIG_DIR") or
                        str(Path(os.environ["JSTACK_ROOT"]).expanduser() / "Config")).expanduser()
        return self.scheduler_dir() / "config"

    def scheduler_state_dir(self) -> Path:
        if os.environ.get("SCHEDULER_STATE_DIR"):
            return Path(os.environ["SCHEDULER_STATE_DIR"]).expanduser()
        if "SCHEDULER_HOME" not in os.environ and os.environ.get("JSTACK_ROOT"):
            return Path(os.environ.get("JSTACK_STATE_DIR") or
                        str(Path(os.environ["JSTACK_ROOT"]).expanduser() / "State")).expanduser() / "scheduler"
        return self.scheduler_dir() / "state" / "scheduler"

    def spend_categories_path(self) -> Path:
        """Where a finer spend-category map may sit, in the state dir. The
        file is optional — `spend.py` falls back to its built-in two-rule
        split when nothing is there."""
        return self.state_dir() / "token_categories.json"

    def day_tz(self):
        """The tzinfo spend buckets days in — None means the host's local
        clock, which is what every other stamp on a default host uses. A
        profile answers a fixed zone when its other reports are pinned to one
        and a DST-exact day boundary would quietly disagree with them."""
        return None

    def timeline_db(self) -> Path:
        return _jstack_timeline_db()

    def pings_db(self) -> Path | None:
        """No ping lane on a standalone host — nothing here sends to a chat."""
        return None

    def token_path(self, state: Path) -> Path:
        """Inside the state dir — the *effective* one, not `self.state_dir()`.

        A host's token belongs to that host. Building this from the profile's
        own default instead would mean `--state-dir` moved a second host's
        board, sessions and devices while quietly leaving it reading the first
        host's token: two hosts, one credential, and no error anywhere.
        """
        return state / "api-token"

    def releases_dir(self, state: Path) -> Path:
        return state / "releases" / "mac"

    def credentials_dir(self) -> Path:
        """`~/.local/share/jremote/credentials` — beside state, not inside it.

        APNs keys and a signed Hub's mesh credentials survive state resets.
        """
        return HOME / ".local" / "share" / "jremote" / "credentials"

    def peer_script(self) -> Path:
        """The mesh tool, beside the package that installed it with it."""
        return package_root() / "scripts" / "wireguard" / "wg_peer.py"

    def wireguard_dir(self) -> Path:
        """Signed apps keep mutable mesh state outside their resource seal."""
        if str(package_root()).endswith(".app/Contents/Resources/packages"):
            return credentials_dir() / "wireguard"
        return package_root() / "Credentials" / "wireguard"

    def security_alert(self, body: str) -> None:
        """The server log — a standalone host has no messaging channel of its
        own, and a loud line where its logs are read is the honest maximum."""
        print(f"jremote SECURITY: {body}", flush=True)

    # -- project dirs --------------------------------------------------

    def project_dir_to_agent(self, dirname: str):
        """Reverse Claude Code's project-dir encoding against the instance root.

        Claude names a project dir after its working directory with every `/`
        replaced by `-`, so `/Users/x/Agents/Ada/chat` is stored as
        `-Users-x-Agents-Ada-chat`. Hardcoding a home prefix and an
        agent-name pattern would bake in facts about one machine, not about
        the encoding. Here the root encodes itself,
        and the base is resolved by asking which directory actually exists —
        so an agent whose name carries a hyphen or a digit still resolves.

        The root must match at a path boundary. A bare `startswith` lets a
        *sibling* root claim our agents — with the root `~/Agents`, the dir
        `-Users-x-AgentsAda-chat` strips to `Ada-chat` and resolves, which
        would file another tree's session under a local agent.

        One ambiguity is inherent to the encoding and cannot be decided from
        the string: `/x/Agents/backup/L` and `/x/Agents-backup/L` encode
        identically. Both read as ours; only the first exists as a directory,
        so the base lookup below settles it in every case that matters.
        """
        prefix = str(self.root).replace("/", "-")
        if not dirname.startswith(prefix + "-"):
            return None
        rest = dirname[len(prefix) + 1:].lstrip("-")
        if not rest:
            return None
        dirs = self._agent_dirs()
        parts = rest.split("-")
        # Longest directory-backed head wins: 'service-call' is a mode, but an
        # agent literally named 'service-call' would outrank it.
        for i in range(len(parts), 0, -1):
            head = "-".join(parts[:i])
            if head.lower() in dirs:
                mode = "-".join(parts[i:]) or "default"
                return head.lower(), mode
        return None


def _resolve_nested(root: Path, mode: str) -> str | None:
    """`chat-reminder` under an agent root → `chat/reminder`, if it exists."""
    if "-" not in mode:
        return None
    parts = mode.split("-")
    for i in range(1, len(parts)):
        head = "-".join(parts[:i])
        tail = "-".join(parts[i:])
        sub = root / head
        if not sub.is_dir():
            continue
        if (sub / tail).is_dir():
            return f"{head}/{tail}"
        deeper = _resolve_nested(sub, tail)
        if deeper:
            return f"{head}/{deeper}"
    return None


def _jstack_timeline_db() -> Path:
    """jStack's timeline store — the one file `log_event` writes.

    The chain is `plugins/jstack/root.py::timeline_dir()`, restated here
    because the host package cannot import the plugin: the timeline's own
    override, then the logs dir's, then the install root. Every step must stay
    in step with that file.

    It used to stop at `$HOME/Logs/Timeline` and call that "where log_event
    writes on every machine", which is true only where the root IS $HOME. On an
    install rooted elsewhere the plugin wrote `{JSTACK_ROOT}/Logs/Timeline` and
    this read an empty `$HOME` store: sessions logged correctly, the Timeline
    tab, the tag picker and the doctor showed nothing, and an empty store is a
    legitimate state — so nothing anywhere reported a fault. The running-memory
    loop wrote to one database and read from another.
    """
    timeline = os.environ.get("JSTACK_TIMELINE_DIR", "").strip()
    if timeline:
        return Path(timeline).expanduser() / "timeline.db"
    logs = os.environ.get("JSTACK_LOGS_DIR", "").strip()
    base = Path(logs).expanduser() if logs else stack_root() / "Logs"
    return base / "Timeline" / "timeline.db"


_PRUNED_TREES = {"build", ".build", "SourcePackages", "node_modules",
                 "DerivedData", "Pods", ".venv", "venv", "scratch", "pad"}


def _git_checkouts(root: Path, depth: int = 3) -> list[Path]:
    """Directories holding a `.git` directory under `root`, at most `depth`
    levels down. A `.git` *file* is a linked worktree and is skipped — its
    history is the main checkout's, and listing both bills every commit twice."""
    out: list[Path] = []
    if not root.is_dir():
        return out
    base_depth = len(root.parts)
    for dirpath, dirnames, _ in os.walk(root):
        here = Path(dirpath)
        if (here / ".git").is_dir():
            out.append(here)
            dirnames[:] = []          # a repo's subdirectories are its own
            continue
        if len(here.parts) - base_depth >= depth:
            dirnames[:] = []
            continue
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".") and d not in _PRUNED_TREES)
    return out


def _jstack_checkout() -> Path | None:
    """The clone jStack itself lives in, or None where it has no clone.

    The nearest `.git` ancestor of the installed plugin — NOT any checkout
    that happens to contain it. A home directory that is itself a repo
    contains the jStack clone too, and matching on containment alone prunes
    the user's entire tree to remove one directory inside it.

    None is the right answer twice over: on a machine with no jStack, and on
    one running the plugin from `plugins/cache/` where there is no clone to
    confuse with anybody's work.
    """
    try:
        from . import plugin_paths
        here = plugin_paths.jstack_root().resolve()
    except (OSError, RuntimeError):
        return None
    for candidate in [here, *here.parents]:
        try:
            if (candidate / ".git").is_dir():
                return candidate
        except OSError:
            return None
    return None


def _own_checkout(repo: Path) -> bool:
    """Is this checkout jStack's own clone rather than the user's work?

    The feed's git producer answers "what shipped here today". jStack's
    development history is the tool's, not the machine's — and on a Mac that
    has just run the installer it is the ONLY checkout under the root, so the
    Timeline tab opens on a day made entirely of commits the user never wrote
    and cannot place. Same principle as `_PRUNED_TREES` dropping
    SourcePackages: someone else's history, sitting inside our tree.

    Matched by path, so a clone under a different directory name is still
    recognised, and a repo that merely contains the clone is not.
    """
    own = _jstack_checkout()
    if own is None:
        return False
    try:
        return repo.resolve() == own
    except OSError:
        return False


def _fold(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------

_profile = None


def instance_root_marker() -> Path:
    """The file the installer writes so an env-less host still finds its agents.

    A fixed path under `$HOME` — deliberately *not* under `state_dir()`, which
    resolves through `profile()` → `instance_root()` and would make this
    recursive, and deliberately *not* under `~/Library/Application Support`,
    which is TCC-gated: any non-owning reader (the CLI, a test, the installer)
    trips a permission dialog on the user's screen. `~/.config` is plain,
    dialog-free, and readable by the host. The sealed Hub launches through
    SMAppService with `HOME` and nothing else (no login shell, so no
    `$JSTACK_ROOT`; the old `JREMOTE_INSTANCE_ROOT` was pinned into a
    `com.jremote.host` plist that the sealed app never loads). `HOME` is the one
    thing it always has, so the root it was installed against is recorded here,
    keyed off `HOME` alone."""
    return HOME / ".config" / "jstack" / "instance_root"


def _marker_root() -> Path | None:
    """The instance root recorded at install time, or None if unwritten."""
    try:
        text = instance_root_marker().read_text().strip()
    except OSError:
        return None
    return Path(text).expanduser() if text else None


def write_instance_root(root: Path) -> Path:
    """Record the instance root so a launchd-spawned host resolves it without a
    shell. Called by the installer, which knows the root from `$JSTACK_ROOT`."""
    marker = instance_root_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(Path(root).expanduser()) + "\n")
    return marker


def stack_root_marker() -> Path:
    """Where the install root is recorded, beside `instance_root_marker()`.

    Same reasoning, same directory, written by the same installer step — see
    that function for why `~/.config` and why keyed off `HOME` alone. Two
    markers rather than one because the two answers are genuinely independent:
    `--agent-root` may put agents outside the root, and deriving either from
    the other would make a supported layout resolve one of them wrong.
    """
    return HOME / ".config" / "jstack" / "root"


def write_stack_root(root: Path) -> Path:
    """Record the install root so a launchd-spawned host resolves it without a
    shell. Called by the installer, which knows it from `$JSTACK_ROOT`."""
    marker = stack_root_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(Path(root).expanduser()) + "\n")
    return marker


def stack_root() -> Path:
    """The install root — `$JSTACK_ROOT`, else the install-time marker, else
    `$HOME`. The host's answer to `plugins/jstack/root.py::root()`.

    The middle step is the sealed Hub's, which launches through SMAppService
    with `HOME` and nothing else: no login shell, so no `$JSTACK_ROOT`, and the
    service environment allowlist (`macos/runtime_entry.py`) admits no override
    of its own. Without a marker it fell through to `$HOME` and every derived
    directory went with it.

    The last fallback before `$HOME` reads the *agents* marker, and only when
    it names an `Agents` directory — the layout every installer writes. It is a
    migration read, not a derivation: a machine installed before this marker
    existed has no root recorded, and one witness that can only be right or
    absent beats resolving its logs to a directory nothing writes.
    """
    env = os.environ.get("JSTACK_ROOT", "").strip()
    if env:
        return Path(env).expanduser()
    try:
        text = stack_root_marker().read_text().strip()
    except OSError:
        text = ""
    if text:
        return Path(text).expanduser()
    agents = _marker_root()
    if agents is not None and agents.name == "Agents":
        return agents.parent
    return HOME


def instance_root() -> Path:
    """Where the default profile looks for agents.

    Order matters, and the last step is a safety rule, not a convenience:

    · `JREMOTE_INSTANCE_ROOT` wins — a test points it at a fixture, and a
      shell-launched tool that exports it means it.
    · else `$JSTACK_ROOT/Agents` — the one tree the installer, the plugin and
      `jstack-doctor` all resolve agents against. Honouring it here is what
      makes the host agree with the doctor a person just watched pass.
    · else the **install-time marker** (`instance_root_marker()`) — this is the
      answer for the sealed Hub, which launches with no shell and so has
      neither env var above. Without it `active_agents()` read an empty
      `~/Agents` and the app's Agents tab came up blank on every install whose
      tree lives under `$JSTACK_ROOT`.
    · else `$HOME/Agents`, **even when that directory does not exist.**

    The old last resort was a bare `$HOME`, and it was the blank-thread bug:
    on an install whose agents live under `$JSTACK_ROOT` (not `~/Agents`),
    `instance_root()` fell through to the home directory, `active_agents()`
    read every folder in it — Desktop, Documents, a CI runner's checkout — as
    an agent, and `welcome` opened its first session in the alphabetically
    first one. An absent agents tree must read as *zero* agents, which
    `_agent_dirs()` returns for a path that isn't there; a home full of
    invented ones is the single answer this must never give."""
    env = os.environ.get("JREMOTE_INSTANCE_ROOT")
    if env:
        return Path(env).expanduser()
    jstack_root = os.environ.get("JSTACK_ROOT")
    if jstack_root:
        return Path(jstack_root).expanduser() / "Agents"
    marker = _marker_root()
    if marker is not None:
        return marker
    return HOME / "Agents"


def profile_module_name() -> str:
    """The module the machine may supply: `JREMOTE_PROFILE_MODULE`, else the
    conventional name. Named by env rather than found by search — a profile
    decides what this host serves, and only the host's own configuration may
    pick one."""
    return os.environ.get("JREMOTE_PROFILE_MODULE", "").strip() or "jremote_host_profile"


def profile():
    """The active profile, resolved once.

    `auto` asks the machine first and falls back to default — a missing or
    unloadable profile module is a machine that never configured one, which is
    the standalone host, not a fault. `external` insists and raises instead:
    running as something other than what was asked for surfaces much later, in
    a board nobody can explain.
    """
    global _profile
    if _profile is not None:
        return _profile
    want = os.environ.get("JREMOTE_HOST_PROFILE", "auto").strip().lower()
    if want in ("auto", "external"):
        try:
            mod = importlib.import_module(profile_module_name())
            _profile = mod.make_profile()
            return _profile
        except Exception:
            if want == "external":
                raise
    _profile = DefaultProfile(instance_root())
    return _profile


def reset_profile():
    """Forget the resolved profile — tests flip the env and re-resolve."""
    global _profile
    _profile = None


# --------------------------------------------------------------------------
# The API the package imports — same names, same contracts, one seam.
# --------------------------------------------------------------------------

def active_agents():
    return profile().active_agents()


def workspace(agent_id: str) -> Path:
    return profile().workspace(agent_id)


def split_id(agent_id: str):
    return profile().split_id(agent_id)


def project_dir_to_agent(dirname: str):
    return profile().project_dir_to_agent(dirname)


def submode_dirs(base: str):
    return profile().submode_dirs(base)


def umbrella_dir_name(base: str):
    return profile().umbrella_dir_name(base)


def spawn_path(*prepend: str, inherit: str | None = None) -> str:
    return profile().spawn_path(*prepend, inherit=inherit)


def default_model() -> str:
    return profile().default_model()


def project_dir_agent_overrides() -> dict[str, str]:
    return profile().project_dir_agent_overrides()


def session_labels() -> dict[str, str]:
    return profile().session_labels()


def repos() -> list[Path]:
    return profile().repos()


def repo_agent(repo: Path) -> str:
    return profile().repo_agent(repo)


def repo_project(repo: Path) -> str:
    """Guarded with getattr, like every seam answer added after the first
    profiles shipped: an external profile written against the older seam is
    still a valid profile, and it gets the default answer, not a crash."""
    fn = getattr(profile(), "repo_project", None)
    return fn(repo) if fn else ""


def spend_categories_path() -> Path:
    fn = getattr(profile(), "spend_categories_path", None)
    return fn() if fn else state_dir() / "token_categories.json"


def day_tz():
    fn = getattr(profile(), "day_tz", None)
    return fn() if fn else None


def control_module() -> str:
    fn = getattr(profile(), "control_module", None)
    return fn() if fn else ""


def scheduler_dir() -> Path:
    return profile().scheduler_dir()


def scheduler_config_dir() -> Path:
    fn = getattr(profile(), "scheduler_config_dir", None)
    return fn() if fn else scheduler_dir() / "config"


def scheduler_state_dir() -> Path:
    fn = getattr(profile(), "scheduler_state_dir", None)
    return fn() if fn else scheduler_dir() / "state" / "scheduler"


def timeline_db() -> Path:
    return profile().timeline_db()


def pings_db() -> Path | None:
    return profile().pings_db()


def in_test_process() -> bool:
    """Whether this interpreter is a test run rather than a serving host.

    Two signals because neither alone covers the window. `PYTEST_CURRENT_TEST`
    is the documented marker, but pytest sets it per *test* and deletes it when
    that test ends — and this package raises alarms from daemon threads
    (`auth._gate`, `enrolment`), so a lockout tripped in a test's last
    microseconds can reach the send after the variable is gone. `pytest` in
    `sys.modules` is the process-lifetime half: true from the first import to
    interpreter exit, which is the window that actually needs covering.

    A serving host imports neither, so this is False everywhere it matters.
    """
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) or "pytest" in sys.modules


def security_alert(body: str) -> None:
    """Raise a security alarm the way this host can — the profile's own
    channel where one exists, the server log anywhere else. Failure is
    swallowed loudly: the
    alert path must never take down the request that tripped it.

    **A test process never alarms a person.** Suites drive this path with
    synthetic lockouts by design, and on a host whose profile alerts over a
    messaging channel every one of those fixtures pages a human — the
    embedding host that found this had taken 100 of them over twelve days. A
    conftest fixture that swaps the sink is opt-in per checkout: it fixes the
    tree it lands in and leaves every stale worktree and staged copy still
    wired to the live channel. The refusal belongs here, where no checkout can
    opt out of it.
    """
    if in_test_process():
        print(f"jremote SECURITY (test process, not delivered): {body}",
              flush=True)
        return
    try:
        profile().security_alert(body)
    except Exception as e:  # noqa: BLE001
        print(f"jremote SECURITY (alert channel failed: {type(e).__name__}): "
              f"{body}", flush=True)


def package_root() -> Path:
    """The directory this package is importable *from*.

    Not machine state and not a profile question — it is wherever the package
    was installed, and the only correct answer is derived from this file. Used
    as the cwd for `python -m jstack_host.…` subprocesses, which resolve the
    module by import and so must be started somewhere it imports.

    It was `parents[2]` for as long as the package lived two levels inside a
    larger tree, which is the kind of constant that keeps working right up
    until the package moves and then picks a directory that merely exists.
    """
    return Path(__file__).resolve().parents[1]


def credentials_dir() -> Path:
    """Where this host keeps secrets it did not mint itself.

    The APNs signing key and the WireGuard private keys — things a host is
    *given*, as opposed to the bearer token it generates for itself (that one
    is `token_path()`, under state).

    `JREMOTE_CREDENTIALS_DIR` overrides. Every caller resolves through here
    rather than naming a directory relative to the source, because a secret
    found by walking up from `__file__` is a secret that silently relocates
    when the package does — and the failure is a host that comes up fine and
    cannot push a notification.
    """
    env = os.environ.get("JREMOTE_CREDENTIALS_DIR")
    if env:
        return Path(env).expanduser()
    return profile().credentials_dir()


def peer_script() -> Path:
    """`wg_peer.py` — the tool that edits this host's wireguard peer table.

    A profile answer and not a path derived from the source, because the mesh
    tooling and the package do not have to have arrived together. A host that
    installed both gets the copy beside the package; a machine whose tunnel
    predates the package keeps the copy its own daemons already drive, and the
    two never end up writing and reading different peer tables.

    `JREMOTE_PEER_SCRIPT` overrides.
    """
    env = os.environ.get("JREMOTE_PEER_SCRIPT")
    if env:
        return Path(env).expanduser()
    return profile().peer_script()


def wireguard_dir() -> Path:
    """Where this host's mesh state lives — `wg0.conf`, the keys, `endpoint`.

    A profile answer for the same reason `peer_script()` is one, and it is the
    same reason twice: the mesh tooling and the package do not have to have
    arrived together, and a host that reads one directory while its own daemons
    write another has two answers for one mesh. Splitting them is not a cosmetic
    drift — it is `can_pair()` returning False on the machine that owns the
    peer table, which takes `/tunnel/pair` to a 503 and reports a hub as
    `local` (#42).

    `WG_PEER_DIR` overrides, first and outright, because that is the variable
    `wg_peer.py` itself honours — the tool and its readers move together or the
    split comes back under a different name. That sentence was true of this
    module and false of the mesh: `install_hub.sh`, `wg_up.sh` and `wg_sync.sh`
    each derived the directory from their own location and ignored the variable
    entirely, so a relocated mesh had the pairing tool writing peers into one
    conf while the installer minted — and both root daemons loaded — another.
    All four honour it now, and `test_jremote_wireguard.py` proves it by running
    them rather than by reading them.

    Read through `getattr` so a profile written before this existed keeps
    working: an external profile is somebody else's file, and a package upgrade
    that raises AttributeError on their machine is a package that broke them.
    """
    env = os.environ.get("WG_PEER_DIR")
    if env:
        return Path(env).expanduser()
    answer = getattr(profile(), "wireguard_dir", None)
    if answer is not None:
        return answer()
    return package_root() / "Credentials" / "wireguard"


def state_dir() -> Path:
    """Where jRemote keeps its own state on this host.

    `JREMOTE_STATE_DIR` overrides both profiles — it is how a second host on
    this same Mac (a test, a second instance) gets its own state without
    touching the dashboard's, and how anyone relocates it without a code edit.
    It must be set before the package is imported, because the seven modules
    that name a file in here bind it as a module constant.

    Answers a path and does not create it. Making a directory is the kind of
    thing an import may not do, and these callers are all module-level.
    `ensure_state_dir()` is the create, and the host entry point calls it once.
    """
    env = os.environ.get("JREMOTE_STATE_DIR")
    return Path(env).expanduser() if env else profile().state_dir()


def ensure_state_dir() -> Path:
    """`state_dir()`, made to exist. For a host that is starting up."""
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def token_path() -> Path:
    """The bearer token this host expects, as an absolute path.

    `JREMOTE_TOKEN_PATH` overrides. Provisioning a new host is then one command
    with no code edit and no assumption about where the package was unpacked —
    which is the whole point of the file being named by the host rather than
    found relative to the source.
    """
    env = os.environ.get("JREMOTE_TOKEN_PATH")
    if env:
        return Path(env).expanduser()
    return profile().token_path(state_dir())


def releases_dir() -> Path:
    """Where published Mac app builds live.

    Its own answer rather than a subdirectory of `state_dir()`, because on this
    Mac it already is its own directory (`Infrastructure/state/jremote-releases`,
    not `dashboard/state`) and moving it would strand the published builds the
    app updater is pointed at.
    """
    env = os.environ.get("JREMOTE_RELEASES_DIR")
    if env:
        return Path(env).expanduser()
    return profile().releases_dir(state_dir())


# ── Who this host is ──

class SecondIdentity(RuntimeError):
    """Refused: a host identity was about to be minted into a state dir that
    no host on this machine is serving."""


def refuse_second_identity(state, what: str) -> None:
    """Raise unless `state` is the state dir a host here actually serves.

    MINTING IS THE ONE WRITE THAT CANNOT BE UNDONE BY GETTING THE PATH RIGHT
    LATER. Everything else a stray process leaves in the wrong state dir is
    junk: a feed it will re-derive, a cache it will rebuild, a lock nobody
    holds. A `host-id` is a *claim*, and the app believes claims — it routes on
    them, so a second id for one machine is read as a second machine at the
    same address, every session on its board real and none of them the ones
    asked for. Same for the internal token: `internal_token()` re-keys the
    `host-internal` row whenever the plaintext beside it is missing, so a
    process on the wrong dir mints into a store the real host never reads, and
    d9b432f's read-compare-write race comes back across two directories
    instead of within one (#45).

    So the question is not "is there a file here" but "is anything serving
    here", and the embed marker is the only thing on disk that answers it. A
    host mounted into another server declares its own state dir in
    `~/.local/state/jremote/embedded.json` precisely so a process that knows
    nothing can find it (#34). A marker naming a *different* directory is that
    host saying, in writing, that this is not where it lives.

    NO MARKER MEANS NO OPINION, AND THAT IS DELIBERATE. A fresh standalone
    host's very first call has to mint, and a machine that never embedded one
    has nothing to disagree with. Refusing on absence would make a new install
    unbootable to protect it from a second host it does not have. The refusal
    fires only where the evidence is positive and specific: something declared
    a state dir, and it is not this one.

    AN EXPLICIT `JREMOTE_STATE_DIR` NAMING THIS DIR IS CONSENT, NOT CONFUSION.
    Setting it is the remedy this refusal's own message prescribes, and
    `state_dir()` documents it as how a second host on this same Mac — a test,
    a second instance — gets its own state beside the embedded one. A process
    that set the variable and is minting into the very directory it names has
    already answered "is anything serving here": it is, deliberately, this
    process. The refusal still fires when the variable and the mint target
    disagree — that is a process resolving one dir and writing another — and
    when the variable is unset, where the marker is the only voice.

    Loud, not quiet. The alternative is to adopt the declared dir silently,
    which repairs this caller and hides that it resolved wrong — and a process
    that got here has a broken path, a missing `JREMOTE_STATE_DIR`, or an
    `embed.adopt()` it never called. The whole of #34 was a command answering
    confidently about a host that does not exist; answering confidently *as*
    one is the same failure with a write behind it.
    """
    env = os.environ.get("JREMOTE_STATE_DIR")
    if env and Path(env).expanduser().resolve() == Path(state).resolve():
        return
    from . import embed  # local: embed imports this module

    declared = str(embed.read().get("state_dir") or "")
    if not declared:
        return
    if Path(declared).expanduser().resolve() == Path(state).resolve():
        return
    raise SecondIdentity(
        f"refusing to mint {what} into {state}: the host on this machine "
        f"serves {declared} (declared in {embed.marker_path()}). This process "
        f"resolved the wrong state dir — call embed.adopt(), or set "
        f"JREMOTE_STATE_DIR, before asking this host who it is.")


def host_id() -> str:
    """A stable id for this host, minted once into its state dir.

    The app needs to know *which machine* it is talking to, and a URL cannot
    tell it: the same Mac is a `.local` name on the LAN, an in-tunnel address
    from a cafe, and `127.0.0.1` to an app running on it. Three addresses, one
    instance — and two instances can each be `127.0.0.1` to their own app.

    That last case is the one that bites. Local-first routing wants to prefer
    loopback whenever this Mac is itself a host, but an app configured for the
    second host and *running* on this one would then quietly draw the
    wrong machine's board — every session real, none of them the ones asked
    for. Comparing ids is what makes that impossible.

    Minted at call time, never at import: this writes a file, and an import
    that writes is exactly what `state_dir()` refuses to be.

    Reading is unconditional; *minting* is not. A dir that already holds an id
    is answering for whoever put it there, and the answer is the same whoever
    asks. Creating one is a claim about this machine, and `refuse_second_identity`
    is where that claim has to survive a second host having already made it.
    The check sits between the read and the mint for that reason — and before
    `ensure_state_dir()`, so a refused call does not leave the directory it
    refused to write in.
    """
    env = os.environ.get("JREMOTE_HOST_ID")
    if env:
        return env
    state = state_dir()
    path = state / "host-id"
    try:
        existing = path.read_text().strip()
        if existing:
            return existing
    except OSError:
        pass
    refuse_second_identity(state, "host-id")
    ensure_state_dir()
    minted = str(uuid.uuid4())
    path.write_text(minted)
    return minted


def host_name() -> str:
    """What to call this host in the app's instance grid.

    The machine's own name by default, because that is the name its user
    already knows it by and one nobody has to maintain. `JREMOTE_HOST_NAME`
    overrides for a machine whose hostname is not what anyone calls it.
    """
    env = os.environ.get("JREMOTE_HOST_NAME", "").strip()
    if env:
        return env
    # First label only. `gethostname()` can answer `studio.home` — the
    # router's domain, not part of what anyone calls this Mac; on another
    # network the same machine would be `studio.local` and the grid would show
    # a differently-named row for it. The label before the first dot is the
    # machine; everything after is whichever network it woke up on.
    return socket.gethostname().split(".")[0] or "jRemote host"
