"""Stop hook: a session takes its own compaction boundary, at the seam it declared.

THE SPEC. Two modes, and they are not the same thing:

  * COMPACT MID-RUN — the session is working, `context_ceiling` tells it the session has
    got heavy, it picks a good spot to stop: commits, pushes, ends the turn, and closes
    its final message with `CONTINUE_MARK` on its own line. That marker is the request.
    This hook compacts at that seam and hands the session its work back. Unconditional —
    a session that asked to be resumed gets resumed.

  * COMPACT ON DELIVERY — the turn ended and the work is DONE: delivered, committed,
    nothing waiting. No marker. Nothing resumes here and nothing compacts either, unless
    the user switched that agent's **Compact When Done** on (Agents tab → agent →
    sliders, stored by `agent_prefs`) — and even then the boundary is taken WITHOUT a
    resume, because there is nothing to resume. Off is the default, and off means this
    hook does not exist for a finished turn.

SILENCE MEANS DONE. An earlier design read silence as "not finished" and compacted every
heavy turn-end by default — and since sessions almost never wrote a done-marker (two
declarations against a hundred Stops in the decision log), every finished delivery
ghost-compacted in the user's face, ate two and a half minutes of `Compacting
conversation`, and then spoke to itself off the continue nudge. A mechanism whose default
fires on finished work tramples the very switch that was supposed to govern it.

The marker is on the OTHER side now because that is the side a session can actually be
relied on to write. A session parking work mid-flight is doing so BECAUSE it just read
the ceiling injection — the instruction is at most a few tool calls old, and writing the
marker is part of obeying it. "Append a marker to every answer you give while heavy" is
what the old design demanded, and what it produced was a finished session answering one
small question, compacted for it, then nudged into reporting that its work was done.

The cost of a forgotten marker is a missed seam, and the CLI's own auto-compact backs
that up — mid-task, ugly, but the work continues. The cost the OLD default paid was a
compaction nobody asked for, over the only copy of a finished conversation. That
asymmetry is the whole design.

WHY A MARKER AND NOT A GUESS. Whether the work is finished is the one question the
decision cannot work out for itself. The harness's task store has an opinion about 10
transcripts out of 2,215; closing prose does not separate "Standing by." from "Looking at
that transcript." Both were tried, both reverted. So the session declares it — and
exact-match on the CLOSING LINE only, because a substring test declared its own author
finished when the report explaining the marker quoted it mid-sentence.

THE CLOSING MESSAGE DOES NOT EXIST YET WHEN STOP FIRES. The CLI writes the turn's own
final assistant line AFTER the Stop hooks return — measured at 0.7s on one session: the
hook read, decided and logged at 15:16:12.0, and the message it was judging landed at
15:16:12.716. So the parent literally cannot read the declaration it is gating on, and no
settle inside the parent can fix that: it may be the very thing the CLI is waiting out.
The read lives in the DETACHED CHILD instead, which outlives the hook, watches for the
first assistant text to land after the Stop-time offset — that line is the turn's own
closing message, unambiguously — and falls back to the newest text on file if nothing
lands. The answer is read ONCE: the child's one read is handed to the gate, the log and
the continue, because two reads of an asynchronously landing line once answered two
different ways and produced compacted-AND-abandoned.

THE CUT IS THE LOAD METER'S, NOT THE CLI'S. `compaction.HEAVY` — 160k, measured, the same
number the app and the dashboard chip draw. Auto-compact is a backstop and reaching it is
already a failure. This deliberately does NOT derive a threshold from the CLI's own
auto-compact window: a running session resolves that window once at startup and never
re-reads the setting, so the hook believed a 267k trigger while the CLI was still using
167k and stayed silent through a delivery showing 4% left.

WHETHER, not just when. The cut decides when to look; `compaction.recoverable` decides
whether it is worth doing. A session sitting on a large floor gets little back from a
compaction — what fills its window is overhead a summary cannot drop.

BUT NEITHER OF THOSE IS A STATEMENT ABOUT THE WORK. The marker fuses two claims, "compact
me here" and "I am not finished — hand me back", and every gate in this file once answered
only the first: a declared seam that failed a weight test was dropped entirely. Not
compacted AND not resumed, which is strictly worse than the heavy case, because the heavy
one at least gets its prompt. One session parked at 146,143 — 13,857 under the cut — closed
with `Next Move: Task #6`, and sat dead with four open task rows until the user found it
himself. So a session that declares is ALWAYS handed back; the weight now only chooses HOW
(`decide`, `continue_in_place`).

A NEW TURN IS NEW WORDS, NOT NEW BYTES. The CLI appends its own `stop_hook_summary` and
`turn_duration` system lines right after the Stop hooks return, so "has the file grown
since Stop" is true on EVERY delivery a beat after the child starts watching — whichever of
the two got there first won, and roughly half of all legitimate compactions died as phantom
`superseded`. Each deferred boundary then fired at the worst possible moment instead: the
next Stop, which is the user mid-conversation. Supersession is judged by reading what
landed after the offset — only a real user or assistant line is a turn.

THE BOUNDARY IS A SEAM, NOT AN ENDING — SO SOMETHING HAS TO STEP ACROSS IT. A mid-run
boundary with nothing after it is a session parked for a day at a summary whose closing
line was "Continue... Resume directly": that instruction is inert, the CLI only moves on a
prompt. So after the boundary lands, the child hands the session back its own docket — but
only on the marked path, never in the user's voice (the nudge names its own origin), and
never over him (a real turn after the boundary means he got there first).

The landing is read off the boundary line, NOT off the reading falling. A compaction writes
no assistant turn, so the per-message reading still reports the pre-compact weight until
the session next replies. A continue gated on the number would never fire, and a second
`/compact` gated on it would always fire — `freshly_compacted` reads the boundary for the
same reason.

INTENTIONAL, NOT SPRAYED. The send is `tmux send-keys` because that is the only mechanism
that exists — but keys typed at a terminal is exactly the failure mode this has to be
trusted not to cause, so every way it could land somewhere unintended is closed before it
fires:

  * addressed, never focused — `-t jr-<sid>` names this session's own tmux session, so a
    different window being in front is not a thing that can happen;
  * never into typed text — the composer must be empty. A half-written message when the
    turn ends is the real collision, and a prompt row with anything after it aborts;
  * never into a pane that is busy — an empty box is not an idle CLI. The permission
    footer stays up the whole time a turn runs and the whole time a compaction runs, so
    "box empty + footer present" was true of a session in the middle of both. Keys sent
    there do not run: they QUEUE, and sit above the composer armed to fire on the next
    Enter — somebody else's next Enter;
  * never mid-turn — if a real turn landed since Stop, this decision is stale. Abort; the
    next Stop reconsiders from scratch;
  * never twice — a boundary newer than the last assistant line means one already landed;
  * never left behind — after the Enter, the box is checked. Typing a slash command opens
    the CLI's autocomplete, and no send is guaranteed to take, so anything of ours still
    sitting there is cleared back out. A hook that types cleans up after itself;
  * never forced — everything here gives up silently. A missed seam costs headroom and the
    CLI's backstop catches it. A misplaced `/compact` costs a person's message.

Only managed sessions run in tmux. One without a managed session is left alone.

IT SAYS WHAT IT DID, EVERY TIME. Every path above ends in silence, which is correct for a
hook that types into a terminal and wrong for one anybody has to trust. One line per Stop,
one per child outcome, in `compact-delivery.jsonl` beside the rest of this host's state.
Cheap, pruned by age, and never able to fail the hook it is recording.

BOTH ENGINES, ONE DECISION. Every rule above is about a session's weight, its declaration
and its pane; none of it is about which CLI drew the pane. What differs is the grammar —
the prompt glyph, the footer that proves a live TUI, the shape of a boundary line in the
rollout — so the grammar is a table (`ENGINES`, `_GRAMMAR`) and the decision is written
once. Codex is the reason `pane_idle` can take its answer from the transcript: its idle
marker on screen is the empty-composer placeholder, which vanishes the moment anything is
typed, so on that engine "is it working" is a question only the rollout can answer.
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import agent_prefs, codex_transcript, compaction, context_ceiling, hostenv, managed

POLL_SECS = 0.5
MAX_WAIT_SECS = 20  # somebody typing for longer than this: leave it, the next Stop retries

#: The same wait, for a session that DECLARED a seam — and it is a different number for the
#: reason `STALE_LOCK_SECS` gives below: "the next Stop retries" is what makes twenty
#: seconds safe, and it is false here. A parked session ends no further turn, so the Stop
#: that would reconsider never fires; whatever this branch drops is dropped for good.
#:
#: IT WAS DROPPING THE PROMISE. Every `busy` the decision log has ever carried — 2026-09-24
#: 01:39 (244ca668), 09-25 08:05 (27df58cc), 09-25 19:07 (fe02ed56), 09-25 21:53
#: (2dec5785) — sits directly under a `resume: true` / `decision: compact` line. Four for
#: four: the pane was not ready for twenty seconds, the child walked away, and the session
#: it had just agreed to carry sat at its seam with nothing coming. The last of them was
#: rescued by hand, `/compact` then `continue` typed into the pane three minutes later.
#:
#: Twenty seconds is not a long time for a pane to be unready: a queued send into a working
#: pane has been measured at 4m25s (see `_is_sent_command`). So a declared seam gets the
#: fifteen minutes the outside stall sweep waits before it looks, minus a minute — this
#: child is finished and its lock released before that sweep's window opens, rather than
#: contending with it. Only the patience is new: the same guards decide every send,
#: `turn_moved` still ends it the moment the person drives, `keepalive` holds the lock.
SEAM_WAIT_SECS = 840

#: One lock PER SESSION, and the per-session part is load-bearing. The invariant is
#: narrower than the machine: `run` must not have two children half-deciding for ONE
#: session, because the compaction and the continue that steps across it are a single
#: decision. Nothing about a SECOND session needs serializing — every send is addressed
#: `-t jr-<sid>` and gated on that pane's own readiness, so two children typing into two
#: different panes is not a collision that exists.
#:
#: Machine-wide, it silently dropped work. A holder lives up to BOUNDARY_WAIT_SECS while
#: `keepalive` keeps its mtime fresh, so any OTHER session's Stop landing inside that
#: window failed `flock` and the child returned having recorded nothing at all — the one
#: path in a file that makes a point of saying what it did, every time. Two sessions lost
#: a seam to it three seconds apart: parked with the marker as the closing line, every
#: other gate passing, and a dangling `candidate` with no successor line as the only trace.
#:
#: And that loss is not the cheap kind this file elsewhere accepts. "The next Stop
#: reconsiders" is what makes every other give-up-silently branch safe, and it is FALSE
#: here: the marker means the session PARKED, so there is no next turn and no next Stop.
STALE_LOCK_SECS = 120

#: The lock this process holds, set by `held` so `keepalive` can touch it without every
#: waiter in the file having to carry a sid it otherwise has no use for. A child holds
#: exactly one lock for its whole life, so one slot is the honest shape.
_HELD_AT = None

#: How long to watch the box after an Enter before deciding the send did not take.
SUBMIT_CHECK_SECS = 3.0

#: How long to wait for our own `/compact` to finish, measured from the last sign of life
#: rather than from the send. Measured compactions run to ~145s on a 224k session, and the
#: child is detached, so this is generous on purpose: giving up here costs the continue,
#: and the session parks exactly as it did before.
#:
#: AS A FLAT BUDGET FROM THE SEND IT WAS THE WRONG CLOCK. One session's send went in at
#: 12:17:38, the CLI wrote the command's caveat at 12:18:25 — and then the compaction did
#: not actually run until 12:21:18, finishing at 12:23:02. One number had to cover the wait
#: to START plus the 104s to finish; the start alone ate 220s of it, the child logged
#: `sent/no-boundary` at 12:22:39, and the boundary landed 23 seconds later with nothing
#: left alive to step across it.
BOUNDARY_WAIT_SECS = 300

#: The absolute cap on a child's life, however much the CLI keeps proving it is still
#: working. The restart above is what makes the budget mean "since the last sign of life",
#: and a restart with no ceiling is a process that never exits.
MAX_BOUNDARY_WAIT_SECS = 900

#: The harness's own task store, one dir per session. Read-only here, and LOGGED, NOT
#: OBEYED — it is written only by a session that used the task tools (10 transcripts out of
#: 2,215 on the machine this was measured on), so its absence asserts nothing. It stays in
#: the decision log because the answer is free, and a disagreement between it and the
#: declaration is the first place to look when a session compacted, or did not, and should
#: not have.
TASKS_DIR = Path.home() / ".claude/tasks"
OPEN_STATUS = {"pending", "in_progress"}

#: One line per Stop and one per child outcome, beside the host's other state. Deliberately
#: not the session event feed: that file is the app's timeline, and a boundary decision is
#: not something to tell anybody about on their phone.
LOG_NAME = "compact-delivery.jsonl"
LOG_RETAIN_DAYS = 30

#: The switch on the app's agent settings page, stored host-side by `agent_prefs`. Off for
#: an agent nobody switched on, and off for a seat that resolves to no agent at all. It is
#: asked exactly once: on a turn that ended over the heavy cut WITHOUT asking to be resumed
#: — a finished delivery. On, it takes the boundary there, without a resume.
WHEN_DONE = "compact_when_done"

#: How a session asks to be compacted and resumed: the last line of its final message,
#: alone on that line. An HTML comment, chosen because it is exact-match (no turn of phrase
#: trips it by accident) and because a markdown renderer that hides it costs nothing to
#: read. `context_ceiling` teaches it in the same injection that tells a heavy session to
#: end the turn, which is why the one session that needs to write it — the one parking work
#: mid-flight because of that injection — can be relied on to.
#:
#: IT MUST BE THE LAST LINE, NOT MERELY PRESENT. A substring test was tried first and it
#: declared its own author finished: the report explaining the marker quoted it
#: mid-sentence. `_declares` reads the closing line and nothing else.
CONTINUE_MARK = "<!-- to-be-continued -->"

#: How long the CHILD waits for the turn's own closing message to reach the transcript.
#: Generous relative to the observed sub-second landing, cheap because the child is off the
#: Stop path entirely.
SETTLE_SECS = 10.0
SETTLE_POLL = 0.1

#: The nudge, and the reason it reads like this. It arrives as a user turn, so it must never
#: be mistakable for a person: it says who sent it in its first four words. It points at the
#: summary rather than restating any task, because the summary is already written and a
#: second description of the work is a second chance to get it wrong. It is only ever sent
#: to a session that ASKED for it — the marker is what it is answering — so it can say so
#: plainly. And it keeps an exit: a session whose summary reads as finished must be able to
#: say that in a line instead of manufacturing work to justify the prompt.
CONTINUE = (
    "[compact-on-delivery hook, not the user] You ended your last turn asking to be "
    "continued, so your context was compacted at the seam you picked — nobody has said "
    "anything new. Pick up where the summary leaves off and keep going. If nothing is "
    "actually left, say so in a line and stop rather than inventing work."
)

#: The same nudge for a session that parked but did not need a boundary.
#:
#: It cannot be the text above. That one says "your context was compacted at the seam you
#: picked", and on this path nothing was compacted — a session told it lost context it still
#: has will go looking for what it thinks it dropped, which is the re-discovery waste the
#: whole mechanism exists to reduce. Saying so plainly is also the useful half: everything
#: the session had is still in front of it, so there is nothing to reconstruct.
CONTINUE_IN_PLACE = (
    "[compact-on-delivery hook, not the user] You ended your last turn asking to be "
    "continued. Your context had room, so nothing was compacted — everything you had is "
    "still here — and nobody has said anything new. Pick up where you left off and keep "
    "going. If nothing is actually left, say so in a line and stop rather than inventing "
    "work."
)

#: Appended to the nudge ONLY when this session's task store actually has an open row.
#:
#: The two halves of surviving a boundary were built without knowing about each other. The
#: summary is a reconstruction — lossy by nature — and the carry checklist spends its budget
#: re-describing work that a session using the task tools has already written down in
#: structured form. Those rows are not in the context window at all: they are files under
#: `~/.claude/tasks/<sid>/`, so a compaction cannot touch them. They are the one part of the
#: plan that crosses the seam at full fidelity, and until now nothing told the session on
#: the far side that they were still there.
#:
#: Conditional because an unconditional pointer is a lie most of the time — 12 of 42 session
#: dirs held any rows at all — and the child already knows which kind this is: it computes
#: `docket(sid)` for the decision log either way, so the answer costs nothing. Pointing a
#: session at an empty store would teach it to distrust the whole nudge.
DOCKET_LINE = (
    " Your task list came through the boundary untouched — it lives on disk, not in the "
    "summary — so read those rows first and work from them."
)


# --- where this host keeps things -------------------------------------------------------

def lock_dir():
    """Where the per-session locks go.

    `hostenv.state_dir()` and not a literal, and the difference has bitten: a process that
    resolves the default profile answers `~/.local/state/jremote`, which on a machine whose
    host serves state from somewhere else is a directory nothing reads. Sixty-six ownerless
    zero-byte locks accumulated in one before anybody looked.

    Overridable only so the suite can take its locks somewhere disposable — the child is a
    separate process, so no `setattr` reaches it and an env var is the only handle. The host
    never sets it.
    """
    return Path(os.environ.get("JSTACK_COMPACT_LOCK_DIR") or hostenv.state_dir())


def log_path():
    return Path(os.environ.get("JSTACK_COMPACT_LOG")
                or hostenv.state_dir() / LOG_NAME)


# --- the two engines' grammar ------------------------------------------------------------

#: Everything about reading a pane that differs between CLIs.
#:
#: `footer` is a string only the live TUI draws, in every mode it can be in — proof that a
#: CLI, and not a launching shell or a dialog, owns this pane. For claude it was one
#: literal ("bypass permissions on") until shift+tab proved that is one keystroke away from
#: false: the CLI redraws the footer per mode, and from that keystroke on `pane_is_ready`
#: answered False for the rest of the session's life. Two declared seams on one session
#: logged `busy` twenty seconds later against a pane sitting idle. The labels and not the
#: `(shift+tab to cycle)` chrome after them: a narrow pane truncates the footer mid-phrase
#: and the label is the part that survives.
#:
#: MANUAL MODE DRAWS A FOOTER. This list said it did not — that the CLI dropped the banner
#: for "? for shortcuts" there — and so every mode but that one was enumerated. On
#: 2026-09-25 a sweep of the seven live panes on this Mac through `why_not_ready` answered
#: `no-footer` for a session parked at an empty box, nine minutes idle, whose last line read
#:
#:     ⏸ manual mode on · ← for agents
#:
#: One keystroke puts a session there and nothing takes it out, so that session could not
#: have been compacted at any seam for as long as it lived — the identical shape to the
#: shift+tab bug above, left by the same assumption that the enumeration was complete. It
#: is worth saying what found it: not a fixture and not a transcript, but the new
#: diagnostic run against the real panes. A bare `busy` never could have.
#:
#: SO THE MODES ARE NO LONGER ENUMERATED. Twice now the list has been one short, and the
#: CLI builds these strings rather than storing them whole — grepping its binary for
#: "<word> mode on" returns auto, plan, and three unrelated features, so no list read off
#: it can be trusted complete. `[a-z]+ mode on` takes any of them, present or future, and
#: the two footers that do not use the word ("bypass permissions on", "accept edits on")
#: stay named. The claim this pattern has to support is only "a live TUI is drawing here";
#: which mode it names was never a readiness fact, and the enumeration was precision this
#: check had no use for and could not keep.
#:
#: `working` is the CLI's own spinner line — a word, an ellipsis, then a parenthesised
#: payload. The elapsed time is deliberately NOT part of it: the CLI drops the timer
#: between segments of a long turn and on a narrow pane, so a pattern asking for digits
#: read a WORKING pane as idle and a message held for an idle pane queued. It stays this
#: tight, though: the FINISHED line of the same turn reads "Sautéed for 3m 35s" — elapsed
#: time, no ellipsis, no parentheses — and anything loose enough to catch that would make
#: this hook silent forever. So does the truncated permission footer, whose ellipsis
#: FOLLOWS its parenthesis.
#:
#: `busy` is a pane that is not ready even with an empty box. A compaction is the dangerous
#: one: it writes NOTHING to the transcript until it lands, so the "has a turn landed"
#: guard is blind for its whole two and a half minutes, and the footer stays up throughout.
#: Keys sent into it queue instead of running — "Press up to edit queued messages" is the
#: box saying it is holding something for later, and a queue fires on whoever presses Enter
#: next.
#:
#: `screen_idle` is False for codex, and it gets no `footer` or `working` pattern at all,
#: because it has nothing stable to match. Measured on a real pane (v0.156.1, capture in
#: `tests/fixtures/codex-pane-idle.txt`): the only idle marker on screen is the dim
#: empty-composer placeholder, which is gone the moment anything is typed, and the row that
#: does survive typing is a status line of the model name, the cwd and a warning count —
#: none of which is a string this can be pinned to. So for such an engine the two facts are
#: taken where they actually live: a composer row on screen proves a live TUI is drawing
#: this pane (a launching shell has none, and a dialog replaces it with numbered options),
#: and the rollout's own `task_started` / `task_complete` events say whether it is working.
#: Claude's footer survives typing, so there the screen answers both.
ENGINES = {
    "claude": {
        "prompt": "❯",
        "footer": re.compile(
            r"(?:bypass permissions|accept edits) on\b|\b[a-z]+ mode on\b"
            r"|\? for shortcuts"),
        "working": re.compile(r"…\s*\("),
        "busy": ("Compacting conversation", "Press up to edit queued messages"),
        "placeholder": (),
        "screen_idle": True,
        "boxed": True,
    },
    "codex": {
        "prompt": "›",
        # None: this engine gets no footer test. See `ENGINES` and `pane_idle`.
        "footer": None,
        "working": None,
        "busy": ("Compacting conversation",),
        # On screen, never typed: an empty codex box draws its own invitation.
        "placeholder": ("Ask Codex to do anything",),
        "screen_idle": False,
        "boxed": False,
    },
}


def grammar(engine):
    return ENGINES.get(engine, ENGINES["claude"])


def engine_of(path, row=None):
    """Which CLI wrote this transcript.

    The file first, the registry second. `context_ceiling.engine_of` reads the rollout's
    own shape, which is the fact; the registry row is a record of what the host spawned and
    predates any hand-started session.
    """
    try:
        found = context_ceiling.engine_of(path)
    except Exception:
        found = None
    if found in ENGINES:
        return found
    return (row or {}).get("engine") or "claude"


# --- reading the transcript --------------------------------------------------------------

def is_boundary(entry, engine):
    """True for the one line a compaction writes when it lands."""
    if engine == "codex":
        return entry.get("type") == "compacted"
    return entry.get("type") == "system" and entry.get("subtype") == "compact_boundary"


def _is_compaction_artifact(entry):
    """True for the turns `/compact` writes about itself.

    A compaction lands four user-role lines that no one typed — the summary, the caveat, the
    echoed command, its stdout. Counting any of them as a person speaking would make the
    continue abort on its own compaction, every time.
    """
    if entry.get("isCompactSummary") or entry.get("isMeta"):
        return True
    content = (entry.get("message") or {}).get("content")
    return isinstance(content, str) and content.lstrip().startswith(
        ("<command-name>", "<command-message>", "<local-command-stdout>",
         "<local-command-caveat>"))


def _is_sent_command(entry, engine):
    """True for the `/compact` WE typed, landing as its own turn.

    THIS IS THE ONLY SIGN OF LIFE A COMPACTION EVER GIVES IN BAND, and it was the one line
    not counted. `_is_compaction_artifact` names four rows — summary, caveat, echoed
    command, stdout — and the client writes ALL FOUR only after the boundary: read
    b2e6524a's transcript in file order and 748/749 carry 00:20:32 timestamps while sitting
    below the 00:22:49 boundary line. So `signs` could never rise during a run, the restart
    in `wait_and_continue` never fired, and BOUNDARY_WAIT_SECS was a flat budget from the
    send no matter what its comment said.

    The submitted command is different: it is written the moment the box takes it, which is
    exactly the event the budget wanted to start from. On 2026-09-24 that send sat queued in
    a busy pane for 4m25s (17:16:07 → 17:20:32), the child gave up at 17:21:08 with
    `sent/no-boundary`, and the boundary landed at 17:22:49 on a compaction that had worked.
    Counting this row makes the queue wait cost nothing: the clock starts when the
    compaction does.
    """
    if engine != "claude":
        return False
    content = (entry.get("message") or {}).get("content")
    return isinstance(content, str) and content.strip() == COMPACT_CMD


def turn_of(entry, engine):
    """`(role, text)` for a line that is a real turn, else `(None, "")`.

    A turn is a person's prompt or the agent's own words. Tool results are user-role too on
    both engines, and so is everything a compaction writes about itself; neither is a turn.
    `text` is populated for assistant lines only — it is what `_declares` reads — and a
    user prompt answers `("user", "")`, which is all any caller asks of it.
    """
    if engine == "codex":
        if entry.get("type") != "response_item":
            return None, ""
        payload = entry.get("payload") or {}
        role = payload.get("role")
        if payload.get("type") != "message" or role not in ("user", "assistant"):
            return None, ""
        said = "\n".join(
            block.get("text") or "" for block in payload.get("content") or []
            if isinstance(block, dict)
            and block.get("type") in ("input_text", "output_text"))
        if role == "user":
            typed = codex_transcript.strip_attachments(said)
            # Codex inlines the instruction docs and every tool caveat as user text. They
            # are the rollout talking to itself, not a prompt, and counting one as a turn
            # aborts the continue on the session's own boundary.
            if not typed or typed.startswith(codex_transcript._NOISE_PREFIXES):
                return None, ""
            return "user", ""
        return ("assistant", said) if said.strip() else (None, "")
    kind = entry.get("type")
    if kind not in ("user", "assistant") or _is_compaction_artifact(entry):
        return None, ""
    content = (entry.get("message") or {}).get("content")
    if kind == "user":
        return ("user", "") if isinstance(content, str) and content.strip() else (None, "")
    if not isinstance(content, list):
        return None, ""
    said = "".join(block.get("text", "") for block in content
                   if isinstance(block, dict) and block.get("type") == "text")
    return ("assistant", said) if said.strip() else (None, "")


def rows(blob):
    """Every parseable JSON line in `blob`, in order. Unparseable lines are skipped."""
    for line in (blob or "").splitlines():
        try:
            yield json.loads(line)
        except ValueError:
            continue


def tail_from(path, offset):
    """Everything written after `offset`, or None when the file cannot be read."""
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None


def reading(path, engine=None):
    """The newest per-message context reading, or None."""
    try:
        got = context_ceiling.last_readings(path, want=1, engine=engine)
    except Exception:
        return None
    return got[-1] if got else None


def floor_of(path, engine):
    """The session's fixed overhead — what a compaction drops back to."""
    try:
        return context_ceiling.first_reading(path, engine)
    except Exception:
        return 0


