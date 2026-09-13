"""root — the one declaration a jStack install derives its tree from.

"Where is everything" used to be answered in six places: SCHEDULER_HOME, an
`agent_root` in scheduler.json, the same default restated independently in
three hooks, and absolute machine paths in review.json. Each was a separate
chance to disagree, and on a second machine they did — half the tools resolved
into a private tree that machine never had. This module is the single answer:
one root, everything below it by structure.

    $JSTACK_ROOT/
    ├── Agents/         agent workspaces; a seat is Agents/<id>/<seat>/
    ├── Systems/        Systems/<slug>/SYSTEM.md
    ├── Config/         scheduler.json, schedule.json, review.json
    ├── State/          runtime state, runs, locks
    ├── Logs/           timeline db, tool logs
    └── Credentials/    tokens (never in the checkout)

Callers hand in their own already-parsed config dict. This module never reads
a config file itself: reading one would make it a second opinion about which
file is authoritative, which is the exact disease being cured.

Import discipline: this file imports nothing from the package and nothing
beyond the stdlib. It is loaded two ways — as a sibling (`import root` with
the plugin dir on sys.path, which is how scheduler/* reaches it) and by
standalone bin/ scripts that insert the plugin dir themselves — and anything
it imported from the tree would close a cycle on the first module that
imports it back.

Every function resolves on every call. A daemon lives for weeks and the test
suite changes the environment between cases; a module constant captured at
import answers with the environment of a process start nobody remembers —
that is exactly how five tests once errored at setup instead of running.
Recomputing is a handful of dict lookups; correctness beats a saved syscall.

Results are `expanduser()`-ed but never `resolve()`-d: a symlinked workspace
is legitimate, and resolving it changes the path a session reports as its cwd.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple


def _shipping_tree_roots() -> "tuple[Path, ...]":
    """The trees no jStack data dir may resolve into: the checkout shipping this file.

    Same rule as `scheduler/config.py::_shipping_tree_roots`, and deliberately
    a copy rather than an import. config.py must keep working on an older
    install where this module is absent, and importing config here would hand
    root.py the very cycle it exists to stay free of — so both files carry the
    rule, each self-contained. If the rule ever changes, change it in both.

    This package is distributed inside a PUBLIC git repository. A Config/,
    State/, Logs/ or Credentials/ dir rooted in that checkout puts a live
    token one `git add -A` from being published — so resolving there is an
    error, never a fallback. Forbidden: this file's own directory always (a
    plugin-cache install has no .git, but is still wiped on update), plus the
    nearest enclosing git checkout. The walk stops before $HOME so a user
    whose home directory is itself a repo (dotfiles) keeps ~ usable.
    """
    here = Path(__file__).resolve().parent
    roots = [here]
    home = Path.home()
    for candidate in here.parents:
        if candidate == home or candidate == candidate.parent:
            break
        if (candidate / ".git").exists():
            if candidate not in roots:
                roots.append(candidate)
            break
    return tuple(roots)


def _refuse_shipping_tree(path: Path, env_var: str) -> Path:
    """Refuse a data dir inside the shipping checkout, loudly.

    Failing beats silently picking somewhere else: a secret written to a
    wrong-but-safe place is recoverable; one written into a public working
    tree is not. The refusal is the same whether the value came from the
    environment, a config file, or the derivation — the repo is not a valid
    home even on purpose.
    """
    probe = Path(os.path.expanduser(str(path))).resolve()
    for tree in _shipping_tree_roots():
        if probe == tree or tree in probe.parents:
            raise RuntimeError(
                f"{env_var}={path} resolves inside the checkout that ships "
                f"this package ({tree}) — a public git tree. Config, state, "
                f"logs and credentials must live outside it: set JSTACK_ROOT "
                f"(or the {env_var} override) to a directory outside the "
                f"repository."
            )
    return path


def root(cfg: "dict|None" = None) -> Path:
    """The install root: $JSTACK_ROOT, else the caller's cfg["root"], else $HOME.

    No walk-up marker search, no probing — one declaration, stated or
    defaulted. `cfg` is the caller's own already-parsed config file, so there
    is exactly one opinion about which file is authoritative: the caller's.
    """
    val = os.environ.get("JSTACK_ROOT")
    if not val and cfg:
        val = cfg.get("root")
    if val:
        return _absolute(val)
    return Path.home()


def _absolute(val, name: str = "JSTACK_ROOT") -> Path:
    """A declared root is absolute or it is refused.

    A relative one reaches launchd, which requires absolute paths in
    WorkingDirectory and StandardErrorPath and refuses to spawn without them:
    the job exits 78 (EX_CONFIG) before it runs a line, KeepAlive retries it
    forever, and the only symptom is a daemon that is "loaded but not running".
    That was a whole install ending on a red FAIL because a root was typed
    without a leading slash.

    Anchoring it to $HOME instead would be worse — the same string would then
    mean two different directories depending on which one of these two paths
    the reader took. It refuses, and names what to type instead.
    """
    path = Path(str(val)).expanduser()
    if not path.is_absolute():
        raise ValueError(
            f"{name} must be an absolute path, got {val!r} — "
            f"launchd refuses a relative one and the daemons will not start. "
            f"Try {Path.home() / str(val).lstrip('./')}"
        )
    return path


def _derived(cfg: "dict|None", env_var: str, cfg_key: str, leaf: str,
             guarded: bool, base=None) -> Path:
    """One derived dir: its env override, else its cfg key, else base()/leaf.

    `base` defaults to root() — the top-level dirs hang straight off it. A dir
    nested inside another one passes that other one's accessor, so it follows
    its parent's override instead of re-deriving from the root: with
    JSTACK_LOGS_DIR pointed elsewhere, the timeline goes with the logs rather
    than staying behind in a Logs/ nobody is writing to.
    """
    val = os.environ.get(env_var)
    if not val and cfg:
        val = cfg.get(cfg_key)
    if val:
        path = _absolute(val, env_var)
    else:
        path = (root(cfg) if base is None else base(cfg)) / leaf
    if guarded:
        _refuse_shipping_tree(path, env_var)
    return path


def agents_dir(cfg: "dict|None" = None) -> Path:
    """Where agent workspaces live.

    The cfg key is the EXISTING `agent_root`: installs already declare it, and
    renaming a key every install has set breaks them for the sake of symmetry.
    Not guarded — a user may legitimately keep agent workspaces inside a repo.
    """
    return _derived(cfg, "JSTACK_AGENTS_DIR", "agent_root", "Agents", guarded=False)


def systems_dir(cfg: "dict|None" = None) -> Path:
    """Systems/<slug>/SYSTEM.md lives here. Not guarded — docs, not secrets."""
    return _derived(cfg, "JSTACK_SYSTEMS_DIR", "systems_root", "Systems", guarded=False)


def config_dir(cfg: "dict|None" = None) -> Path:
    return _derived(cfg, "JSTACK_CONFIG_DIR", "config_dir", "Config", guarded=True)


def state_dir(cfg: "dict|None" = None) -> Path:
    return _derived(cfg, "JSTACK_STATE_DIR", "state_dir", "State", guarded=True)


def logs_dir(cfg: "dict|None" = None) -> Path:
    return _derived(cfg, "JSTACK_LOGS_DIR", "logs_dir", "Logs", guarded=True)


def credentials_dir(cfg: "dict|None" = None) -> Path:
    return _derived(cfg, "JSTACK_CREDENTIALS_DIR", "credentials_dir",
                    "Credentials", guarded=True)


def timeline_dir(cfg: "dict|None" = None) -> Path:
    """Where timeline.db lives — one answer, because it is one database.

    Three tools wrote this default independently: bin/log_event (the writer),
    bin/msg (which files an exchange into both seats' timelines) and the
    session-end engine (which exports it to every spawn and reads the row count
    back to prove a write happened). Three literals agreeing is not one
    location; it is three locations that happen to collide. Move any one of
    them and the tools do not fail — they succeed against different files, and
    the timeline silently forks: mail written to one db, sessions to another,
    the engine watching a third for growth that is happening somewhere else.
    """
    return _derived(cfg, "JSTACK_TIMELINE_DIR", "timeline_dir", "Timeline",
                    guarded=True, base=logs_dir)


# ----------------------------------------------------------------- agents
#
# Six places in three repos used to disagree about what an agent is. This is
# the one definition: an agent is a directory directly under agents_dir() that
# either carries a CLAUDE.md itself, or has at least one immediate subdirectory
# that does — the second case being a seat, Agents/<id>/<seat>/CLAUDE.md.
# A directory with neither is not an agent; it is some other folder that
# happens to live there.


def _has_claude_md(d: Path) -> bool:
    return (d / "CLAUDE.md").is_file()


def is_agent(d: Path) -> bool:
    """CLAUDE.md at the top, or at least one non-dot seat subdir carrying one.

    Public because a caller that has already resolved its own agents dir needs
    to ask the question about a directory it names itself, without handing over
    a cfg that JSTACK_AGENTS_DIR could then outrank.
    """
    if _has_claude_md(d):
        return True
    try:
        return any(
            child.is_dir() and not child.name.startswith(".")
            and _has_claude_md(child)
            for child in d.iterdir()
        )
    except OSError:
        return False


def agents(cfg: "dict|None" = None) -> "list[str]":
    """Sorted ids of every agent under agents_dir(). Dot-entries are skipped.

    A missing agents_dir() answers [] — a machine mid-install is a normal
    state, not an error.
    """
    try:
        children = list(agents_dir(cfg).iterdir())
    except OSError:
        return []
    return sorted(
        d.name for d in children
        if d.is_dir() and not d.name.startswith(".") and is_agent(d)
    )


def _fold(name: str) -> str:
    """`work-ops`, `work_ops` and `workops` are one agent to anyone typing an
    id from memory; only the directory on disk knows which spelling it chose."""
    return name.lower().replace("-", "").replace("_", "")


def resolve_agent(agent_id: str, cfg: "dict|None" = None) -> "Path|None":
    """The workspace dir for `agent_id`, or None when no such agent exists.

    Matching, first hit wins: exact directory name, then case-insensitive,
    then with `-`/`_` folded away. When two on-disk names fold together the
    answer is deterministic — the exact-case hit wins outright, otherwise the
    first candidate in sort order.

    Never returns a path that does not exist. Joining {agent_root}/{agent_id}
    blind hands a nonexistent directory to a spawn, which fails later and
    further away than the typo that caused it; None lets the caller say so at
    the point of the mistake, naming the id that missed.
    """
    if not agent_id:
        return None
    ids = agents(cfg)
    if not ids:
        return None
    base = agents_dir(cfg)
    if agent_id in ids:
        return base / agent_id
    for candidates in (
        [i for i in ids if i.lower() == agent_id.lower()],
        [i for i in ids if _fold(i) == _fold(agent_id)],
    ):
        if candidates:
            return base / sorted(candidates)[0]
    return None


def seat_of(path, cfg: "dict|None" = None, base=None) -> "tuple[str|None, str|None]":
    """(agent, submode) for a directory inside an agent, else (None, None).

    submode is the rest of the path below the agent, "/"-joined and lowered —
    per-dir seats, so `social/chat` is its own seat, distinct from `chat` — or
    "chat" at the agent root itself.

    The gate is `agents()`'s definition of an agent, and that is the point.
    Three callers each carried their own copy of it, all gating on a CLAUDE.md
    at the agent's top level. On a machine whose agents each keep one that is
    invisible; on a machine laid out as Agents/<id>/<seat>/CLAUDE.md with
    nothing at the top, every one of them refused to name the seat the session
    was sitting in — mail could not say who sent it, and the tools read as
    simply not working there. One definition, asked once, cannot drift apart
    into three answers again.

    The two-value shim over `seat_at`, kept because several callers want
    exactly these two strings. Anything that also needs the directory, or the
    address to name this seat elsewhere, should ask `seat_at` and let the
    Seat derive them — see the drift its docstring describes.
    """
    seat = seat_at(path, cfg, base)
    return (None, None) if seat is None else (seat.agent, seat.submode)


def seats(agent_id: str, cfg: "dict|None" = None) -> "list[str]":
    """Sorted seat names for an agent that resolves — its immediate non-dot
    subdirectories carrying their own CLAUDE.md — else [].

    Immediate children only. A nested seat (`social/threads`) is not in this
    list; ask `resolve_seat` for those, which walks to any depth.
    """
    ws = resolve_agent(agent_id, cfg)
    if ws is None:
        return []
    try:
        children = list(ws.iterdir())
    except OSError:
        return []
    return sorted(
        c.name for c in children
        if c.is_dir() and not c.name.startswith(".") and _has_claude_md(c)
    )


# ------------------------------------------------------------- addressing
#
# One grammar for naming a seat, everywhere: `agent-seat`, hyphens walking
# down the seat tree, an agent alone meaning its cockpit.
#
#     alice                -> alice/chat        (the cockpit)
#     alice-social         -> alice/social/chat (descends into chat/)
#     alice-pm             -> alice/pm          (no pm/chat exists)
#     alice-service-call   -> alice/service-call
#     self                 -> the asking seat
#
# `resolve_agent`'s fold is the wrong tool here and must not be reached for:
# it reads `-` as a spelling variant (`work-ops` == `workops`), while this
# grammar reads it as structure. Both readings cannot be true of one string,
# and the fold wins by accident when a caller picks it — every hyphenated
# seat id resolves to None and the command answers "no such agent" about an
# agent that plainly exists. resolve_agent answers "which agent is this
# name"; resolve_seat answers "which seat does this address name". A command
# that takes an @-token wants the second.


class AddressError(ValueError):
    """An address that names no seat.

    Carries a message written for whoever typed it — the seats that do exist,
    or the agents that do. Callers present it their own way (a hook blocks
    with it, a CLI exits on it); raising rather than exiting is what lets the
    same resolver serve both without one of them dying inside a library.
    """


class Seat(NamedTuple):
    """A resolved seat, and every spelling of it derived from one resolution.

    Three spellings of one thing were being hand-built at their use sites —
    the hyphen id that addresses it, the slash form the timeline files it
    under, and the directory a session boots in. Built by hand they drift:
    a wake booked from `alice/social/threads` was labelled `alice-social`
    and booted one seat above the session that asked for it. Derived from a
    single resolution they cannot.
    """

    agent: str        #: lowercase base id, e.g. "alice"
    submode: str      #: seat below the agent, "/"-joined, e.g. "social/chat"
    path: Path        #: the seat directory a session boots in
    agent_dir: Path   #: the agent root, in its on-disk case, e.g. .../Alice

    @property
    def id(self) -> str:
        """The canonical address — `alice-social-chat`. Round-trips through
        `resolve_seat`. A bare cockpit (CLAUDE.md at the agent root, no chat/
        dir) is just the agent: there is no seat dir to name."""
        if self.path == self.agent_dir:
            return self.agent
        return f"{self.agent}-{self.submode.replace('/', '-')}"

    @property
    def timeline(self) -> str:
        """The timeline/log_event spelling — `alice/social/chat`."""
        return f"{self.agent}/{self.submode or 'chat'}"


def _agent_dirs(cfg: "dict|None" = None) -> "dict[str, Path]":
    """lowercase agent id -> its workspace dir, in on-disk case."""
    base = agents_dir(cfg)
    return {name.lower(): base / name for name in agents(cfg)}


def _seats_under(d: Path) -> "list[str]":
    try:
        return sorted(c.name for c in d.iterdir()
                      if c.is_dir() and not c.name.startswith(".")
                      and _has_claude_md(c))
    except OSError:
        return []


#: Directories that are never a seat and are never walked through to find one.
#: A pad is the seat's shared working room — `bin/msg` stashes attachments in
#: it and `/pict` writes into it — and what lands there is checkouts, which
#: carry CLAUDE.md files of their own. A CLAUDE.md is the evidence for a seat
#: everywhere else in this module, so without this the repo a session parked
#: in its pad this morning reads as a seat: addressable by mail that nobody
#: will ever read, and offered as a wake target that boots into a checkout.
_RESERVED_DIRS = frozenset({"pad"})


def _walk_segments(base: Path, segs: "list[str]") -> "str|None":
    """Resolve hyphen segments as a directory path under `base`.

    `service-call` resolves as the single dir `service-call` before it is
    tried as `service/call`: a hyphen inside a real directory name is far
    commoner than a nested pair, and trying the whole string first is what
    makes `alice-service-call` work without special-casing it. Recursion is
    what reaches a seat at any depth — `alice-social-threads-x.words` finds
    `social/threads-x.words` because each level retries the whole tail first.
    """
    if not segs:
        return ""
    whole = "-".join(segs)
    if whole not in _RESERVED_DIRS and _has_claude_md(base / whole):
        return whole
    for i in range(1, len(segs)):
        head = "-".join(segs[:i])
        sub = base / head
        if head in _RESERVED_DIRS or not sub.is_dir():
            continue
        tail = _walk_segments(sub, segs[i:])
        if tail is not None:
            return f"{head}/{tail}" if tail else head
    return None


def resolve_seat(spec: str, sender: "str|None" = None,
                 cfg: "dict|None" = None) -> Seat:
    """Resolve an address — `alice-social`, `@alice-social`, `self` — to a Seat.

    A leading `@` is optional, so a token typed either way resolves the same.
    `self`/`me` needs `sender` as the asking seat's `agent/submode`.

    Raises AddressError when nothing on disk answers, naming what does — an
    address that resolves to nowhere must fail where it was typed, never
    downstream where the reason is gone.
    """
    spec = (spec or "").strip()
    if spec.startswith("@"):
        spec = spec[1:]
    if not spec:
        raise AddressError("no seat named — use agent, agent-seat, or self")

    known = _agent_dirs(cfg)

    if spec.lower() in ("self", "me"):
        if not sender:
            raise AddressError("self: could not tell which seat is asking")
        agent, _, submode = sender.partition("/")
        agent, submode = agent.lower(), (submode or "chat")
        agent_dir = known.get(agent)
        if agent_dir is None:
            raise AddressError(
                f"self: {agent!r} is not an agent under {agents_dir(cfg)}")
        path = agent_dir / submode
        if submode == "chat" and not _has_claude_md(path) \
                and _has_claude_md(agent_dir):
            path = agent_dir  # bare cockpit: the seat is the agent root
        return Seat(agent, submode, path, agent_dir)

    parts = [p for p in spec.lower().split("-") if p]
    base = agent_dir = None
    # Longest agent-name match first: an agent whose own name holds a hyphen
    # must win over the same prefix read as agent + seat.
    for i in range(len(parts), 0, -1):
        cand = "-".join(parts[:i])
        if cand in known:
            base, agent_dir, parts = cand, known[cand], parts[i:]
            break
    if base is None:
        have = ", ".join(sorted(known)) or "(none found)"
        raise AddressError(f"unknown agent in {spec!r}. Known agents: {have}")

    submode = _walk_segments(agent_dir, parts)
    if submode is None:
        raise AddressError(
            f"{spec!r}: no seat {'-'.join(parts)!r} under {agent_dir}. "
            f"Seats there: {', '.join(_seats_under(agent_dir)) or '(none)'}")

    # The cockpit descent: a seat that itself holds a chat/ dir means the
    # operator seat, not the container. alice-social -> alice/social/chat.
    if not submode:
        if _has_claude_md(agent_dir / "chat"):
            submode = "chat"
        elif _has_claude_md(agent_dir):
            # A bare agent — CLAUDE.md at the top, no chat/ dir — keeps its
            # cockpit at the agent root itself. A session booted there files
            # as {agent}/chat, so that is the seat this address names;
            # {root}/chat would be a directory no session ever boots in.
            return Seat(base, "chat", agent_dir, agent_dir)
        else:
            submode = ""
    elif _has_claude_md(agent_dir / submode / "chat"):
        submode = f"{submode}/chat"

    if not submode:
        raise AddressError(f"{spec!r}: {agent_dir} has no chat/ seat")
    path = agent_dir / submode
    if not _has_claude_md(path):
        raise AddressError(
            f"{spec!r}: {path} is not a seat (no CLAUDE.md) — no session "
            f"boots there. Seats: "
            f"{', '.join(_seats_under(agent_dir)) or '(none)'}")
    return Seat(base, submode, path, agent_dir)


def seat_at(path, cfg: "dict|None" = None, base=None) -> "Seat|None":
    """The Seat a directory sits in, or None when it is not inside an agent.

    The reverse of `resolve_seat`: a session knows its cwd and needs the
    address for it. `seat_at(cwd).id` is the only correct way to name the
    seat that is asking — hand-building it from the first path segment names
    the seat above a nested one.
    """
    if path is None:
        return None
    # `base` lets a caller that already resolved the agents dir pass it
    # straight in, so its answer cannot differ from the one it just computed.
    base = agents_dir(cfg) if base is None else Path(base)
    try:
        rel = Path(path).resolve().relative_to(base.resolve())
    except (ValueError, OSError):
        return None
    # `is_agent` directly, not `resolve_agent`: the name here was read off the
    # path, so it needs no fuzzy matching, and routing through a lookup would
    # re-resolve the agents dir and could answer about a different one.
    if not rel.parts or not is_agent(base / rel.parts[0]):
        return None
    agent_dir = base / rel.parts[0]
    submode = "/".join(p.lower() for p in rel.parts[1:]) or "chat"
    return Seat(rel.parts[0].lower(), submode,
                agent_dir / "/".join(rel.parts[1:]) if rel.parts[1:]
                else agent_dir,
                agent_dir)


def enclosing_seat(path, cfg: "dict|None" = None) -> "Seat|None":
    """The nearest seat at or above `path`, or None outside the agent tree.

    A session's cwd is not always a seat: it may be standing in a scratch dir
    or a checkout below one. This walks up to the deepest directory that is a
    seat, which is the seat whose CLAUDE.md and path rules that session is
    actually running under.

    Deepest, not first: `alice/social/threads` carries its own CLAUDE.md, so a
    session there belongs to the threads seat and not to `social` above it.
    Stopping at the first path component below the agent was how a wake booked
    from the threads seat came back up in `social/` — a different seat, with
    different rules, answering for work it had never seen.
    """
    seat = seat_at(path, cfg)
    if seat is None:
        return None
    parts = [] if seat.path == seat.agent_dir else seat.submode.split("/")
    for depth in range(len(parts), 0, -1):
        # A checkout parked in a pad carries its own CLAUDE.md; the seat a
        # session standing in one belongs to is the seat above the pad.
        if any(p in _RESERVED_DIRS for p in parts[:depth]):
            continue
        here = seat.agent_dir / "/".join(parts[:depth])
        if _has_claude_md(here):
            return Seat(seat.agent, "/".join(parts[:depth]), here,
                        seat.agent_dir)
    # No seat below the agent: the agent dir is the seat, named as the cockpit
    # because that is how a session booted there files itself.
    return Seat(seat.agent, "chat", seat.agent_dir, seat.agent_dir)
