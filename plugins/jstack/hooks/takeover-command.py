#!/usr/bin/env python3
"""UserPromptSubmit — `/takeover` hands this session's work to a fresh one.

The third way out of a session, and the only one that does not trust it.

    /handoff    the session distills ITSELF into a brief. Whatever it got
                wrong, it gets wrong again in the doc — a summary written by
                the session that made the mistake carries the mistake forward
                with the session's own confidence attached.
    /splitoff   a verbatim dub, resumed. Lossless and therefore inheriting:
                the copy starts holding every conclusion the original reached,
                including the wrong ones, and its compaction damage too.
    /takeover   neither. The new session is handed a POINTER to the source
                transcript and a mandate to read it itself. No distillation to
                trust, no context to inherit — it forms its own read of what
                happened, checks the claims against the tree, and continues.

Which is why this is a hook and not a skill. Every step is fixed: the
transcript path comes from the payload, the workspace from the agent registry,
the briefing from a template that never varies. Nothing here is a judgement, so
nothing here needs a model — and routing it through one would put the outgoing
session's voice back into a payload whose whole purpose is to exclude it.

Grammar:

    /takeover                     this workspace, no focus
    /takeover <focus...>          this workspace, scoped to <focus>
    /takeover @agent              that agent's cockpit
    /takeover @agent-<seat>       that agent's named seat
    /takeover @agent <focus...>   that agent's cockpit, scoped

The address is the one grammar mail and the scheduler share: an agent alone is
its cockpit, hyphens walk down the seat tree (`@alice-social-threads`), and a
seat holding its own `chat/` resolves there. A slash is refused with the
hyphen spelling rather than guessed at.

The source session is untouched and keeps running — a takeover is a second
pair of eyes arriving, not a handover the source has to survive. Closing it, if
that is what is wanted, stays the user's call in the window that owns it.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(PLUGIN_ROOT))
from _answer import block    # noqa: E402 — sibling modules, path set above
from _prompts import load as load_prompt  # noqa: E402

try:
    import root as _root  # noqa: E402
except ImportError:      # an install predating root.py — @agent is unavailable
    _root = None

#: Overridable so the test can stand a fake in its place rather than opening
#: windows on whatever desktop happens to be running the suite.
TERMINAL = os.environ.get("JSTACK_TERMINAL_BIN") or "open-terminal-here"

#: The adapter probe is a usage print and the open is an osascript round-trip
#: or a managed-spawn CLI. Both are seconds; the ceiling makes a hang read as
#: a hang instead of a silently dead command.
TIMEOUT = 30

# `/takeover`, `/jstack:takeover`, or the stub's sentinel — then the rest.
TRIGGER = re.compile(r"^\s*(?:/(?:jstack:)?takeover|JSTACK_TAKEOVER_CMD)\b[ \t]*(.*)$",
                     re.IGNORECASE | re.DOTALL)

# The briefing template lives at prompts/takeover-briefing.md — fixed text
# by design: the one thing a takeover must not contain is the outgoing
# session's account of itself, and a file the model never writes cannot
# smuggle one in.


def payload_or_exit() -> dict:
    try:
        return json.load(sys.stdin)
    except Exception:
        sys.exit(0)


def seat_label(cwd: str) -> str:
    """`agent/submode` for a workspace dir, else the basename. Never blank —
    the briefing names where the work came from, and "" names nowhere.

    `enclosing_seat`, not `seat_of`: the two answer differently for exactly the
    directories a session wanders into. See `source_home`.
    """
    if _root is not None and hasattr(_root, "enclosing_seat"):
        seat = _root.enclosing_seat(cwd)
        if seat is not None:
            return seat.timeline
    if _root is not None:
        agent, submode = _root.seat_of(cwd)
        if agent:
            return f"{agent}/{submode}"
    return Path(cwd).name or cwd


def source_home(cwd: str) -> Path:
    """The seat directory a takeover of `cwd` should open in.

    NOT `cwd` itself, and that was the bug. The payload reports where the
    source session's shell is STANDING, which is not where the session
    belongs: a `cd` into the seat's pad, a checkout parked in it, a build tree
    — the harness carries that as the session's cwd from then on, and a
    takeover typed an hour later inherited it. 95e01508 is what that produces.
    Its source sat in the seat, ran `cd pad` an hour in, and the takeover
    opened a fresh session in `<seat>/pad` — a directory that is
    deliberately not a seat anywhere in this module (`_RESERVED_DIRS`), so the
    new session had no seat to be addressed by: the host answered "unknown
    session" to every pad-addressed route on it, and it read whatever CLAUDE.md
    the checkout in that pad happened to carry instead of the seat's own.

    `enclosing_seat` is the walk-up that already exists for this, and the
    `@agent` path has always gone through its sibling `resolve_seat`. This is
    the no-@ path finally asking the same question.

    Outside the agent tree the cwd stands: a session working in a project
    checkout has no seat to be normalised to, and refusing to take it over is
    a worse answer than opening where it is.
    """
    if _root is not None and hasattr(_root, "enclosing_seat"):
        seat = _root.enclosing_seat(cwd)
        if seat is not None:
            return seat.path
    return Path(cwd)


def target_cwd(token: str) -> "tuple[Path, str]":
    """(workspace, agent display name) for an `@agent-seat` token.

    One grammar, `root.resolve_seat`, shared with mail and the scheduler:
    hyphens walk down the seat tree and an agent alone means its cockpit.
    Deterministic by construction, because a hook has no judgement to apply —
    where handoff reads the focus to pick a sub-mode, which is a model's call
    and not available here.
    """
    if _root is None:
        block("/takeover: this jStack install has no agent resolver "
              "(root.py missing) — drop the @agent and take over in place")
    if not hasattr(_root, "resolve_seat"):
        block("/takeover: this jStack install predates shared seat addressing "
              "— update the plugin, or drop the @agent and take over in place")

    # A slash was this command's own spelling for one release and never any
    # other command's. Say so, rather than reporting the whole token as an
    # unknown agent and leaving the user to guess which half was wrong.
    if "/" in token:
        block(f"/takeover: seats are addressed with a hyphen, not a slash — "
              f"try @{token.replace('/', '-')}")
    try:
        seat = _root.resolve_seat(token)
    except _root.AddressError as exc:
        block(f"/takeover: {exc}")
    return seat.path, seat.agent_dir.name


def adapter_supports_first_prompt() -> bool:
    """Does the `open-terminal-here` first in PATH take `--first-prompt`?

    Asked, not assumed. The adapter is overridable by design — this machine
    replaces it to route spawns through a managed host — so an install can
    easily carry a new hook against an older adapter. An adapter that does not
    parse the flag passes it through to `claude`, which dies on an unknown
    option, and the takeover window opens and closes before anyone reads it.
    """
    try:
        probe = subprocess.run([TERMINAL], capture_output=True, text=True,
                               timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "--first-prompt" in (probe.stdout + probe.stderr)


def stage(text: str) -> str:
    """Write the briefing outside every workspace and return its path.

    A one-shot payload must not land in a tree somebody commits. The adapter
    reads it into a variable and deletes it before `claude` starts, so on the
    normal path nothing lingers even here.
    """
    fd, path = tempfile.mkstemp(prefix="jstack-takeover-", suffix=".md")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    return path


def title_for(agent: str, focus: str, source_seat: str) -> str:
    """`TO · <topic>` / `TO→<Agent> · <topic>`, matching handoff's `HF ·`.

    The topic is the focus as typed, clipped — there is no model here to name
    the work in three words, and an invented title would be the one piece of
    this payload that nobody wrote."""
    words = " ".join(focus.split()[:5])
    if len(words) > 40:
        words = words[:39].rstrip() + "…"
    topic = words or source_seat
    return f"TO→{agent} · {topic}" if agent else f"TO · {topic}"


def main() -> None:
    payload = payload_or_exit()
    match = TRIGGER.match(payload.get("prompt") or "")
    if not match:
        sys.exit(0)          # not ours — every other prompt passes untouched

    # The harness hands every hook the file it is writing to. Reconstructing it
    # from the session id and the cwd is a guess about path encoding, and a
    # takeover built on a guess reviews the wrong conversation.
    transcript = (payload.get("transcript_path") or "").strip()
    if not transcript:
        block("/takeover: no transcript path in the hook payload — "
              "there is nothing for the new session to read")
    if not Path(transcript).is_file():
        block(f"/takeover: no transcript on disk yet — {transcript}")

    # Where the source session BELONGS, which is not always where its shell is
    # standing — `source_home`. Resolved once: the briefing names it and, on
    # the no-@ path, the new window opens in it.
    source_cwd = str(source_home(str(payload.get("cwd") or Path.cwd())))
    sid = (payload.get("session_id") or Path(transcript).stem).strip()

    rest = " ".join((match.group(1) or "").split())
    agent = ""
    if rest.startswith("@"):
        token, _, rest = rest.partition(" ")
        cwd, agent = target_cwd(token[1:])
    else:
        cwd = Path(source_cwd)
    focus = rest.strip()

    if not cwd.is_dir():
        block(f"/takeover: target workspace does not exist — {cwd}")

    source_seat = seat_label(source_cwd)
    if focus:
        focus_line = focus
        continue_line = (
            f"Your scope is: **{focus}**\n\n"
            "That is an explicit narrowing from the person who opened you. Work it, "
            "and leave the session's other threads alone unless one of them blocks "
            "this. If the source session never got to this, say so plainly and start "
            "it — a takeover is allowed to find that the answer is not in there.")
    else:
        focus_line = "(none — the session's live thread)"
        continue_line = (
            "No focus was given: pick up the thread that was live in the last "
            "exchanges and carry it forward. If the session ended mid-step, finish "
            "the step.")

    briefing = load_prompt("takeover-briefing.md").format(
        sid=sid, source_seat=source_seat, source_cwd=source_cwd,
        transcript=transcript, focus_line=focus_line, continue_line=continue_line)
    brief_path = stage(briefing)

    title = title_for(agent, focus, source_seat)
    kick = ("Take over the session named in your briefing: read it from the "
            "transcript, verify what it claims against the tree, then continue"
            + (f" — focus: {focus}." if focus else "."))

    if not shutil.which(TERMINAL):
        block(f"/takeover: no terminal adapter on PATH ({TERMINAL}).\n"
              f"  briefing staged at {brief_path}\n"
              f"  cd {cwd} && claude --append-system-prompt \"$(cat {brief_path})\"")

    cmd = [TERMINAL, str(cwd), "--prompt-file", brief_path, "--name", title]
    autostart = adapter_supports_first_prompt()
    if autostart:
        cmd += ["--first-prompt", kick]

    try:
        opened = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=TIMEOUT).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        opened = False

    where = f"@{agent.lower()} · {cwd}" if agent else str(cwd)
    if not opened:
        block(f"/takeover: the terminal would not open — nothing was spawned.\n"
              f"  briefing staged at {brief_path}\n"
              f"  cd {cwd} && claude --append-system-prompt \"$(cat {brief_path})\"")

    lines = [f"takeover → {title}", f"  {where}",
             f"  reading this session from source: {transcript}"]
    if not autostart:
        lines.append("  adapter takes no --first-prompt — the window opened with the "
                     "briefing loaded but did NOT start; type anything to kick it off")
        lines.append(f"  briefing left at {brief_path}")
    lines.append("  this session is unchanged and still yours")
    block("\n".join(lines))


if __name__ == "__main__":
    main()