def turn_state(path, engine):
    """"working" / "idle" / "" — what the rollout says the CLI is doing.

    Only codex answers this: its rollout carries `task_started` / `task_complete` events,
    which is the signal its screen cannot give (see `ENGINES`). Claude's readiness is read
    off the pane, so this returns "" there and `pane_idle` ignores it.
    """
    if engine != "codex":
        return ""
    try:
        return codex_transcript.summary(Path(path)).get("turn") or ""
    except Exception:
        return ""


def freshly_compacted(path, engine):
    """True when a boundary is newer than the last thing the session said.

    This is the guard the reading cannot be. `reading()` comes off assistant lines and a
    compaction writes none, so from the moment one lands until the session next replies the
    number still reports what the session weighed BEFORE it — 202,643 on a session sitting
    at 10,591. Every Stop in that window clears the heavy cut on a stale figure and sends a
    second `/compact` at a session that was just compacted. "Never twice, the reading will
    have fallen" was the one promise in this file that could not hold, because the reading
    is precisely what does not fall. The boundary line does not lie; read that instead.
    """
    blob = context_ceiling.tail(path, context_ceiling.TAIL_BYTES)
    seen = False
    for entry in rows(blob):
        if is_boundary(entry, engine):
            seen = True
        elif turn_of(entry, engine)[0] == "assistant":
            seen = False  # the session has spoken since; the reading is live again
    return seen


