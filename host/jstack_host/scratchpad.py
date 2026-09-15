"""The pad — a seat's one shared folder.

`{instance root}/{Name}/{seat}/pad` is a single directory that the user and
every session of that seat both read and write. It is a file channel, not an output dump: the
user parks things in it for the agent, the agent parks things in it for them, and
either can use it as working room — a temporary checkout, a build, a rendering
that only matters until it's been looked at.

There is one per seat and it is not divided. No folder per session, no separate
inbox for share-sheet drops: a session is a conversation, not a place, and a
file the user dropped this morning has to be in the same folder as the file an
agent writes this afternoon or the channel doesn't work. The harness disagrees
by default — it hands each session a private directory in the system temp dir —
so `assistant/hooks/pad_link.py` replaces that directory with a symlink to the
pad at session start, which makes the path in a session's system prompt and the
shared folder the same place.

Ownership is asymmetric, and it is the reason a pad can be trusted as a
channel. The user can delete anything in it. An agent may only clean up what an
agent put there: a file that arrived from the phone is marked (`OWNER_ATTR`) and
is theirs, so a sweep leaves it alone. Nothing an agent does can quietly
remove what they parked for it to find.

**One directory at a time.** A listing reads exactly the directory it was asked
for and never descends. The pad is also a workbench — checkouts, build trees
and dependency trees land in it, and those run to hundreds of thousands of
files — so a walk that flattens the tree cannot be made safe by naming the
directories to skip: it only takes one nobody thought to name. A folder is a
row that opens, which costs one `scandir` whether it holds two files or a
million.

Addressing is one shape everywhere: a path relative to the pad, where the
empty string is the pad itself. That rel names a file to fetch or delete and a
folder to open, so the pane's whole vocabulary is a single string.
"""

import ctypes
import ctypes.util
import mimetypes
import os
import re
import shutil
import time
from pathlib import Path

from .hostenv import _NON_MODE_DIRS, _PRUNED_TREES, instance_root, workspace

# Generous — screenshots are megabytes, screen recordings tens of them. The
# cap exists so a runaway client can't fill the disk, not to police use.
MAX_UPLOAD = 200 * 1024 * 1024

# One directory can still be pathological on its own (a build tree's object
# dir). Listing stops here and says so — a pane that silently shows the first
# slice reads as "this is everything", which is the lie this module exists
# to stop telling.
MAX_ROWS = 2000

# The seat's one shared folder, relative to the workspace.
PAD = "pad"

# Set on anything that arrives from the phone. An agent's cleanup skips these:
# they are the user's, parked for the agent to find, and a channel where the
# other side can delete your messages is not a channel. Rides with the file on
# move and copy; absence just means "an agent's", which is the safe default
# for a file nobody claimed.
OWNER_ATTR = "com.jremote.pad.owner"

# mimetypes leaves gaps the app would feel (macOS Python has no .md entry);
# pin the ones agents actually drop so the phone picks the right viewer.
_EXTRA_MIME = {
    ".md": "text/markdown",
    ".log": "text/plain",
    ".heic": "image/heic",
}


def _projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def _cwd_slug(cwd: str) -> str:
    """A cwd encoded the way the harness names its per-seat dirs —
    `/` and `.` both become `-` (`/Users/x/.claude` → `-Users-x--claude`)."""
    return re.sub(r"[/.]", "-", cwd)


def agent_pad(agent_id: str) -> Path:
    """The seat's pad, reached from an agent id — what the pane uses before a
    thread has spawned anything. A seat is a place and exists before its first
    session does.

    Raises KeyError for an agent whose base isn't in agents.json.
    """
    return workspace(agent_id) / PAD


def _path_holds(seat: Path, where: Path) -> bool:
    """Is `where` the seat itself, or somewhere inside it? Both resolved."""
    return where == seat or seat in where.parents


def _slug_holds(seat_slug: str, dirname: str) -> bool:
    """The same question asked of two harness project-dir names.

    A project dir is `_cwd_slug(cwd)`, and the slug is not reversible — `/`
    and `.` both become `-`, so `…-chat-pad` could spell a `pad` inside the
    `chat` seat or a seat literally named `chat-pad`. Prefix is therefore an
    answer, not the answer; `_deepest` is what makes it safe, because a real
    `chat-pad` seat produces the longer slug and wins outright.
    """
    return dirname == seat_slug or dirname.startswith(seat_slug + "-")


