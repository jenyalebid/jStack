"""Seats — every directory a session can boot in, and the browse that finds one.

**A seat is a directory holding a AGENTS.md.** That is not a new rule invented
here: `bin/msg` resolves `@ada-code-go` by it, the session-start injector
recognises an agent by it, and the review engine decides what it reviews by it.
Reading the same marker is the whole point — a second definition of "seat" is a
second thing to keep in step, and the two would disagree on the day it mattered.

The weaker twin is `submode_dirs()`, which lists direct children against a name
blocklist. It cannot see a nested seat at all, and its blocklist prunes
`missions/` — which on this Mac holds two real ones. It stays where it is for
the roster's `sub_modes` field; nothing new should be built on it.

Two callers:

* the app's **directory picker** — `browse()` walks one directory at a time
  from the agent's own root, telling each row whether it is a seat, so a card
  can only ever be minted onto somewhere a session can actually start. This is
  also why no endpoint hands out a catalogue of every seat: discovery is a
  browse, so the wire never carries a list nobody is looking at.
* **stats for the carded seats** — `resolve()` maps the ids in the `shortcuts`
  table back to real paths, and `project_dir_name()` encodes those the way
  Claude Code names its transcript dir. Always this direction: the encoding
  flattens `/` and `.` to the same `-`, so `social/threads-ada.words` and
  `social/threads/ada/words` come back as one indistinguishable string.
  Decoding a project dir into a seat guesses; encoding a seat is exact.

One directory at a time, never the tree, for the same reason `PadFolderView`
reads one folder: a seat is also a workbench, and a checkout dropped in one
runs to hundreds of thousands of files.
"""

from pathlib import Path

from .hostenv import split_id, workspace

# Directories under a seat that are never themselves a seat, and why:
#
#   pad, git   the Law reserves both — `git/` is what the seat saves, `pad/`
#              is the shared shelf. Neither is spawnable, and `pad/` is where
#              the only AGENTS.md false positives on this Mac live
#              (`Ops/chat/pad/issue-6`), because work under a pad carries
#              its own instructions.
#   scratch    volatile by contract.
#
# `missions`, `memory`, `active` and `concepts` are deliberately NOT here.
# They are how an agent organises its own work, and if one holds a AGENTS.md
# then it IS a seat — `Ada/missions/200-dau` already is one.
_RESERVED = {"pad", "git", "scratch"}

# Machine trees: no seat lives inside one and walking them is unbounded.
_MACHINE = {"node_modules", "__pycache__", ".venv", "venv", "build",
            "DerivedData", ".build", "dist", ".git"}


def is_seat(path: Path) -> bool:
    """The stack's own test, verbatim: a directory holding a AGENTS.md."""
    return any((path / _n).is_file() for _n in ("AGENTS.md", "CLAUDE.md"))


def _listable(child: Path) -> bool:
    name = child.name
    return (child.is_dir() and not name.startswith(".")
            and name not in _RESERVED and name not in _MACHINE)


def project_dir_name(path: Path | str) -> str:
    """A working directory → the name Claude Code gives its transcript dir.

    `/` and `.` both become `-`. Lossy in reverse, which is why nothing here
    reverses it.
    """
    return str(path).replace("/", "-").replace(".", "-")


def seat_id(base: str, rel: str) -> str:
    """`("ada", "code/go")` → `ada-code-go` — the id every spawn path
    already takes (`workspace()` resolves it back through `split_id`). An
    empty `rel` is the agent's own root seat."""
    return f"{base}-{rel.replace('/', '-')}" if rel else base


def slug(base: str, rel: str) -> str:
    """`("ada", "code/wordy")` → `ada/code/wordy` — where the seat stands,
    read from the agent root down.

    The other spelling of the same pair `seat_id()` encodes, and the readable
    one: `/` stays `/`, so nothing about it is ambiguous the way a seat id is.
    It exists because a card on Home has no section header above it saying
    whose seat it is — the slug is the card's own answer to that.
    """
    return f"{base}/{rel}" if rel else base


def agent_root(agent_id: str) -> Path:
    """The agent's umbrella directory — where a picker always starts, whatever
    seat the caller happens to be addressing."""
    base, _ = split_id(agent_id)
    return workspace(base)