def turn_moved(path, offset, engine):
    """True when a real turn line landed after `offset` — new words, not new bytes.

    The guard this replaces compared file SIZE, and the CLI appends its own
    `stop_hook_summary` and `turn_duration` system lines right after the Stop hooks return
    — so every delivery "grew" a beat after the child started watching, and whether the
    child's first check beat the CLI's write was a coin flip. Half of all legitimate
    compactions died as phantom `superseded`, and each deferred boundary then fired at the
    NEXT Stop instead: somebody mid-conversation, watching a compaction land on a question
    asked three minutes ago.

    Unreadable answers True — the one direction that never types.
    """
    blob = tail_from(path, offset)
    if blob is None:
        return True
    return any(turn_of(entry, engine)[0] for entry in rows(blob))


def _declares(text):
    """True when `CONTINUE_MARK` is the closing line of `text`, alone on it.

    The whole point of a marker over a classifier is that writing it has to be deliberate,
    and "appears anywhere in the message" is not deliberate — it is satisfied by any
    sentence that mentions the thing. Backticks are tolerated because `context_ceiling`
    shows the marker inside them, so a session copying the instruction verbatim gets what
    it plainly meant rather than a silent miss.
    """
    for line in reversed((text or "").splitlines()):
        if line.strip():
            return line.strip().strip("`").strip() == CONTINUE_MARK
    return False