def _deepest(seats: "list[Path]", holds) -> "Path|None":
    """The longest-pathed seat `holds` says contains the session, or None.

    Longest, because seats nest: `Ada/social/threads` and `Ada/social` both
    contain a session working under threads, and the one whose CLAUDE.md that
    session is actually running under is the deeper of the two.
    """
    winner = None
    for ws in seats:
        if holds(ws) and (winner is None or len(str(ws)) > len(str(winner))):
            winner = ws
    return winner


def session_pad(sid: str) -> Path:
    """The same pad, reached from a session id.

    Two ways in, because neither covers a session's whole life on its own.

    The transcript's project dir is named for the seat's working directory, so
    it places any session the machine has ever run — live or long closed. But
    Claude Code writes no transcript until a turn lands, and a session spawned
    from the phone is attachable, drop-able-on and visible in the Files pane
    from its first instant. Resolving only through the transcript made every
    pad route 404 in exactly that window: the terminal was live and typing,
    and attaching a photo answered "unknown session". So a sid the transcript
    can't place is asked of tmux instead — the live pane's own cwd is the same
    answer, earlier. (`pty.py` fixed this same transcript-existence assumption
    on the close path; this was the other half of it.)

    BOTH WAYS IN ASK WHICH SEAT CONTAINS THE SESSION, NOT WHICH SEAT IT IS.
    They used to demand an exact match, and a session's working directory is
    not a fixed point: `cd` into the seat's own pad, into a checkout parked
    there, into a worktree, and the harness carries that as the cwd for the
    rest of the session — it names the project dir, and it is what the live
    pane reports. None of those directories is a seat (`_NOT_A_SEAT` prunes
    the pad from the walk on purpose), so an equality test placed the session
    nowhere and every pad route on it answered "unknown session" while its
    terminal sat there live. 95e01508 spent a morning like that. Containment
    is the same question the rest of the tree already asks — `root
    .enclosing_seat` for addresses, `agent_of` for the compaction switch —
    and the deepest containing seat wins, because seats nest.

    Raises KeyError for a session neither can place — the caller maps that to
    a 404, which now means "no such session" rather than "not yet".
    """
    seats = _seat_dirs()
    projects = _projects_dir()
    if projects.exists():
        for d in projects.iterdir():
            if d.is_dir() and (d / f"{sid}.jsonl").exists():
                if ws := _deepest(seats, lambda w: _slug_holds(_cwd_slug(str(w)), d.name)):
                    return ws / PAD
    from . import open_path     # local: open_path imports managed
    try:
        live = Path(open_path.pane_cwd(sid)).resolve()
    except (KeyError, OSError):
        raise KeyError(sid) from None
    if ws := _deepest(seats, lambda w: _path_holds(w.resolve(), live)):
        return ws / PAD
    raise KeyError(sid)


# Directory names a walk stops at rather than descends into. A seat's own
# `pad`, `git` and `scratch` are its contents, not modes — and a build tree
# under one is neither. The roster already knows both sets; borrowing them
# keeps one answer to "what is a mode dir" and keeps the walk cheap on a root
# as wide as a home directory.
_NOT_A_SEAT = _NON_MODE_DIRS | _PRUNED_TREES


def _seat_dirs() -> list[Path]:
    """Candidate workspaces under the instance root, three deep — modes nest
    (Ada/social/chat) and nothing below a seat is one.

    The root is the host's, not this Mac's. `~/Agents` is only the default; a
    machine that keeps its seats anywhere else (`JREMOTE_INSTANCE_ROOT`, which
    the roster already reads) had every session-addressed pad route answer
    "unknown session" — the transcript placed the session fine, and then no
    seat in this list could match it, because the list was empty.
    """
    root = instance_root()
    out: list[Path] = []
    frontier = [root]
    for _ in range(3):
        deeper: list[Path] = []
        for parent in frontier:
            try:
                entries = sorted(os.scandir(parent), key=lambda e: e.name)
            except OSError:
                continue        # unreadable — not every dir under a wide root is ours
            for e in entries:
                if e.name.startswith(".") or e.name in _NOT_A_SEAT:
                    continue
                try:
                    if not e.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                out.append(Path(e.path))
                deeper.append(Path(e.path))
        frontier = deeper
    return out


