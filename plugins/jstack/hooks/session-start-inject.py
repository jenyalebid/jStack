#!/usr/bin/env python3
"""SessionStart hook — inject the seat's recent timeline history into new sessions.

A file on disk is not context in a session. This hook closes the loop on the
timeline (the single running memory): on session start it resolves the agent +
sub-mode from the session's cwd and injects everything that seat's last N
SESSIONS wrote as SessionStart additionalContext, so the session starts sighted
instead of cold. The WRITE half is the session-end engine (selfwrite/review)
and the Stop hook for auto sessions — both log via `log_event <agent/submode>
...`; the injection reads back through `log_event tail --sessions`.

The window unit is the session, not the entry: "the last ten times I sat in
this seat" is what a person resuming actually means, and it holds steady when
a session writes more than one entry (pipeline consolidations, multi-topic
days) — an entry-counted window would quietly shorten the recalled history
exactly then. Rows with no session_id count as one session each.

Injection fires ONLY into live, human-driven sessions — that is the point of
it: a person sitting down mid-history. Headless spawns (schedulers, watchers,
daemons running `claude --print`) never inject, for every seat, regardless of
config. The content law is the mirror image: only `origin=direct` entries
ride the injection (`log_event tail --origin direct`) — auto sessions
neither receive injections nor appear in them.

Which seats get injected, and how many SESSIONS deep, is host config — the
`timeline_inject` map in $JSTACK_REVIEW_CONFIG (~/.claude/jstack/review.json):

    "timeline_inject": {"*/*": 10, "alpha/chat": 20, "*/pm": 5}

Values are an int session count, or a dict carrying it as {"sessions": N} (or
legacy {"n": N}; extra keys ignored — pre-session-window configs read as
session counts, which is the same number for the common one-entry session and
strictly more history otherwise, never less). Match
is per-dir, nearest wins: at each depth exact "agent/seat" beats wildcard
"*/seat", then the seat's parent dir is tried ("alpha/social/chat" falls back
to "*/social"). Finally `*/*` is the fleet default, so a newly-created seat
inherits injection without another registration edit. With no default, seats
with no match get nothing. No config key → no injection anywhere (opt-in).

A session can be opened on a SUBJECT instead of its seat: `JSTACK_TIMELINE_TAG`
in the spawn's environment (a pinned tag on a spawner's board, or set by hand
from the desk) swaps the seat window for a tag window — the last N sittings
ANY seat had on that tag, seat named on each line. It replaces the seat
history rather than adding to it: opening a subject is not opening a seat, and
a block of both is neither. The session is tagged with it at that same instant,
so the work it does continues the thread instead of falling out of it. An
unknown tag falls back to the seat's history and says so in the injection.

Seat resolution (MUST match the engine's resolve_submode and the Stop hook):
the session dir's full path under {agent_root}/{Name}, "/"-joined — seats are
directories, and each dir is its own seat (chat ≠ social/chat ≠ social); empty
(cwd == the agent root) → "chat", the default cockpit mode. Cockpit sessions
run at the agent root — they are NOT required to cd into chat/, so the
project-dir key (transcripts + memory) is never disturbed. The tail a seat
injects is its own dir plus ancestor dirs, never siblings (log_event tail
semantics). The "review" sub-mode is skipped.

Defensive: any error → silent exit 0, empty output. A SessionStart hook must
never block or corrupt a session. Stdlib only, no host dependency.
Kill switch: JSTACK_TIMELINE_INJECT_DISABLED=1 (legacy
JSTACK_CONTINUITY_INJECT_DISABLED honored too).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_BIN = PLUGIN_ROOT / "bin"
sys.path.insert(0, str(PLUGIN_ROOT))
import repo_seat  # noqa: E402

try:
    import root as _root  # noqa: E402
except ImportError:
    # An older install runs this hook without root.py — a supported state. A
    # SessionStart hook that raises breaks every session start on the machine,
    # so this is the one place degrading quietly is right: fall back to the
    # literal pre-root default at the use site.
    _root = None


def _config() -> dict:
    cfg_path = Path(
        os.environ.get(
            "JSTACK_REVIEW_CONFIG",
            str(Path.home() / ".claude" / "jstack" / "review.json"),
        )
    ).expanduser()
    try:
        data = json.loads(cfg_path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def resolve(cwd: Path, root: Path) -> tuple[str | None, str | None]:
    """(agent, submode) for a workspace session, else (None, None).

    submode is the session dir's full path under {root}/{Name} ("/"-joined —
    per-dir seats: social/chat is its own seat, distinct from chat and from
    social), or "chat" at the agent root. Recognized when {Name} is an agent:
    a CLAUDE.md at its top, or a seat below it carrying one."""
    if _root is not None:
        return _root.seat_of(cwd, base=root)
    # Older install without root.py: the literal gate this shipped with.
    try:
        rel = cwd.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return None, None
    if not rel.parts:
        return None, None
    agent_dir = root / rel.parts[0]
    if not (agent_dir / "CLAUDE.md").is_file():
        return None, None
    submode = "/".join(p.lower() for p in rel.parts[1:]) or "chat"
    return agent_dir.name.lower(), submode


_NO_TTY = {"??", "?", "-", ""}

# An IDE drives its embedded agent from a window, not a terminal: nothing in
# the ancestry ever holds a tty, so the walk below would read a live human
# session as a headless spawn and inject nothing. An IDE ancestor is the
# window's equivalent of a controlling terminal.
_IDE_ANCESTORS = {n.strip() for n in (
    os.environ.get("JSTACK_IDE_ANCESTORS") or "Xcode").split(",") if n.strip()}


def _is_interactive() -> bool:
    """A human-driven session has a controlling terminal somewhere; headless
    spawns (schedulers, watchers, daemons running `claude --print`) sit in a
    launchd/init-rooted chain with none.

    The CLI spawns hooks detached from its controlling terminal, so opening
    /dev/tty inside a hook fails even when a human is driving — the terminal
    lives on the CLI process up the ancestry. Try /dev/tty (covers direct
    invocation), then walk parents via ps and count any ancestor holding a
    tty as interactive. An IDE ancestor counts too: an editor drives its
    embedded agent from a window and no process in that chain ever holds a
    tty, so a human sitting in Xcode would otherwise read as a daemon
    (JSTACK_IDE_ANCESTORS overrides the set).
    JSTACK_ASSUME_INTERACTIVE=1 short-circuits to True — for hosts (or tests)
    whose spawn shape defeats the ancestry walk."""
    if os.environ.get("JSTACK_ASSUME_INTERACTIVE"):
        return True
    try:
        with open("/dev/tty"):
            return True
    except OSError:
        pass
    pid = os.getppid()
    for _ in range(8):
        if pid <= 1:
            return False
        try:
            out = subprocess.run(
                ["ps", "-o", "tty=,ppid=,comm=", "-p", str(pid)],
                capture_output=True, text=True, timeout=3,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return False
        parts = out.split(None, 2)
        if len(parts) < 2:
            return False
        if parts[0] not in _NO_TTY:
            return True
        if len(parts) > 2 and Path(parts[2]).name in _IDE_ANCESTORS:
            return True
        try:
            pid = int(parts[1])
        except ValueError:
            return False
    return False


def inject_count(cfg: dict, agent: str, submode: str) -> int:
    """Sessions to inject for a seat — per-dir, nearest match wins.

    At each depth the exact "agent/seat" key beats "*/seat"; no hit → try the
    seat's parent dir. Values are an int, or a dict carrying the count as
    "sessions" (legacy "n" honored — same key, now read as sessions; extra
    keys ignored — whether to inject at all is the caller's live-session
    gate, not per-seat config)."""
    inject = cfg.get("timeline_inject")
    if not isinstance(inject, dict):
        return 0
    seat = submode
    while True:
        for key in (f"{agent}/{seat}", f"*/{seat}"):
            if key not in inject:
                continue
            val = inject[key]
            try:
                if isinstance(val, dict):
                    return int(val.get("sessions", val.get("n", 0)))
                return int(val)
            except (TypeError, ValueError):
                return 0
        if "/" not in seat:
            try:
                val = inject.get("*/*", 0)
                if isinstance(val, dict):
                    return int(val.get("sessions", val.get("n", 0)))
                return int(val)
            except (TypeError, ValueError):
                return 0
        seat = seat.rsplit("/", 1)[0]


def tail_argv(seat: str | None, n: int, as_json: bool = False,
              tag: str | None = None, binary: str | None = None) -> list:
    """The window read, as argv — THE definition of what an injection carries.

    Exported because `pict` previews this injection and must not re-derive it:
    a second spelling of the window (entries where this counts sessions, a
    dropped `--origin`, a missing `--tag`) is a preview that disagrees with
    the thing it claims to show, and the preview is what a person checks.
    `binary` is the caller's own log_event path — pict's test seam."""
    # --origin direct: the injection is a person sitting down mid-history —
    # it carries the seat's human-driven narrative only. Auto sessions
    # (origin=indirect: crons, publish wakes, spawned work) neither receive
    # injections (the live-session gate above) nor ride in them.
    #
    # A tag read passes NO seat: the subject is the window, and who worked it
    # is what the read is meant to cross. log_event names the seat on every
    # line in that mode, so the block still says who did what.
    cmd = [binary or str(PLUGIN_BIN / "log_event"), "tail"]
    if seat:
        cmd.append(seat)
    if tag:
        cmd += ["--tag", tag]
    cmd += ["--sessions", str(n), "--origin", "direct"]
    if as_json:
        cmd.append("--json")
    return cmd


def tail(seat: str | None, n: int, as_json: bool = False,
         tag: str | None = None) -> str:
    cmd = tail_argv(seat, n, as_json=as_json, tag=tag)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def pinned_tag() -> str:
    """The subject this session was opened on, or "".

    Set by whoever spawned the session — a pinned tag on a spawner's board,
    or `JSTACK_TIMELINE_TAG=<tag> claude` from the desk. It replaces the
    seat's own history with the subject's: the same seat opened on `payments`
    and on `social` is two different cockpits, and the point of opening one
    is not to read the other."""
    return os.environ.get("JSTACK_TIMELINE_TAG", "").strip().lower()


def tag_known(tag: str, binary: str | None = None) -> bool:
    """Is `tag` in the vocabulary? Unknown is NOT the same as empty — a typo'd
    pin must fall back to the seat's history and say so, not silently boot a
    session blind on a subject that does not exist. `binary` is the caller's
    own log_event path (pict's test seam)."""
    try:
        r = subprocess.run([binary or str(PLUGIN_BIN / "log_event"), "tag",
                            "list", "--json"], capture_output=True, text=True, timeout=8)
        if r.returncode != 0:
            return False
        return any(t.get("name") == tag for t in json.loads(r.stdout or "[]"))
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        return False


def carry_tag(tag: str, session_id: str) -> None:
    """Attach the pinned tag to this session, at its first instant.

    Without this the pin decays: a session opened on a subject writes its
    entries untagged, so the next session opened on the same pin cannot see
    what this one did. Idempotent (INSERT OR IGNORE), so a resume re-running
    the hook costs nothing. Never mints — an unknown tag was already refused
    above, and minting is a deliberate act."""
    if not session_id:
        return
    try:
        subprocess.run([str(PLUGIN_BIN / "log_event"), "tag", "set", tag,
                        "--session", session_id],
                       capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        pass


def explain(seat: str) -> dict:
    """What a live session of `seat` would be injected, as data.

    THE answer for any consumer that needs to show or check the injection
    (dashboard markers, audits, tests): ask this instead of re-deriving the
    window from the timeline db. The config walk and the origin law live in
    exactly one place — a second implementation is a second truth, and the
    one that isn't the injector is the one that goes stale and lies.

    Liveness is deliberately NOT part of the answer: this is the
    hypothetical "if a person sat down in this seat right now", which is
    what a marker in a UI means. Shape:

        {"ok": bool, "seat": str, "sessions": int, "ids": [int, ...]}

    `sessions` is the configured window depth (0 = this seat injects
    nothing); `ids` are the entry ids inside it — more than one per session
    when a session logged more than once.

    ok=False means the answer could not be computed (log_event unreachable
    or unparseable) — a caller must render that as unknown, never as "these
    rows don't inject".
    """
    agent, _, submode = seat.partition("/")
    agent = agent.lower()
    submode = submode.lower() or "chat"
    out = {"ok": True, "seat": f"{agent}/{submode}", "sessions": 0, "ids": []}
    if not agent or submode == "review":
        return out
    out["sessions"] = n = inject_count(_config(), agent, submode)
    if n <= 0:
        return out
    raw = tail(out["seat"], n, as_json=True)
    if not raw:
        # No entries and a failed call are indistinguishable in the text
        # tail, so json mode is asked for explicitly: empty output here
        # means the call itself produced nothing.
        out["ok"] = False
        return out
    try:
        rows = json.loads(raw)
        out["ids"] = [int(r["id"]) for r in rows if r.get("id") is not None]
    except (json.JSONDecodeError, ValueError, TypeError, KeyError):
        out["ok"] = False
    return out


def inbox_updates(seat: str) -> list:
    """This seat's unseen updates. Reading them consumes them — an update is
    delivered exactly once, which is what keeps a mailbox from becoming a
    backlog. bin/msg is the only reader of that truth."""
    try:
        r = subprocess.run([str(PLUGIN_BIN / "msg"), "updates-for", seat],
                           capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return []
    if not r.stdout.strip():
        return []
    try:
        rows = json.loads(r.stdout)
        return rows if isinstance(rows, list) else []
    except (json.JSONDecodeError, ValueError):
        return []


def build_inbox(seat: str, rows: list) -> str:
    """Updates another seat sent — news, shown once, then gone.

    Deliberately NOT a queue and deliberately not urgent. Nothing here is
    owed to anyone: a request that needed doing would have arrived as a task,
    in the session it was sent to, not as something to find later."""
    lines = [
        "<jstack-updates>",
        f"{len(rows)} note{'s' if len(rows) != 1 else ''} sent to {seat} since "
        "your last session. Context only — nobody is waiting on any of it, "
        "there is nothing to close, and you will not see it again. Act on one "
        "only if it changes what you are about to do.",
        "",
    ]
    for r in rows:
        head = f"[#{r['id']}] from {r['from_seat']} · {str(r['created_at']).replace('T', ' ')}"
        if r.get("reply_to"):
            head += f" · answering your #{r['reply_to']}"
        lines.append(head)
        lines.append(f"  {r['subject']}")
        if r.get("body"):
            body = str(r["body"]).strip().splitlines()
            lines.extend("  " + ln for ln in body[:4])
            if len(body) > 4:
                lines.append(f"  … msg read {r['id']}")
        for att in json.loads(r.get("attachments") or "[]"):
            lines.append(f"  attached: {att}")
        lines.append("")
    lines.append("</jstack-updates>")
    return "\n".join(lines)


def build_context(agent: str, submode: str, entries: str, n: int,
                  note: str = "") -> str:
    seat = f"{agent}/{submode}"
    head = (
        f"Injected on entry by jStack — everything {seat} (your seat) wrote across "
        f"its last {n} sessions, oldest first. This is your own recent history: "
        "you are not starting "
        "cold. Build on it — don't re-discover, re-propose, or re-litigate what's "
        "already below. A `↳ verdict:` line is the independent review's call on that "
        "run — if its note names a move to avoid, pick differently."
    )
    return (
        "<jstack-timeline>\n"
        + (f"{note}\n\n" if note else "")
        + f"{head}\n\n{entries}\n"
        "</jstack-timeline>"
    )


def build_tag_context(tag: str, seat: str, entries: str, n: int) -> str:
    """The subject cockpit — this session was opened ON something.

    Deliberately NOT the seat's history plus the subject's: a pinned session
    is a sitting with one subject, and the seat's other threads are noise
    against it. Whoever worked it is in the rows, because a subject is not
    one agent's — the seat here is only where the terminal happens to run."""
    return (
        "<jstack-timeline>\n"
        f"Injected on entry by jStack — this session is pinned to **{tag}**, so "
        f"what follows is the last {n} sittings ANY seat had on that subject, "
        f"oldest first, each line naming who worked it. It is not "
        f"{seat}'s own history: you are opening a subject, not a seat. Build on "
        "it — don't re-discover, re-propose, or re-litigate what's already below. "
        "Your own entries are tagged the same way automatically, so what you do "
        "here continues this thread. A `↳ verdict:` line is the independent "
        "review's call on that run.\n\n"
        f"{entries}\n"
        "</jstack-timeline>"
    )


def build_identity(agent: str, submode: str, root: Path, registry: dict) -> str:
    """The agent's role file, for a session that reached its seat through a repo.

    A cockpit session loads its role by standing in it — CLAUDE.md walk-up
    climbs from cwd to the workspace and picks it up. A session in a repo can't:
    the workspace is a sibling of the checkout, not an ancestor, so walk-up
    passes it by and the session gets the repo's docs and none of its own
    identity. Reading it in is the same answer walk-up would have given, applied
    to the layout the IDE forces.

    Ordered least-specific first, matching how walk-up stacks: shared protocol
    at the agent root, then the agent's own role file, then the seat's."""
    entry = registry.get(agent) or {}
    ws = entry.get("workspace")
    seat_dir = Path(str(ws)).expanduser() if ws else root / agent
    try:
        agent_dir = root / seat_dir.resolve().relative_to(root.resolve()).parts[0]
    except (ValueError, OSError, IndexError):
        agent_dir = seat_dir

    parts = []
    seen = set()
    for path in (root / "CLAUDE.md", agent_dir / "CLAUDE.md", seat_dir / "CLAUDE.md"):
        try:
            rp = path.resolve()
        except OSError:
            continue
        if rp in seen or not path.is_file():
            continue
        seen.add(rp)
        try:
            body = path.read_text().strip()
        except OSError:
            continue
        if body:
            parts.append(f"--- {path} ---\n{body}")
    if not parts:
        return ""

    body = "\n\n".join(parts)
    return (
        "<jstack-identity>\n"
        f"Injected on entry by jStack. Your working directory is a repo that "
        f"{agent} owns, so you are the {agent} agent working in it — the seat is "
        f"{agent}/{submode}. Your role files are below: CLAUDE.md walk-up climbs "
        "from the working directory and your workspace is a sibling of this "
        "checkout, not an ancestor, so it never reaches them. Read them as your "
        "own identity, the same as if you had started in the workspace. Where "
        "they describe your cockpit as the working directory, that part is the "
        "terminal shape — here the working directory is the code.\n\n"
        f"{body}\n"
        "</jstack-identity>"
    )


def main() -> int:
    # `--explain <agent/seat>` — machine mode, no stdin contract: prints the
    # injection answer for a seat as JSON (see explain()). Consumers that
    # display or verify injections call this; the kill switch below is a
    # session-time gate, not part of the answer.
    argv = sys.argv[1:]
    if argv and argv[0] == "--explain":
        seat = argv[1] if len(argv) > 1 else ""
        if not seat:
            print("--explain needs a seat (agent or agent/submode)",
                  file=sys.stderr)
            return 2
        print(json.dumps(explain(seat)))
        return 0

    if os.environ.get("JSTACK_TIMELINE_INJECT_DISABLED") or \
            os.environ.get("JSTACK_CONTINUITY_INJECT_DISABLED"):
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    cwd = payload.get("cwd") or os.getcwd()

    cfg = _config()
    root = (_root.agents_dir(cfg) if _root is not None
            else Path(cfg.get("agent_root") or "~/Agents").expanduser())
    agent, submode = resolve(Path(cwd), root)
    via_repo = False
    if agent is None:
        # Not a seat directory. It may still be a repo the registry says an
        # agent owns — an IDE fixes cwd to the checkout, so this is the only
        # way that session ever sees its own history.
        registry_path = repo_seat.registry_path_for(cfg, root)
        agent, submode = repo_seat.seat_for(cwd, root, registry_path)
        via_repo = agent is not None
    if agent is None or submode == "review":
        return 0

    # Both halves are for a person sitting down in the seat. A headless spawn
    # (cron, watcher, `claude --print`) gets neither: it was given its task,
    # and the seat's mail is not it. The one exception is a wake spawned FOR a
    # message — that carries the message inline in its own prompt, so it needs
    # no injection to see it.
    if not _is_interactive():
        return 0

    seat = f"{agent}/{submode}"
    blocks = []

    # Identity first, and only for a session that reached its seat through a
    # repo: CLAUDE.md walk-up already gave a cockpit session its role, but a
    # session standing in a checkout never passes its workspace.
    if via_repo and cfg.get("inject_identity", True):
        identity = build_identity(
            agent, submode, root, repo_seat.load_registry(registry_path))
        if identity:
            blocks.append(identity)

    # A pinned tag replaces the seat window with the subject's. The seat's
    # configured depth still sets it — but a seat that injects nothing still
    # gets history when a pin explicitly asked for a subject, so 0 floors at
    # the fleet default rather than making the pin a no-op.
    tag = pinned_tag()
    note = ""
    if tag and not tag_known(tag):
        # Refusing quietly would boot the session with no history at all and
        # nothing on screen explaining why. Say it, then fall back to the seat.
        note = (f"[This session was opened on tag '{tag}', which is not in the "
                "timeline vocabulary (`log_event tag list`). Falling back to "
                "this seat's own history.]")
        tag = ""

    n = inject_count(cfg, agent, submode)
    if tag:
        depth = n if n > 0 else 10
        carry_tag(tag, str(payload.get("session_id") or "").strip())
        # Always emitted, even empty: a session that does not know it is
        # pinned frames its work as the seat's, and the first sitting on a
        # fresh subject is exactly when that framing matters.
        entries = tail(None, depth, tag=tag) or \
            "(nothing recorded on this subject yet — this is its first sitting.)"
        blocks.append(build_tag_context(tag, seat, entries, depth))
    elif n > 0:
        entries = tail(seat, n)
        if entries:
            blocks.append(build_context(agent, submode, entries, n, note=note))
            note = ""
    if note:
        # The fallback had nothing to say either — the bad pin is still the
        # most useful thing this session can be told.
        blocks.insert(0, f"<jstack-timeline>\n{note}\n</jstack-timeline>")

    # Updates last, and only if consuming them succeeded. They are news from
    # other seats, not work — so they sit below this seat's own history rather
    # than above it, and they are shown exactly once.
    if not os.environ.get("JSTACK_INBOX_INJECT_DISABLED"):
        rows = inbox_updates(seat)
        if rows:
            blocks.append(build_inbox(seat, rows))

    if not blocks:
        return 0

    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": "\n\n".join(blocks),
                }
            }
        )
    )
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:  # never block a session
        raise SystemExit(0)