def _closing_message(path, engine):
    """(text of the last assistant message with text, is the turn's own still missing).

    The second half is the race guard. A real user prompt sitting AFTER the newest
    assistant text means the turn that just ended has not written its own reply yet, so
    that text belongs to the previous turn and answering from it answers the wrong
    question.
    """
    blob = tail_from(path, 0)
    if blob is None:
        return None, False
    text, said_at, asked_at = None, -1, -1
    for i, entry in enumerate(rows(blob)):
        role, said = turn_of(entry, engine)
        if role == "user":
            asked_at = i
        elif role == "assistant":
            text, said_at = said, i
    return text, asked_at > said_at


def asked_to_continue(path, engine, settle=0.0):
    """True when the turn that just ended closed with `CONTINUE_MARK`.

    Reads the last assistant message with text in it and asks whether its closing line is
    the marker, nothing else. Not any earlier turn — a session that parked once, got its
    resume, and then finished is finished.

    Every failure answers False, which is "finished", which leaves the session alone. An
    unreadable transcript, a session that never heard of the marker, a turn that ended in
    tool calls, a read that lost the race: all of them cost a seam at worst — the CLI's own
    auto-compact backs that up — never a ghost boundary over finished work. That is the
    direction where being wrong is cheap.
    """
    deadline = time.time() + settle
    while True:
        text, pending = _closing_message(path, engine)
        if not pending or time.time() >= deadline:
            return _declares(text)
        time.sleep(SETTLE_POLL)


def closing_text_after(path, offset, engine):
    """The newest assistant text written after `offset`, or None if none has landed.

    Reading from the Stop-time offset is what makes the answer unambiguous: the CLI writes
    the turn's closing message after the Stop hooks return, so a text line landing past
    that point IS the turn's own last word — never a mid-turn status note, which is what a
    full-file read answers with while the real message is still in flight.
    """
    blob = tail_from(path, offset)
    if blob is None:
        return None
    text = None
    for entry in rows(blob):
        role, said = turn_of(entry, engine)
        if role == "assistant":
            text = said
    return text


def settle_declaration(path, offset, engine):
    """The child's one read of the declaration: (wants_resume, supersession baseline).

    Waits for the closing message to land past the Stop-time offset. If nothing lands, the
    message was written before Stop (older CLI, different timing) and the newest text on
    file is the answer.

    The baseline moves to NOW, past the closing line it just read, so `turn_moved` does not
    read the turn's own last word as a new turn and abort the send it gates.
    """
    deadline = time.time() + SETTLE_SECS
    text = None
    while time.time() < deadline:
        text = closing_text_after(path, offset, engine)
        if text is not None:
            time.sleep(SETTLE_POLL)  # a sibling text block may still be landing
            text = closing_text_after(path, offset, engine) or text
            break
        time.sleep(SETTLE_POLL)
    if text is None:
        text, _ = _closing_message(path, engine)
    try:
        baseline = max(os.path.getsize(path), offset)
    except OSError:
        baseline = offset
    return _declares(text), baseline


def resume_scan(path, offset, engine):
    """(state, signs) — where our `/compact` has got to, and how much of its own
    bookkeeping it has written since we sent it.

    The state is the answer every caller wanted; `signs` is the one `wait_and_continue`
    needed and did not have. A compaction writes NOTHING to the transcript for its whole run
    except the lines it writes about itself, and of those the only one written BEFORE the
    boundary is the `/compact` the box took from us — the other four (caveat, echoed
    command, stdout, summary) are flushed after it, so counting only those gave a counter
    that could never move. Counting the submitted command turns "still nothing" into two
    different facts: nothing has happened, or something is happening and has not finished.

    `waiting` — no boundary on file yet; the compaction is still running.
    `landed`  — the boundary is written and nothing has spoken since.
    `taken`   — a real turn followed it. A person is driving; this is no longer ours.
    """
    blob = tail_from(path, offset)
    if blob is None:
        return "taken", 0  # unreadable: the one direction that never types
    landed, signs = False, 0
    for entry in rows(blob):
        if is_boundary(entry, engine):
            landed = True
            continue
        role = turn_of(entry, engine)[0]
        if role is None:
            if _is_compaction_artifact(entry):
                signs += 1
            continue
        if not landed and _is_sent_command(entry, engine):
            signs += 1  # our own `/compact`, taken by the box — see `_is_sent_command`
            continue
        if landed:
            return "taken", signs
    return ("landed" if landed else "waiting"), signs


def resume_state(path, offset, engine):
    """Just the state. The shape every caller but the waiter needs."""
    return resume_scan(path, offset, engine)[0]


# --- reading the pane --------------------------------------------------------------------

#: Any escape sequence, for reading a captured pane as the words it draws.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

#: Just the colour/intensity ones, whose state has to be tracked across a line.
_SGR = re.compile(r"\x1b\[([0-9;]*)m")

#: The CLI's own argument placeholder at the end of a composer line — `/compact <optional
#: custom summarization instructions>`. On screen, never typed. See `was_aborted`.
_ARG_HINT = re.compile(r"\s*<[^<>]*>\s*$")