def _mime(p: Path) -> str:
    if m := _EXTRA_MIME.get(p.suffix.lower()):
        return m
    return mimetypes.guess_type(p.name)[0] or "application/octet-stream"


# macOS keeps extended attributes behind libc rather than the `os` module
# (`os.getxattr` is Linux-only), and its calls carry two extra arguments —
# position and options — that the Linux ones don't.
_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_libc.getxattr.restype = ctypes.c_ssize_t
_libc.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                           ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
_libc.setxattr.restype = ctypes.c_int
_libc.setxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                           ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]


def is_boss(p: Path) -> bool:
    """Did this file arrive from the phone? An unreadable or absent attr reads
    as no — an agent sweeping its own output is the ordinary case, and failing
    closed on every file would make cleanup impossible instead of safe."""
    buf = ctypes.create_string_buffer(16)
    n = _libc.getxattr(str(p).encode(), OWNER_ATTR.encode(), buf, len(buf), 0, 0)
    return n > 0 and buf.raw[:n] == b"boss"


def _mark_boss(p: Path) -> None:
    # A filesystem without xattrs costs the mark, not the file.
    _libc.setxattr(str(p).encode(), OWNER_ATTR.encode(), b"boss", 4, 0, 0)


def _resolve(pad: Path, rel: str) -> Path:
    """A pad-relative rel → the real path it names, file or directory.

    The pad is the boundary: a rel that climbs out of it with `..` raises
    FileNotFoundError and the caller maps that to 404. Existence is the
    caller's to check, so a listing and a fetch can each demand the kind they
    need.
    """
    root = pad.resolve()
    p = (root / rel).resolve() if rel else root
    if not p.is_relative_to(root):
        raise FileNotFoundError(rel)
    return p


def _file_row(p: Path, rel: str) -> dict:
    st = p.stat()
    return {"name": p.name, "rel": rel, "size": st.st_size,
            "mtime": int(st.st_mtime), "mime": _mime(p), "path": str(p),
            "boss": is_boss(p)}


def list_dir(pad: Path, rel: str = "") -> dict:
    """One directory of the pad, read and not descended into.

    An empty rel is the pad itself. Folders come back separate from files,
    each newest first, so a pane can render them as the thing you open rather
    than the thing you preview.

    A pad that doesn't exist yet lists empty rather than raising: a seat has
    one whether or not anything has been put in it, and an error screen on an
    untouched seat reads as a broken pane. Anything else that isn't a
    directory raises FileNotFoundError.
    """
    d = _resolve(pad, rel)
    if not d.is_dir():
        if not rel and not d.exists():
            return {"path": "", "dirs": [], "files": [], "truncated": False}
        raise FileNotFoundError(rel)

    dirs, files, truncated = [], [], False
    for entry in os.scandir(d):
        if entry.name.startswith("."):
            continue
        if len(dirs) + len(files) >= MAX_ROWS:
            truncated = True
            break
        child = f"{rel}/{entry.name}" if rel else entry.name
        try:
            if entry.is_dir(follow_symlinks=False):
                dirs.append({"name": entry.name, "rel": child,
                             "mtime": int(entry.stat().st_mtime)})
            elif entry.is_file(follow_symlinks=False):
                files.append(_file_row(Path(entry.path), child))
        except OSError:
            continue        # vanished mid-scan — a pad is live
    dirs.sort(key=lambda r: r["mtime"], reverse=True)
    files.sort(key=lambda r: r["mtime"], reverse=True)
    return {"path": rel, "dirs": dirs, "files": files, "truncated": truncated}


def list_files(sid: str) -> list[dict]:
    """The flat top of a session's pad — the legacy shape, kept for the
    dashboard's own Files view. Folders are reached through `list_dir`."""
    return list_dir(session_pad(sid))["files"]