# The walk is a `AGENTS.md` stat per directory, so it costs whatever is checked
# out inside a seat, not what the seat count suggests: 2393 directories and
# ~120ms across the eight agents here, and the widest tree alone is half of it.
#
# Two things keep that off the hot path, in this order of importance. First,
# `resolve()` walks only the agents that have a card, so nobody pays for a tree
# they never opened. Second, the result is held on a TTL — the tree changes when
# The user makes a directory, which is not a thing that happens between two polls —
# and the create path clears it (`invalidate()`) so a new seat appears at once
# rather than in thirty seconds.
#
# The cache is the smaller half of that, and it is worth saying why: the first
# cut of this cached the walk and then shipped all 51 seats on the fifteen-second
# poll anyway. Caching the cheap end of a payload nobody needed is not an
# optimisation. `carded_seats()` in board.py is where that got fixed.
_TTL = 30.0
_cache: dict[str, tuple[float, list[str]]] = {}


def invalidate(base: str | None = None) -> None:
    """Forget the cached walk — for a new seat to show up now, not in 30s."""
    _cache.pop(base, None) if base else _cache.clear()


def walk(base: str) -> list[str]:
    """Every seat under an agent, as `/`-joined paths relative to its root.

    The root itself is `""` when it carries a AGENTS.md. Reserved and machine
    subtrees are pruned whole — a seat cannot be inside one.
    """
    import time
    hit = _cache.get(base)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    out = _walk_uncached(base)
    _cache[base] = (time.time(), out)
    return out


def _walk_uncached(base: str) -> list[str]:
    try:
        root = workspace(base)
    except KeyError:
        return []
    out: list[str] = []
    if is_seat(root):
        out.append("")

    def descend(d: Path, rel: str) -> None:
        try:
            children = sorted(d.iterdir())
        except OSError:
            return
        for child in children:
            if not _listable(child):
                continue
            sub = f"{rel}/{child.name}" if rel else child.name
            if is_seat(child):
                out.append(sub)
            descend(child, sub)

    descend(root, "")
    return out


def resolve(seat_ids, bases) -> dict[str, tuple[str, str]]:
    """`{seat_id: (base, rel)}` for the ids that name a seat that exists.

    Built by encoding, never decoding. `walk()` produces the real relative
    paths and `seat_id()` names them, so this map is exact where `split_id()`
    can only guess: `social/threads-ada.words` and `social/threads/ada/words`
    encode to one id, and only the walk knows which is on disk.

    Only the agents actually named get walked — a host with fifty seats and
    five cards pays for the five. An id with no entry in the result names a
    directory that is gone; the caller says so rather than drawing a live card
    onto nothing.
    """
    wanted = set(seat_ids)
    if not wanted:
        return {}
    out: dict[str, tuple[str, str]] = {}
    for base in bases:
        if not any(s == base or s.startswith(base + "-") for s in wanted):
            continue
        for rel in walk(base):
            sid = seat_id(base, rel)
            if sid in wanted:
                out[sid] = (base, rel)
    return out


def browse(agent_id: str, rel: str = "") -> dict:
    """One directory of an agent's tree, for the picker.

    `rel` is relative to the agent's root and is confined to it — a `..` or an
    absolute path names nothing rather than escaping, the same posture the
    scratchpad routes take. Raises KeyError for an unknown agent (the caller
    turns that into a 404) and ValueError for a path outside the root.

    Each row says whether it is a seat and whether it has anything inside, so
    the app can grey out what cannot be picked and still let it be entered.
    """
    root = agent_root(agent_id).resolve()
    here = (root / rel).resolve() if rel else root
    if here != root and root not in here.parents:
        raise ValueError(f"{rel!r} escapes {root}")
    if not here.is_dir():
        raise KeyError(f"no directory {rel!r} under {root}")

    base, _ = split_id(agent_id)
    entries = []
    try:
        children = sorted(here.iterdir())
    except OSError:
        children = []
    for child in children:
        if not _listable(child):
            continue
        sub = f"{rel}/{child.name}" if rel else child.name
        entries.append({
            "name": child.name,
            "path": sub,
            "seat_id": seat_id(base, sub),
            "is_seat": is_seat(child),
            "has_children": any(_listable(c) for c in _safe_iter(child)),
        })
    return {
        "agent_id": agent_id,
        "base": base,
        "path": rel,
        "root": str(root),
        # The directory standing here — pickable when it carries a AGENTS.md,
        # which is also true of the agent root itself.
        "is_seat": is_seat(here),
        "seat_id": seat_id(base, rel),
        "parent": rel.rsplit("/", 1)[0] if "/" in rel else ("" if rel else None),
        "entries": entries,
    }


def _safe_iter(d: Path):
    try:
        return list(d.iterdir())
    except OSError:
        return []