def pane(name):
    """The session's visible screen WITH its styling, or None if there is no such session.

    `-e` is load-bearing, and its absence was a bug: the CLI's inline suggestion is drawn
    into the input box in dim (SGR 2), so a plain capture renders `❯ continue` for a box
    that is EMPTY and one somebody typed `continue` into identically. The composer gate read
    the suggestion as typing and refused to compact — a session parked at 215k with five
    open tasks, silently, because the guard could not see grey.

    Every reader below that wants words rather than styling strips it back out itself; only
    `composer_line` needs the intensity, and it must come from the SAME capture — a second
    call to get the styled copy is a second moment, and the box can change between them.
    """
    try:
        out = subprocess.run(managed._t("capture-pane", "-p", "-e", "-t", name),
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _typed(line):
    """The part of a captured line a person actually put there — everything NOT dim.

    Dim is the CLI drawing its own text: the ghost completion in the composer, the empty
    box's placeholder. It is on screen, it is not input, and pressing Enter does not send
    it. Solid is a person. Splitting on that is the only signal that separates them, since
    the characters themselves are identical.

    A line with no styling at all — a synthetic pane, a `capture-pane` without `-e` — comes
    back whole, so this can only ever ADD emptiness, never invent typed text.
    """
    solid, dim, pos = [], False, 0
    for match in _SGR.finditer(line):
        if not dim:
            solid.append(line[pos:match.start()])
        codes = (match.group(1) or "0").split(";")
        i = 0
        while i < len(codes):
            code = codes[i] or "0"
            if code in ("38", "48", "58"):
                # An extended colour, whose PARAMETERS are not intensity codes: `38;5;N`
                # picks from the 256 palette, `38;2;R;G;B` is truecolour. Reading them one
                # at a time sees the `2` in either and calls the rest of the line dim — and
                # `38;5;2` (green) is a colour these CLIs actually draw. That reads real
                # typing as a ghost, which is the one direction this must never be wrong
                # in: it types `/compact` onto the end of somebody's message.
                i += 3 if (i + 1 < len(codes) and codes[i + 1] == "5") else 5
                continue
            if code == "2":
                dim = True
            elif code in ("0", "22"):
                dim = False
            i += 1
        pos = match.end()
    if not dim:
        solid.append(line[pos:])
    return _ANSI.sub("", "".join(solid))


def composer_line(screen, engine="claude"):
    """What has been typed into the input box, or None when the screen shows no box.

    The last prompt glyph on screen: a queued message renders as an indented prompt row
    ABOVE the box, so first-match would read the queue and call the box by its name.

    Dim content is dropped — see `_typed`. An empty box showing the CLI's own suggestion
    reads as "", which is what it is: press Enter there and nothing is sent. Codex draws its
    invitation in a palette grey rather than SGR 2, so it is named in `placeholder` and
    struck out here by text; a placeholder is the CLI's, never a person's.
    """
    spec = grammar(engine)
    prompt = spec["prompt"]
    lines = (screen or "").splitlines()
    at = [i for i, line in enumerate(lines)
          if _ANSI.sub("", line).lstrip().startswith(prompt)]
    if not at:
        return None
    index = at[-1]
    typed = _typed(lines[index]).lstrip()
    typed = (typed[len(prompt):] if typed.startswith(prompt) else typed).strip()
    if typed in spec["placeholder"]:
        typed = ""
    if typed or spec["boxed"]:
        return typed
    # A MULTILINE DRAFT WHOSE FIRST LINE IS BLANK. The prompt row carries only the glyph
    # and the words are on the row below it, so reading the glyph row alone calls that box
    # empty and types into somebody's half-written message. Only asked of an engine that
    # draws no box: a boxed composer's next row is its own border (measured on a live pane:
    # the row under an idle `\u276f` is `\u2500\u2500\u2500\u2500...`), so this rule
    # there would answer "occupied" for every idle session forever.
    below = lines[index + 1] if index + 1 < len(lines) else ""
    return "" if not _typed(below).strip() else _typed(below).strip()


def pane_idle(screen, engine="claude", turn=""):
    """Everything `pane_is_ready` asks EXCEPT what is in the box: is this CLI doing work?

    Split out because the box means opposite things to the two callers. Readiness wants it
    empty. `was_aborted` wants it holding one exact string — and both need the same answer
    to "is anything running", from the same marks, or the two can disagree about a pane they
    are looking at together.

    Unknown is not idle. Every path that cannot prove the CLI is quiet answers False,
    because the cost of being wrong is typing `/compact` onto the end of somebody's message.

    Every check wants the WORDS on screen, so they run against a stripped copy: `pane()`
    captures styling, and a marker split by a colour code mid-phrase would go unmatched —
    which for the footer and the busy marks fails OPEN. Only the composer needs the styling,
    and it reads the same capture.
    """
    if not screen:
        return False
    spec = grammar(engine)
    plain = _ANSI.sub("", screen)
    if spec["footer"] is not None:
        if not spec["footer"].search(plain):
            return False  # footer absent: TUI starting, or a dialog is up
    elif composer_line(screen, engine) is None:
        return False  # no composer row: TUI starting, or a dialog is up in its place
    if any(mark in plain for mark in spec["busy"]):
        return False
    if spec["screen_idle"]:
        if spec["working"].search(plain):
            return False  # a turn is running; ending it early is not this hook's call
    elif turn != "idle":
        return False  # this engine's screen cannot say; the rollout has to
    return True


def pane_is_ready(screen, engine="claude", turn=""):
    """True only when the CLI is idle at an empty box and a command typed now would RUN.

    Was `composer_is_empty`, and the rename is the fix. An empty box is not an idle CLI: a
    permission-mode banner is not a readiness signal, and it sits there unchanged while a
    turn runs and while a compaction runs. So the old check was true of a session mid-turn
    — where a send cuts the turn short — and true of a session mid-compaction, where the
    send queues and waits for someone to press Enter.
    """
    return pane_idle(screen, engine, turn) and composer_line(screen, engine) == ""


def why_not_ready(screen, engine="claude", turn=""):
    """Which gate refused, as one short label — "" for a pane a send would run in.

    `pane_is_ready` answers the question the sends ask. This answers the one every
    post-mortem asks, and until now no line on file could: `busy` named the outcome and
    never the cause, so four abandoned seams (see `SEAM_WAIT_SECS`) left no way to tell a
    pane holding somebody's half-typed message from one whose footer had gone missing —
    which is a fault that has happened, eleven times in one session, and was found by hand.

    The branches mirror `pane_idle` and `pane_is_ready` in their order, and a parity test
    holds the two together so a gate added to either cannot go unnamed here.
    """
    if not screen:
        return "no-pane"
    spec = grammar(engine)
    plain = _ANSI.sub("", screen)
    if spec["footer"] is not None:
        if not spec["footer"].search(plain):
            return "no-footer"
    elif composer_line(screen, engine) is None:
        return "no-composer"
    for mark in spec["busy"]:
        if mark in plain:
            return "busy-mark: " + mark
    if spec["screen_idle"]:
        if spec["working"].search(plain):
            return "working"
    elif turn != "idle":
        return "turn: " + (turn or "unknown")
    line = composer_line(screen, engine)
    if line is None:
        return "no-composer"
    if line != "":
        # The text itself is the person's, and it is not this log's to keep. Its length is
        # enough to tell "somebody is writing" from a `/compact` left armed in the box.
        return f"composer-holds: {len(line)} chars"
    return ""


#: What the pane refused on, set where a wait gives up and read by the line that records
#: the outcome. A module slot rather than a return value because every wait here answers
#: with one string that a dozen call sites and tests compare against.
_LAST_BLOCK = {}


def note_block(name, engine, turn, waited):
    """Read the pane once more, at the moment of giving up, and remember why."""
    _LAST_BLOCK.clear()
    _LAST_BLOCK.update(blocked=why_not_ready(pane(name), engine, turn) or "unknown",
                       waited=int(waited))


def block_note():
    """The note, once. Cleared on read, so it can never be attached to a later outcome."""
    note = dict(_LAST_BLOCK)
    _LAST_BLOCK.clear()
    return note


def was_aborted(screen, text, engine="claude", turn=""):
    """True when the CLI has handed our own command straight back to the composer.

    WHAT AN ABORTED COMPACTION LOOKS LIKE, measured by running `/compact` in a throwaway
    session and pressing Escape four seconds in. The CLI writes the prompt line and the
    command's caveat — both parented to `turn_duration`, so the caveat is the prompt's
    SIBLING and the prompt has no descendants at all — then no `<command-name>`, no stdout,
    no boundary, ever. And it puts the command back in the box:

        ❯ /compact  <optional custom summarization instructions>

    That box is armed. Typing into it does not replace the command, it ARGUES it: the next
    thing anybody writes becomes `/compact <their sentence>`, and their Enter compacts the
    session with that message as the summarization instruction instead of sending it. This
    file calls a `/compact` left in a box a loaded gun pointed at whatever gets typed next
    and closes every path that could leave one — except this one, which re-arms it AFTER
    `submit` has proved the send and stopped watching. One session sat like that for three
    minutes.

    A RUNNING COMPACTION IS NOT THIS. It keeps drawing the input box, empty, with the echoed
    command above it in the transcript area and `Compacting conversation…` on screen — so
    the busy marks say "working" and `composer_line` reads the empty box, not the echo.
    `pane_idle` is what separates them, and it is asked first.

    EXACT MATCH, because the whole point is to clear OUR leftover and never anybody's words.
    The moment somebody types into that box it reads `/compact hello`, which is not our
    string — so it is their line now, damaged but theirs, and this returns False and leaves
    it.

    EXCEPT FOR THE CLI'S OWN ARGUMENT HINT, which is on screen and is not typed text. The
    real pane reads `/compact  <optional custom summarization instructions>`, and the hint
    is drawn in a 256-palette grey (`38;5;241`) rather than SGR 2 — so `_typed`, which
    separates real characters from the CLI's by DIMNESS, correctly keeps it. Comparing
    against the bare command therefore failed on the very pane this was written for. A
    trailing `<...>` is stripped because nothing a person types is wrapped in angle
    brackets, and the alternative — matching only the first token — would also swallow
    `/compact hello`, which is the one case that must never be wiped.
    """
    line = composer_line(screen, engine)
    if line is not None:
        line = _ARG_HINT.sub("", line).strip()
    return pane_idle(screen, engine, turn) and line == (text or "").strip()


# --- typing into it ----------------------------------------------------------------------

def send_text(name, text):
    """Type `text` into the session's input box and submit it."""
    for argv in managed._type_argv(name, text):
        subprocess.run(argv, check=True, timeout=5)
    subprocess.run(managed._t("send-keys", "-t", name, "Enter"), check=True, timeout=5)


def clear_line(name):
    """^U — wipe the input box. What the app's `clear` key sends."""
    subprocess.run(managed._t("send-keys", "-t", name, "C-u"), check=True, timeout=5)


def still_ours(screen, text, engine="claude"):
    """True when the box is holding the text we just typed, and not somebody's own.

    Only ever asked in the seconds after our own Enter, and only ever answered yes for a box
    that starts with what we sent — anybody typing into the gap keeps their keystrokes.
    """
    line = composer_line(screen, engine)
    return bool(line) and line.startswith(text.strip()[:12])


def took_effect(screen, engine="claude", turn=""):
    """True when the CLI is visibly DOING the thing we just submitted.

    The direct evidence, and it has to outrank the box, because the CLI ECHOES a slash
    command as its own prompt row in the transcript area and then — for `/compact` — stops
    drawing the input box at all while it runs. `composer_line` takes the last prompt glyph
    on screen, so for those two and a half minutes the echo IS the last one, and it reads
    back the exact text we sent.

    That made `submit` diagnose its own successful send as stuck: it wiped the line with ^U
    and returned False, `wait_and_send` reported `not-taken`, and `run` returned without
    ever reaching `wait_and_continue`. The compaction it had just triggered went through;
    the continue that was supposed to follow it never fired, and the session parked at its
    summary with a full docket — exactly the hole the continue exists to close, reopened by
    the guard meant to make the send safe.
    """
    spec = grammar(engine)
    plain = _ANSI.sub("", screen or "")
    if any(mark in plain for mark in spec["busy"]):
        return True
    if spec["screen_idle"]:
        return bool(spec["working"].search(plain))
    return turn == "working"  # no spinner to match: the rollout is the only witness


def submit(name, text, engine="claude", path=None):
    """Type it, submit it, and prove it went — or take it back out.

    An Enter is not a submission. Typing a slash command opens the CLI's autocomplete
    (`/compact` matches three entries once two completion skills are installed), and any
    pane that turns out to be busy holds the line instead of running it. Either way the text
    stays in the box, and `/compact` left in a box is a loaded gun pointed at whatever gets
    typed next.

    Two ways to prove it went, and `took_effect` is asked FIRST because it is the only one
    that is true while a compaction is running — the box is not on screen to be empty.
    Failing that, a box no longer holding our text is gone. Only when neither is true for
    the whole window is it really stuck, and then it gets wiped.
    """
    send_text(name, text)
    deadline = time.time() + SUBMIT_CHECK_SECS
    while time.time() < deadline:
        time.sleep(POLL_SECS)
        screen = pane(name)
        if screen is None:
            return False  # session gone: nothing left to clean up
        if took_effect(screen, engine, turn_state(path, engine) if path else ""):
            return True
        if not still_ours(screen, text, engine):
            return True
    clear_line(name)
    return False


#: The command itself, in one place: `send_compact` types it and `was_aborted` has to
#: recognise it coming back. Two spellings of it is a guard that silently stops matching.
COMPACT_CMD = "/compact"


def send_compact(name, engine="claude", path=None):
    return submit(name, COMPACT_CMD, engine, path)


def send_continue(name, has_rows=False, engine="claude", path=None):
    """The nudge, plus the pointer to this session's own rows when it has any.

    `has_rows` is passed down from the child's single `docket()` call rather than asked
    again here, for the same reason `wants_resume` is: the store can change between two
    reads, and a nudge that promises rows a session cannot find is worse than no pointer.
    """
    return submit(name, CONTINUE + (DOCKET_LINE if has_rows else ""), engine, path)


# --- the decision log --------------------------------------------------------------------

def record(**fields):
    """Append one line to the decision log. Never raises, never blocks the hook.

    Prunes on write rather than on a schedule — two lines per delivery is a file that would
    take years to matter, and a pruner nobody runs is a file that grows forever. Everything
    here is wrapped: a full disk must cost the log, not the compaction.
    """
    try:
        path = log_path()
        line = dict(fields, ts=time.strftime("%Y-%m-%dT%H:%M:%S"))
        path.parent.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - LOG_RETAIN_DAYS * 86400
        if path.exists():
            try:
                kept = [ln for ln in path.read_text().splitlines() if ln.strip()]
            except OSError:
                kept = []
            if len(kept) > 4000:  # only ever walked when the file is genuinely large
                keep = []
                for ln in kept:
                    try:
                        stamp = json.loads(ln).get("ts", "")
                        old = time.mktime(time.strptime(stamp, "%Y-%m-%dT%H:%M:%S")) < cutoff
                    except (ValueError, OverflowError):
                        old = False
                    if not old:
                        keep.append(ln)
                path.write_text("\n".join(keep) + "\n")
        with path.open("a") as fh:
            fh.write(json.dumps(line) + "\n")
    except Exception:
        pass


# --- what else was on file ---------------------------------------------------------------

def codex_plan(path):
    """The newest native plan in a codex rollout: `open` / `done` / `unknown`.

    Codex writes no per-session task directory, so the Claude store below answers `unknown`
    for every codex session — which is honest but useless, and the engine does keep the same
    fact in band: `update_plan` calls carry the whole list each time, so the last one is the
    current plan. Only structured calls are read; a shell command that happens to write a
    checklist is not a task API.
    """
    blob = context_ceiling.tail(path, context_ceiling.TAIL_BYTES)
    answer = "unknown"
    for entry in rows(blob):
        if entry.get("type") != "response_item":
            continue
        payload = entry.get("payload") or {}
        if (payload.get("name") or "").split(".")[-1] != "update_plan":
            continue
        try:
            plan = json.loads(payload.get("arguments") or "{}")["plan"]
            answer = "open" if any(step.get("status") != "completed" for step in plan) else "done"
        except (ValueError, KeyError, TypeError):
            answer = "open"  # a plan it wrote and we cannot read is not evidence of finishing
    return answer


def docket(sid, path=None, engine="claude"):
    """What this session's task store says about whether it finished. Three answers.

    LOGGED, NOT OBEYED. This used to gate the compaction and it was the bug: the store is
    written only by a session that used the task tools, so almost every session answered
    `unknown` and got read as finished. The declared marker decides now. This stays because
    the answer is free and worth having in the decision log next to the declaration — a
    disagreement between the two is the first place to look when a session compacted, or did
    not, and should not have.

    `open`    — a `pending` or `in_progress` row. The turn ended mid-task.
    `done`    — rows, all of them closed. The session worked its way to the end of them.
    `unknown` — no store, no readable rows, nothing. NOT a "no".
    """
    if engine == "codex":
        return codex_plan(path) if path else "unknown"
    folder = TASKS_DIR / sid
    try:
        names = os.listdir(folder)
    except OSError:
        return "unknown"
    seen = False
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            status = json.loads((folder / name).read_text()).get("status")
        except (OSError, ValueError):
            continue
        seen = True
        if status in OPEN_STATUS:
            return "open"
    return "done" if seen else "unknown"


def compacts_when_done(agent):
    """True when the switch is on for the agent whose seat this is.

    The agent comes off the host's own session registry — the row the Hub wrote when it
    spawned this session — rather than from a workspace-prefix walk over a private agents
    config. A session the host launched already knows whose it is, and a session it did not
    launch has no agent and no switch, which is the same answer either way: off.

    Everything that does not resolve is off. Off is what an agent nobody switched on gets,
    and off means a finished delivery is simply left alone — the compaction nobody asked for
    is the one people object to.
    """
    try:
        return bool(agent) and agent_prefs.is_on(WHEN_DONE, agent)
    except Exception:
        return False


# --- across the boundary -----------------------------------------------------------------

def wait_and_send(name, path, threshold, size_at_stop, engine, budget=None):
    """Poll until it is provably safe, then compact. Silence on every other outcome.

    `budget` is how long a pane may stay unready before this walks away, and the caller
    sets it from the promise it made: `SEAM_WAIT_SECS` for a session that declared a seam,
    the short `MAX_WAIT_SECS` for one nobody is waiting on. See `SEAM_WAIT_SECS`.
    """
    started = time.time()
    deadline = started + (budget or MAX_WAIT_SECS)
    while time.time() < deadline:
        keepalive()
        if not os.path.exists(path):
            return "gone"
        if turn_moved(path, size_at_stop, engine):
            return "superseded"  # a new turn started; this decision is stale
        if pane_is_ready(pane(name), engine, turn_state(path, engine)):
            cur = reading(path, engine)
            if cur is None or cur < threshold:
                return "already-compacted"
            return "sent" if send_compact(name, engine, path) else "not-taken"
        time.sleep(POLL_SECS)
    note_block(name, engine, turn_state(path, engine), time.time() - started)
    return "busy"


def wait_and_continue(name, path, offset, wants_resume, has_rows=False, engine="claude"):
    """Once the boundary lands, hand the session back its work — if it asked for it.

    `wants_resume` IS THE DECLARATION, PASSED DOWN, not read again. Re-reading was the hole
    a previous version fell in: the parent read one answer and the child read another off
    the same message (the closing line lands asynchronously), which compacted a session and
    then refused to resume it — compacted AND abandoned, the one outcome no branch here is
    supposed to produce. One read, one answer.

    A boundary taken WITHOUT the marker is the opted-in delivery case: the Compact When Done
    switch took it, the work is finished, and there is nothing to step across for.
    """
    started = time.time()
    deadline = started + BOUNDARY_WAIT_SECS
    ceiling = started + MAX_BOUNDARY_WAIT_SECS
    landed_at, signs = None, 0
    while time.time() < min(deadline, ceiling):
        keepalive()
        state, seen = resume_scan(path, offset, engine)
        if seen > signs:
            # The CLI wrote another of the compaction's own lines. Whatever the delay
            # between our Enter and the work starting, the work is demonstrably happening,
            # so the budget starts again from here — see BOUNDARY_WAIT_SECS.
            signs, deadline = seen, time.time() + BOUNDARY_WAIT_SECS
        elif signs == 0 and not pane_idle(pane(name), engine, turn_state(path, engine)):
            # NOTHING HAS STARTED YET, so nothing is late yet. A send into a working pane
            # QUEUES: it runs when that turn ends, which is the client's clock and not
            # ours, and on 2026-09-24 it was 4m25s of a 5m budget. Counting the queue
            # against the compaction is what made a busy pane fatal. The 900s ceiling
            # still bounds it, and an Escape out of the box is caught below.
            deadline = time.time() + BOUNDARY_WAIT_SECS
        if state == "taken":
            return "taken"
        turn = turn_state(path, engine)
        if state == "landed":
            if not wants_resume:
                return "delivered"  # the switch's boundary; nothing waiting to resume
            landed_at = landed_at or time.time()
            if pane_is_ready(pane(name), engine, turn):
                return "continued" if send_continue(name, has_rows, engine, path) else "not-taken"
            # The TUI redraws for a moment after a compaction, and somebody may have started
            # typing during it. Give the box a chance to settle, then leave it to them —
            # and give it the seam's patience, because what this branch drops is a session
            # already compacted, sitting at a summary nothing handed back. The ceiling on
            # the loop above still bounds it.
            if time.time() - landed_at > SEAM_WAIT_SECS:
                note_block(name, engine, turn, time.time() - landed_at)
                return "busy"
        elif was_aborted(pane(name), COMPACT_CMD, engine, turn):
            # Killed mid-run. No boundary is ever coming, and the command is sitting armed
            # in the box — see `was_aborted`. Take it back out and stop waiting; the seam is
            # now unclaimed, which an outside stall sweep picks up as its own signal.
            clear_line(name)
            return "aborted"
        time.sleep(POLL_SECS)
    note_block(name, engine, turn_state(path, engine), time.time() - started)
    return "no-boundary"


def continue_in_place(name, path, sid, offset, has_rows=False, engine="claude"):
    """Hand a parked session back its work WITHOUT taking a boundary.

    The other continue waits for a boundary to land because it is stepping across one. There
    is none here: the session asked to be carried on and its window had room, so the whole
    repair is the prompt. That makes this the simpler path and the one with no compaction to
    race — it needs only the two guards that decide whether typing is allowed at all, and it
    takes both from the same functions the boundary path uses.

    Giving up is not the cheap kind of silence the rest of this file accepts, because "the
    next Stop retries" is false for a session that parked. So the wait is the seam's rather
    than the short one, and the outside stall sweep that finds an unclaimed seam is the
    backstop for it rather than the plan.
    """
    started = time.time()
    deadline = started + SEAM_WAIT_SECS
    while time.time() < deadline:
        keepalive()
        if not os.path.exists(path):
            outcome = "gone"
            break
        if turn_moved(path, offset, engine):
            outcome = "taken"  # a person is driving; this is no longer ours
            break
        if pane_is_ready(pane(name), engine, turn_state(path, engine)):
            text = CONTINUE_IN_PLACE + (DOCKET_LINE if has_rows else "")
            outcome = "continued" if submit(name, text, engine, path) else "not-taken"
            break
        time.sleep(POLL_SECS)
    else:
        outcome = "busy"
        note_block(name, engine, turn_state(path, engine), time.time() - started)
    record(sid=sid[:8], outcome=f"in-place/{outcome}", **block_note())
    return f"in-place/{outcome}"


# --- the lock ----------------------------------------------------------------------------

def keepalive():
    """Refresh the lock's mtime so a long wait is never mistaken for a crash.

    `held()` reclaims a lock file older than STALE_LOCK_SECS, and that window used to be
    shorter than the child's entire life. Waiting out a compaction runs straight past it —
    145s of it on the session that prompted this — and a reclaimed lock is two children
    typing into one pane. Touching makes the mtime a liveness signal, which is the only
    thing it was ever being read as.
    """
    try:
        if _HELD_AT:
            os.utime(_HELD_AT, None)
    except OSError:
        pass


def lock_path(sid):
    """This session's own lock file. Keyed by sid — see STALE_LOCK_SECS for why."""
    return lock_dir() / f"compact-delivery.{sid}.lock"


def reap():
    """Delete every session lock nobody is keeping alive. Never raises.

    THE PER-SID FIX TRADED A CONTENTION BUG FOR A LEAK. One machine-wide lock was one file
    forever; one lock per session is one file per session that ever ended, and the only
    unlink was `held()` clearing the path it was about to take — for its OWN sid. No sid ever
    swept another's, and a sid never comes back, so every lock the hook had ever taken was
    still on disk: 66 of them when somebody counted, all zero bytes, all ownerless.

    `keepalive()` is what makes this safe and is why the mtime is the only test needed: a
    child holding a lock touches it through every wait it does, so a live holder's stamp is
    never more than a poll old. Older than STALE_LOCK_SECS means the holder is gone — the
    same judgement `held()` already makes before reclaiming one, applied to all of them.

    Best-effort throughout. A lock that cannot be removed is a lock that gets removed on the
    next delivery, and a hook that fails over housekeeping is a hook switched off.
    """
    folder = lock_dir()
    try:
        names = os.listdir(folder)
    except OSError:
        return
    cutoff = time.time() - STALE_LOCK_SECS
    for name in names:
        if not (name.startswith("compact-delivery.") and name.endswith(".lock")):
            continue
        try:
            if (folder / name).stat().st_mtime < cutoff:
                (folder / name).unlink()
        except OSError:
            pass


def held(sid):
    """This SESSION's lock, or None when its own other child already has it.

    Cannot outlive a crash: stale after STALE_LOCK_SECS, then reclaimed. Per-sid, so a None
    here means one thing only — this session already has a child deciding, and that child
    owns the delivery. It can no longer mean "some unrelated session is busy".
    """
    global _HELD_AT
    path = lock_path(sid)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        reap()  # including, possibly, this sid's own — the unlink below is now its
        if path.exists() and time.time() - path.stat().st_mtime > STALE_LOCK_SECS:
            path.unlink()
        handle = path.open("w")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _HELD_AT = str(path)
        return handle
    except (OSError, ValueError):
        return None


# --- the decision ------------------------------------------------------------------------

def session_row(sid, path):
    """`(sid, row)` for the managed session this transcript belongs to, or `(sid, None)`.

    By sid first, because the host spawns a CLI with the session id it registers, so the two
    are the same string. By transcript second, for a session whose id the hook reports
    differently from the one the row was filed under — a resume, or an engine that mints its
    own.
    """
    try:
        registry = managed.open_registry()
    except Exception:
        return sid, None
    if sid in registry:
        return sid, registry[sid]
    for other, row in registry.items():
        if row.get("transcript") == str(path):
            return other, row
    return sid, None


def decide(path, sid, engine, agent, wants_resume):
    """What this turn-end has earned: `(tmux name, threshold)`, or `(None, reason)`.

      (name, HEAVY)  compact, then step across the boundary.
      (name, None)   the session parked, but a boundary would gain it nothing — hand it
                     back where it stands, with its context untouched.
      (None, reason) leave it alone.

    THE MARKER IS TWO CLAIMS AND ONLY ONE WAS EVER HONOURED. `CONTINUE_MARK` says both
    "compact me here" and "I am not finished — hand me back", and every gate in this function
    was written to answer the first. So a session that parked in a window with room satisfied
    no compaction gate and got NOTHING: no boundary, no nudge, and — since the marker means
    it parked — no next turn to reconsider it. One declared at 146,143, 13,857 short of the
    cut, closed with `Next Move: Task #6`, and sat dead with four open rows. The cut decides
    whether the CONTEXT needs surgery. It was never a statement about whether the WORK is
    finished.

    So the weight questions no longer end the decision; they only choose between the two ways
    of honouring a seam. What still ends it, ahead of them, is everything that means this
    turn is not a park at all (silence is a finished delivery) or that there is nothing to
    type into (no pane) — because those are the two answers where doing nothing is right on
    both counts.

    `wants_resume` arrives as an argument rather than being read here so that the log, this
    gate and the child across the boundary are all quoting one read of one file.
    """
    cur = reading(path, engine)
    if cur is None:
        return None, "no reading"
    if freshly_compacted(path, engine):
        return None, "a boundary is already the newest thing on file"
    if not wants_resume and not compacts_when_done(agent):
        return None, ("the turn ended without asking to be continued — a finished "
                      "delivery, and this agent does not compact those")
    name = managed._name(sid)
    if pane(name) is None:
        return None, "not a managed tmux session"

    # Below here the session is one this hook acts on. The only question left is whether a
    # boundary helps it — and when the answer is no, a parked session is still parked.
    if cur < compaction.HEAVY:
        if wants_resume:
            return name, None
        return None, f"{cur:,} under the heavy cut ({compaction.HEAVY:,})"
    freed = compaction.recoverable(cur, floor_of(path, engine))
    if freed < compaction.LITTLE:
        if wants_resume:
            return name, None
        return None, f"only {freed:,} reclaimable — the weight is this session's floor"
    return name, compaction.HEAVY


def main():
    """The parent: cheap pre-gates only, then hand everything to the detached child.

    The parent must not read the declaration — the closing message it would be judging is
    written only after this hook returns, and a parent that waits for it may be the very
    thing the CLI is waiting on. It checks what is already stable at Stop time (the reading,
    the boundary guard) and detaches; the child settles the declaration and makes the real
    decision.
    """
    if os.environ.get("SKIP_SESSION_HOOK") == "1":
        return
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    path = payload.get("transcript_path")
    sid = payload.get("session_id")
    if not path or not sid or not os.path.isfile(path):
        return
    sid, row = session_row(sid, path)
    engine = engine_of(path, row)

    cur = reading(path, engine)
    if cur is None:
        record(sid=sid[:8], weight=None, decision="skip", reason="no reading")
        return
    if freshly_compacted(path, engine):
        record(sid=sid[:8], weight=cur, decision="skip",
               reason="a boundary is already the newest thing on file")
        return

    # THE WEIGHT IS NOT A PRE-GATE, and it cannot be one. Whether this turn parked is
    # written in the closing message, which does not exist yet (see above), so a parent that
    # ends the decision on the number ends it before the only fact that matters is knowable.
    # It did: a session declaring a seam under the cut was skipped here, the child that reads
    # the marker was never spawned, and the session sat parked with nobody coming. `decide`
    # owns every weight question now — the parent's job is to hand over what is already
    # stable and get out.
    record(sid=sid[:8], weight=cur, decision="candidate")
    # Detach: Stop must return inside its timeout, and the closing message the decision needs
    # does not land until it does. The lock belongs to the child — taking it here would make
    # the parent race the process it just spawned.
    handoff = {"path": path, "sid": sid, "agent": (row or {}).get("agent", ""),
               "engine": engine, "size": os.path.getsize(path)}
    # THE CHILD DOES NOT RE-DERIVE WHERE THIS HOST KEEPS ITS STATE. `jstack-host` adopted
    # the installed host's environment on the way in (`cli._adopt`), which is what points
    # this process at the dashboard's state dir; a child started as `python -m` runs none of
    # that, resolves the DEFAULT profile, and lands on `~/.local/state/jremote`. One
    # delivery then wrote its `candidate` row in the host's state dir and its decision, its
    # outcome, its lock and its `compacts_when_done` read in a second directory nothing
    # serves -- so "why didn't it compact" was answerable from neither file, and the Compact
    # When Done switch was read from a store the app never writes. Handing down the answer
    # the parent already used cannot drift; re-resolving it in the child is what did.
    subprocess.Popen([sys.executable, "-m", __name__, "--child", json.dumps(handoff)],
                     start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     env={**os.environ, "JREMOTE_STATE_DIR": str(hostenv.state_dir())})


def run_child(path, sid, agent, engine, size_at_stop):
    """The child's whole life: settle the declaration, decide, act, say what happened.

    One read of the declaration feeds the gate, the log and the continue alike — two reads
    of an asynchronously landing line once answered two different ways and left a session
    compacted AND abandoned.
    """
    wants, baseline = settle_declaration(path, size_at_stop, engine)
    name, threshold = decide(path, sid, engine, agent, wants)
    dock = docket(sid, path, engine)  # one read, shared by the log and the nudge — see DOCKET_LINE
    record(sid=sid[:8], weight=reading(path, engine), engine=engine,
           resume=wants, docket=dock,
           decision="skip" if name is None else ("compact" if threshold else "continue"),
           reason=None if name else threshold)  # the second slot is the reason on a skip
    if name is None:
        return threshold
    if threshold is None:  # parked, but a boundary buys it nothing — see `decide`
        return continue_in_place(name, path, sid, baseline, dock == "open", engine)
    return run(name, path, sid, threshold, baseline, wants, dock == "open", engine)


def run(name, path, sid, threshold, size_at_stop, wants_resume, has_rows=False,
        engine="claude"):
    """Compact, then step across the boundary. One lock covers both — the continue is part
    of the same decision, and a second delivery FROM THIS SESSION must not start its own
    half of it. Another session's delivery is not a conflict and never was."""
    sent = wait_and_send(name, path, threshold, size_at_stop, engine,
                         SEAM_WAIT_SECS if wants_resume else MAX_WAIT_SECS)
    outcome = sent if sent != "sent" else \
        f"sent/{wait_and_continue(name, path, size_at_stop, wants_resume, has_rows, engine)}"
    record(sid=sid[:8], outcome=outcome, **block_note())
    return outcome


def child(argv):
    handoff = json.loads(argv[0])
    sid = handoff["sid"]
    lock = held(sid)
    if lock is None:
        # Recorded, not silent. This was the one branch that returned having written
        # nothing, so a lost seam left a `candidate` with no successor line and no way to
        # tell it from a child that ran and decided against compacting.
        record(sid=sid[:8], decision="skip",
               reason="this session already has a child deciding this delivery")
        return
    run_child(handoff["path"], sid, handoff.get("agent", ""),
              handoff.get("engine", "claude"), int(handoff["size"]))


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--child":
        try:
            child(sys.argv[2:])
        except Exception as exc:
            record(outcome="error: " + str(exc))
    else:
        try:
            main()
        except Exception:
            pass  # a hook that dies is a hook switched off for the rest of the session