def file_path(pad: Path, rel: str) -> Path:
    """Resolve a client-supplied rel to a real file inside the pad. Anything
    that isn't a plain file there — a directory, a traversal, a name that
    doesn't exist — raises FileNotFoundError."""
    p = _resolve(pad, rel)
    if not p.is_file():
        raise FileNotFoundError(rel)
    return p


def delete(pad: Path, rel: str) -> None:
    """Remove exactly the one file the rel names. The user's side of the channel:
    they can delete anything in their own folder."""
    file_path(pad, rel).unlink()


def clear_dir(pad: Path, rel: str = "") -> int:
    """Empty one directory (the directory itself stays); return how many
    top-level entries went.

    Scoped to the folder the user is standing in and looking at, so the button can
    never take more than the screen showed — and it takes all of it, including
    what they parked, because this is the user clearing their own folder.
    """
    d = _resolve(pad, rel)
    if not d.is_dir():
        raise FileNotFoundError(rel)
    n = 0
    for p in d.iterdir():
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()
        n += 1
    return n


def sweep(pad: Path, rel: str = "") -> tuple[int, int]:
    """An agent tidying up after itself: remove what an agent put here, leave
    what the user did. Returns (removed, kept).

    This is the only removal an agent is entitled to. A file from the phone
    was parked for the agent to find, and a channel whose other side can
    delete your messages is not a channel — so a marked file survives a sweep
    even when it sits inside a folder an agent made.
    """
    d = _resolve(pad, rel)
    if not d.is_dir():
        raise FileNotFoundError(rel)
    removed = kept = 0
    for p in sorted(d.iterdir()):
        if p.is_dir() and not p.is_symlink():
            r, k = sweep(pad, str(p.relative_to(pad.resolve())))
            removed += r
            kept += k
            if k == 0:
                p.rmdir()
        elif is_boss(p):
            kept += 1
        else:
            p.unlink()
            removed += 1
    return removed, kept


_SAFE_CHARS = re.compile(r"[^\w.\-]+")


def _sanitize(filename: str) -> str:
    """A client-supplied filename reduced to something safe to create —
    basename only, non-filename characters collapsed, length capped."""
    name = Path(filename or "").name
    name = _SAFE_CHARS.sub("-", name).strip("-.")
    return name[-80:] if name else "file"


def save(pad: Path, rel: str, filename: str, data: bytes) -> Path:
    """Write a phone-dropped file into the directory the pane is showing;
    return its path.

    Names stay plain — the pane shows them back and the user should recognize what
    they stashed. A collision gets a numeric suffix before the extension rather
    than overwriting what's there. The file is marked theirs, so an agent's sweep
    will leave it.
    """
    d = _resolve(pad, rel)
    d.mkdir(parents=True, exist_ok=True)
    name = _sanitize(filename)
    target = d / name
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 2
    while target.exists():
        target = d / f"{stem}-{n}{suffix}"
        n += 1
    target.write_bytes(data)
    _mark_boss(target)
    return target


def write_back(pad: Path, rel: str, data: bytes) -> Path:
    """Replace a file already in the pad with an edited copy of itself.

    The other side of opening one: the phone fetched these bytes, the user marked
    them up, and they land back on the name they came from rather than beside
    it. That is why this refuses a rel that names nothing — a save-back to a
    file someone deleted meanwhile would silently become a new file, and the
    edit belongs to the original or to no one. `save` is the path for
    something arriving that wasn't here before.

    The result is the user's, whoever wrote the original: they edited it, so an agent's
    sweep steps over it from now on.
    """
    p = file_path(pad, rel)
    p.write_bytes(data)
    _mark_boss(p)
    return p


def save_drop(agent_id: str, filename: str, data: bytes) -> Path:
    """A share-sheet drop with no session behind it. It lands in the seat's
    pad like everything else — the same folder the agent is already looking
    at — stamped so the pad reads as a timeline and a same-name drop never
    collides.

    Raises KeyError for an agent whose base isn't in agents.json — the caller
    maps that to 404.
    """
    pad = agent_pad(agent_id)
    pad.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = _sanitize(filename)
    target = pad / f"{stamp}-{name}"
    n = 2
    while target.exists():
        target = pad / f"{stamp}-{n}-{name}"
        n += 1
    target.write_bytes(data)
    _mark_boss(target)
    return target
