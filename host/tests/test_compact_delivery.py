"""Seam compaction: the session takes its own boundary when the turn ends.

Auto-compact fires beside every model call, so its boundary lands mid-task; nothing can
defer it and no tool can invoke `/compact`. The only lever is WHEN, and Stop is the seam --
work delivered, nothing half-written. This hook compacts there instead, at the load meter's
heavy cut, on the grounds that reaching auto-compact at all is already a failure.

THE SPEC HAS TWO MODES. Mid-run: a working session that the ceiling
injection told to stop parks its docket at a seam and closes its final message with
`CONTINUE_MARK` -- that compacts and resumes, unconditionally. Delivery: a turn that ends
WITHOUT the marker is finished work, and finished work is left alone unless that agent's
per-agent Compact When Done switch is on -- off by default, and even on it never resumes.

SILENCE MEANS DONE. The previous design read silence as "not finished" and compacted every
heavy turn-end by default. Sessions almost never wrote the done-marker (two declarations
against a hundred Stops in the decision log), so every finished delivery ghost-compacted in
a delivered turn and then spoke to itself off the continue nudge. The marker sits on the
mid-run side now because that is the side a session can be relied on to write: it is
obeying the injection it just read. A forgotten marker costs a seam at worst -- the
client's own auto-compact is the backstop -- never a ghost boundary over finished work.

The cut is measured (`compaction.HEAVY`) and does NOT come from `autoCompactWindow`. That
derivation was the bug this replaces: a running session resolves its window once at startup
and never re-reads the setting, so the hook read a 267k trigger while the client was still
using 167k and stayed silent through a delivery the client showed as 4% from compacting.

The send is `tmux send-keys` because that is the only mechanism that exists, which makes the
whole risk surface "could these keys land somewhere they were not meant to". That is what
most of this file pins. The expensive failure is not a missed compaction -- that costs a
turn of headroom and the next seam retries. It is `/compact` typed onto the end of a
message somebody was half-way through writing. So every ambiguous state must resolve to silence.

CLOSING LINE, not present anywhere: the substring version declared its own author, because
the report explaining the marker quoted it mid-sentence. And the answer is read ONCE and
passed down -- re-reading it in the child made one message answer two ways.

Exercises the real module against synthetic transcripts and a faked pane; nothing here
touches a live tmux server.
"""
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from jstack_host import compact_delivery as cod

HOME = os.path.expanduser("~")
#: How the Stop hook's detached child is really started — `python -m`, the same
#: argv `main()` builds. A test that spawned a file by path would be exercising a
#: seam no install has.
CHILD = [sys.executable, "-m", cod.__name__]

FOOTER = "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
RULE = "─" * 60


@pytest.fixture(autouse=True)
def quarantined_state(tmp_path, monkeypatch):
    """The decision log AND the locks, redirected somewhere disposable — every test here.

    `record()` is called from `main()` and from `run()`, so a suite that drives either
    writes into `dashboard/state/jremote_compaction.jsonl` alongside the real host's rows.
    It did, the first time this ran: `{"sid": "s", "outcome": "sent/continued"}` for a
    session that never existed, in the file whose whole purpose is answering "what did the
    hook actually do". A diagnostic that carries test fixtures is a diagnostic that lies.

    The locks are the same argument, and the per-sid change is what exposed it: every run
    of this suite took real locks in `~/.local/state/jremote`, invisible while they all
    shared one filename and obvious the moment they were named after synthetic sids. The
    env override covers the subprocess children too, which no `setattr` can reach.
    """
    monkeypatch.setenv("JSTACK_COMPACT_LOG", str(tmp_path / "decisions.jsonl"))
    locks = tmp_path / "locks"
    monkeypatch.setenv("JSTACK_COMPACT_LOCK_DIR", str(locks))


def screen(composer="", body="⏺ Building it."):
    """A pane as Claude Code actually draws it — captured live, 2026-09-02."""
    return "\n".join([body, "", RULE, f"❯ {composer}", RULE, "", FOOTER])


#: The three panes that are NOT idle, transcribed from a live claude 2.1.220 in a throwaway
#: tmux server (2026-09-02) — the probe that reproduced what a session was found sitting in.
#: Every one of them keeps the permission footer up and the input box empty, which is why
#: "footer present + empty box" was never readiness. The busy footer even keeps its `⏵⏵`.
WORKING_PANE = screen(body="✳ Pollinating… (5s · ↓ 274 tokens · thinking with xhigh effort)")
COMPACTING_PANE = screen(body="· Compacting conversation…\n  ▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱▱ 11%")

#: What a send into a busy pane actually produces: the line does not run, it QUEUES —
#: rendered as an indented `❯` row above the box, armed to fire on the next Enter.
QUEUED_PANE = "\n".join(["· Compacting conversation…", "", "  ❯ /compact", RULE,
                         "❯ Press up to edit queued messages", RULE, "", FOOTER])


def transcript(path, floor, turns, dupes=3):
    """One message per reading, each written the 2-3 times the client really writes it."""
    with open(path, "w") as fh:
        for i, tokens in enumerate([floor] + list(turns)):
            for _ in range(dupes):
                fh.write(json.dumps({"type": "assistant",
                                     "message": {"id": f"m{i}",
                                                 "usage": {"input_tokens": tokens}}}) + "\n")
    return str(path)


#: The measured cut the hook acts on, restated so a one-sided edit fails here too.
#: `test_load_meter_cuts.py` holds it against the app and the dashboard chip.
HEAVY = 160000


@pytest.fixture
def near_ceiling(tmp_path):
    """A session that has crossed the heavy cut on a light floor — plenty to reclaim."""
    return transcript(tmp_path / "t.jsonl", 30000, [HEAVY - 20000, HEAVY + 10000])


# --- the composer gate: everything ambiguous must resolve to "do not type" -------------

@pytest.mark.parametrize("composer,safe", [
    pytest.param("", True, id="empty-box-is-the-only-safe-state"),
    pytest.param("   ", True, id="whitespace-only-is-still-empty"),
    pytest.param("hey can you also", False, id="somebody-is-mid-message"),
    pytest.param("/", False, id="somebody-opened-the-command-menu"),
])
def test_composer_gate(composer, safe):
    assert cod.pane_is_ready(screen(composer)) is safe


@pytest.mark.parametrize("pane,why", [
    pytest.param(WORKING_PANE, "a turn is running — a send here cuts it short"),
    pytest.param(COMPACTING_PANE, "a compaction is running — a send here queues"),
    pytest.param(QUEUED_PANE, "keys are already queued and waiting for an Enter"),
])
def test_an_empty_box_is_not_an_idle_cli(pane, why):
    """`⏵⏵ bypass permissions on` is a permission-mode banner, not a readiness signal: it
    stays up through a whole turn and a whole compaction, and the box reads empty
    throughout both. The old gate said yes to the first two — a send into either does not
    run, it QUEUES, and sits above the composer pointed at whatever the next Enter lands on
    next. (The third it already refused, but only by accident: the queue replaces the box
    with placeholder text, which read as "somebody is typing".)"""
    assert cod.pane_is_ready(pane) is False, why


#: Every footer the CLI draws in place of the one this hook used to be pinned to, lifted
#: from claude 2.1.220's own strings. shift+tab cycles between them, so every one of them
#: is one keystroke away from any session at any moment.
#:
#: `manual mode on` was missing from this list until 2026-09-25, on the belief that manual
#: mode drew no banner and fell back to "? for shortcuts". It draws one. A live pane on this
#: Mac, idle at an empty box for nine minutes, was reading `no-footer` — see the `ENGINES`
#: comment. An enumeration that is one short is not a weaker check, it is no check at all
#: for the mode it omits, and a session left in that mode could not be compacted again.
@pytest.mark.parametrize("footer", [
    pytest.param("  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents",
                 id="bypass-permissions"),
    pytest.param("  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents", id="auto"),
    pytest.param("  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents",
                 id="accept-edits"),
    pytest.param("  ⏵⏵ plan mode on (shift+tab to cycle) · ← for agents", id="plan"),
    pytest.param("  ⏸ manual mode on · ← for agents", id="manual"),
    pytest.param("  ⏸ manual mode on (shift+tab to cycle) · ← for agents",
                 id="manual-with-the-cycle-hint"),
    pytest.param("  ? for shortcuts", id="shortcuts-hint-instead-of-a-banner"),
    pytest.param("  ⏵⏵ auto mode on (shift+tab to  · ←…", id="truncated-on-a-narrow-pane"),
])
def test_an_idle_pane_is_idle_in_every_permission_mode(footer):
    """The footer proves a live TUI is drawing this pane. WHICH mode it names is not a
    readiness fact, and reading it as one cost 95e01508 every seam it asked for: the person
    shift+tabbed it to auto on 2026-09-15, `bypass permissions on` left the screen, and
    from that keystroke the hook decided `compact`, waited out its whole window against a
    pane it could no longer recognise, and logged `busy` — eleven times, while it compacted
    by hand. `session_stall.py` shares this function and was just as blind."""
    idle = "\n".join(["⏺ Done.", "", RULE, "❯ ", RULE, "", footer])
    assert cod.pane_is_ready(idle) is True


def test_a_pane_with_no_footer_at_all_is_still_not_ready():
    """The other half of the same check, and the reason it cannot just be dropped: a shell
    that has not finished launching its CLI draws no footer, and keys typed there land in
    bash. Broadening the marker must not turn into removing it."""
    assert cod.pane_is_ready("\n".join(["$ claude", "", RULE, "❯ ", RULE])) is False


def test_a_busy_pane_stays_busy_in_the_new_modes_too():
    """Mode is orthogonal to busy. A compaction under the auto-mode footer queues exactly
    as it does under the bypass one, and the new marker must not smuggle in a yes."""
    compacting = "\n".join(["· Compacting conversation…", "", RULE, "❯ ", RULE, "",
                            "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents"])
    assert cod.pane_is_ready(compacting) is False


def test_a_finished_turn_is_not_a_running_one():
    """The same spinner line ends the turn it started: `Swirling… (7m 49s · …)` while it
    runs, `Baked for 6s` once it is done. Matching the word would make the hook permanently
    blind at exactly the moment Stop fires — which is the only moment it ever acts."""
    assert cod.pane_is_ready(screen(body="✻ Baked for 6s · 1 shell still running")) is True
    assert cod.pane_is_ready(screen(body="✻ Cooked for 27m 35s")) is True
    assert cod.pane_is_ready(screen(body="⎿  Running 3 shell commands…")) is True


def test_the_queue_row_is_not_mistaken_for_the_input_box():
    """A queued message draws its own `❯` row above the composer. Reading the first one
    would call the queue the box — and find our own `/compact` in it."""
    assert cod.composer_line(QUEUED_PANE) == "Press up to edit queued messages"


# --- the CLI's own grey text is nobody's typing --------------------------------------

#: The composer row exactly as `capture-pane -e` returns it, lifted off the live session
#: this bug was found in (jr-245ca81c, 2026-09-03). The box is EMPTY; `continue` is the
#: CLI's inline suggestion, drawn in dim (SGR 2), and pressing Enter there sends nothing.
#: Without `-e` this same row captures as the bare string `❯ continue` -- indistinguishable
#: from somebody half-way through typing the word, which is how it was read.
GHOST_ROW = "\x1b[39m❯\xa0\x1b[2mcontinue\x1b[0m"

#: The same box with a person actually in it: they typed `cont`, the CLI ghosts the rest.
HALF_TYPED_ROW = "\x1b[39m❯\xa0cont\x1b[2minue\x1b[0m"


def styled(row, body="⏺ Building it."):
    """A pane whose footer and body carry styling too, as a real capture does."""
    return "\n".join([f"\x1b[2m{body}\x1b[0m", "", RULE, row, RULE, "",
                      f"\x1b[2m{FOOTER}\x1b[0m"])


def test_the_clis_own_suggestion_is_an_empty_box():
    """The failure this pair pins: a session sat at 215,283 tokens with five open tasks and
    was never compacted, because `continue` was on the composer row. nobody typed it --
    it is the CLI's ghost completion, and the guard had no way to know, because a capture
    without `-e` throws away the one thing that distinguishes them."""
    assert cod.composer_line(styled(GHOST_ROW)) == ""
    assert cod.pane_is_ready(styled(GHOST_ROW)) is True


def test_typing_mid_word_is_still_typing():
    """Dropping dim must not drop the solid characters in front of it. They typed `cont`;
    a send here appends `/compact` to their line."""
    assert cod.composer_line(styled(HALF_TYPED_ROW)) == "cont"
    assert cod.pane_is_ready(styled(HALF_TYPED_ROW)) is False


def test_styling_never_hides_a_marker():
    """`pane()` captures styling now, so every marker check reads a stripped copy. A footer
    or a `Compacting conversation…` that went unmatched because a colour code split the
    phrase fails OPEN -- it would type into a pane mid-compaction."""
    assert cod.pane_is_ready(styled(GHOST_ROW, body="\x1b[1m·\x1b[0m Compacting conversa"
                                                    "\x1b[2mtion…\x1b[0m")) is False
    assert cod.pane_is_ready("\n".join([RULE, GHOST_ROW, RULE, "", "⏵⏵ bypass permis"
                                        "\x1b[2msions on\x1b[0m (shift+tab)"])) is True


@pytest.mark.parametrize("colour,what", [
    pytest.param("\x1b[38;5;2m", "256-palette green — its N *is* 2"),
    pytest.param("\x1b[38;5;242m", "256-palette grey, the one Claude Code draws most"),
    pytest.param("\x1b[38;2;120;120;120m", "truecolour — its second parameter is 2"),
    pytest.param("\x1b[48;5;2m", "a background colour, same shape"),
])
def test_a_colour_is_not_an_intensity(colour, what):
    """`38`/`48` take their palette as following parameters, and reading those one at a time
    finds a `2` in `38;5;2` and in every truecolour code there is. Calling that dim marks
    a person's own typing as the CLI's ghost and drops it -- and the send that follows lands on
    the end of their half-written message. Wrong in the expensive direction."""
    assert cod.composer_line(styled(f"\x1b[39m❯\xa0{colour}hey can you also\x1b[0m")) \
        == "hey can you also", what
    assert cod.pane_is_ready(styled(f"\x1b[39m❯\xa0{colour}hey can you also\x1b[0m")) is False


#: What the pane looks like in the ~150s AFTER our `/compact` Enter actually worked: the CLI
#: echoes the slash command as its own `❯` row and stops drawing the input box entirely.
#: Transcribed from jr-245ca81c, 2026-09-03 18:18 -- the run that compacted and never resumed.
ECHOED_COMPACT_PANE = "\n".join(["✻ Crunched for 4m 2s", "", "❯ /compact", "",
                                 "✢ Compacting conversation… (1m 13s · ↑ 8.6k tokens)", "",
                                 FOOTER])


def test_our_own_echo_is_not_a_stuck_send():
    """`composer_line` takes the LAST `❯`, and while a compaction runs that is the CLI's echo
    of the command we sent -- so the box "still holds our text" for the whole two and a half
    minutes. `submit` read that as stuck, wiped it with ^U, and returned False; `run` saw
    `not-taken` and never reached `wait_and_continue`. The compaction went through and the
    continue never did, which is the session parked at its summary with a full docket."""
    assert cod.still_ours(ECHOED_COMPACT_PANE, "/compact") is True, "the echo does read back"
    assert cod.took_effect(ECHOED_COMPACT_PANE) is True, "and it outranks the box"


def test_submit_reports_a_send_that_started_a_compaction(monkeypatch):
    """End of the same defect: the return value `run` branches on."""
    monkeypatch.setattr(cod, "send_text", lambda name, text: None)
    monkeypatch.setattr(cod, "pane", lambda name: ECHOED_COMPACT_PANE)
    monkeypatch.setattr(cod, "clear_line",
                        lambda name: pytest.fail("wiped a send that had already taken"))
    assert cod.submit("jr-x", "/compact") is True


def test_a_genuinely_stuck_line_is_still_wiped(monkeypatch):
    """The guard this must not cost: autocomplete swallows the Enter, nothing runs, and
    `/compact` is left in the box pointed at whatever is typed next."""
    wiped = []
    monkeypatch.setattr(cod, "send_text", lambda name, text: None)
    monkeypatch.setattr(cod, "pane", lambda name: screen("/compact"))
    monkeypatch.setattr(cod, "clear_line", lambda name: wiped.append(name))
    assert cod.submit("jr-x", "/compact") is False
    assert wiped == ["jr-x"]


def test_an_unstyled_capture_is_read_whole():
    """`_typed` can only ever ADD emptiness. Nothing dim means nothing dropped, so every
    synthetic pane above -- and a capture that lost its `-e` -- behaves exactly as before
    rather than silently reading as an empty box."""
    assert cod._typed("❯ hey can you also") == "❯ hey can you also"
    assert cod.composer_line(screen("hey can you also")) == "hey can you also"


@pytest.mark.parametrize("junk,why", [
    pytest.param("", "no pane at all"),
    pytest.param(None, "capture failed / session gone"),
    pytest.param(f"{RULE}\n❯ \n{RULE}", "footer absent — TUI starting or a dialog is up"),
    pytest.param(f"some output\n{FOOTER}", "no input box on screen"),
])
def test_unknown_screen_is_never_treated_as_empty(junk, why):
    """Unknown is not empty. Guessing where the keys go is the failure being prevented."""
    assert cod.pane_is_ready(junk) is False, why


# --- the decision: when a turn-end is close enough to be worth the boundary ------------


def test_quiet_when_the_turn_ended_under_the_heavy_cut(tmp_path, monkeypatch):
    """Under the cut nothing compacts, and a finished delivery there is the quietest case
    there is: nothing to reclaim and nothing parked. The weight is the recorded reason."""
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    monkeypatch.setattr(cod, "compacts_when_done", lambda cwd: True)
    path = transcript(tmp_path / "light.jsonl", 30000, [40000, 50000])
    name, why = cod.decide(path, "ad79f32f-1111", "claude", "/any/seat", False)
    assert name is None and "under the heavy cut" in why


def test_a_light_session_that_parks_is_still_handed_back(tmp_path, monkeypatch):
    """6b2457f0, 2026-09-12, in two assertions — and the reason somebody asked why it had not
    been fixed.

    It declared a seam at 146,143, which is 13,857 under the cut, and closed with a
    `Next Move` line naming its own task #6. The gate read the number as the whole answer
    and skipped in the PARENT, before the child that reads the marker was ever spawned. So
    the session got no boundary — correctly, it had room — and no nudge either, because
    the nudge only ever rode across a boundary. It sat dead with four open task rows,
    and there was no next Stop to reconsider it: the marker means it parked.

    Under the cut is a fact about the CONTEXT. It was never a fact about the WORK.
    """
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    path = transcript(tmp_path / "parked-light.jsonl", 30000, [40000, 146143])
    name, threshold = cod.decide(path, "ad79f32f-1111", "claude", None, True)
    assert name == "jr-ad79f32f", "a session that declared a seam must be handed back"
    assert threshold is None, "and handed back in place — a boundary buys it nothing here"


def test_fires_at_the_heavy_cut(near_ceiling, monkeypatch):
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    name, threshold = cod.decide(near_ceiling, "ad79f32f-1111", "claude", None, True)
    assert name == "jr-ad79f32f", "must address this session's own tmux session by name"
    assert threshold == HEAVY


def test_one_token_past_the_cut_is_the_whole_difference(tmp_path, monkeypatch):
    """The measured cut, pinned from both sides on a mid-run wrap. 159,530 was the exact
    weight of the delivery the old window-derived threshold slept through.

    What crosses the cut is the THRESHOLD, not the name: a parked session is handed back
    on both sides of it, and one token is the whole difference between being handed back
    across a boundary and being handed back where it stands."""
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    quiet = transcript(tmp_path / "before.jsonl", 30000, [140000, 159530])
    assert cod.decide(quiet, "ad79f32f-1111", "claude", None, True) == ("jr-ad79f32f", None)

    over = transcript(tmp_path / "after.jsonl", 30000, [159530, HEAVY])
    assert cod.decide(over, "ad79f32f-1111", "claude", None, True) == ("jr-ad79f32f", HEAVY)


def test_a_session_that_is_not_in_tmux_is_left_alone(near_ceiling, monkeypatch):
    """Only CLI sessions run in tmux. Elsewhere there is nothing to type into."""
    monkeypatch.setattr(cod, "pane", lambda name: None)
    name, why = cod.decide(near_ceiling, "ad79f32f-1111", "claude", None, True)
    assert name is None and "tmux" in why


def test_no_compaction_when_there_is_nothing_to_reclaim(tmp_path, monkeypatch):
    """Context that IS the session's floor comes straight back — churn, not a fix.

    The cut says when to look; `recoverable` says whether it is worth doing, and it is the
    only per-session number here. A compaction lands at the floor plus a near-fixed summary,
    so a heavy floor leaves nothing for the summary to drop.

    The second half is the same lesson as the cut: churn is not worth doing, and a session
    that parked on that floor is still parked. It gets the prompt without the boundary.
    """
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    monkeypatch.setattr(cod, "compacts_when_done", lambda cwd: True)
    thin = transcript(tmp_path / "thin.jsonl", HEAVY - 20000, [HEAVY + 10000])
    name, why = cod.decide(thin, "ad79f32f-1111", "claude", "/any/seat", False)
    assert name is None and "reclaimable" in why

    assert cod.decide(thin, "ad79f32f-1111", "claude", None, True) == ("jr-ad79f32f", None)


def test_the_cut_does_not_move_with_the_clients_window(tmp_path, monkeypatch):
    """The regression guard. Nothing in the decision may consult `autoCompactWindow`:
    a running session never re-reads it, so a threshold derived from it describes a client
    that no longer exists."""
    assert not hasattr(cod, "DELIVERY_MARGIN"), "the window-derived margin is back"
    assert not hasattr(cod.context_ceiling, "trigger_point")
    assert cod.compaction.HEAVY == HEAVY

    monkeypatch.setattr(cod, "pane", lambda name: screen())
    monkeypatch.setenv("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "50000")
    over = transcript(tmp_path / "over.jsonl", 30000, [HEAVY - 5000, HEAVY + 5000])
    assert cod.decide(over, "ad79f32f-1111", "claude", None, True)[1] == HEAVY


# --- the send: four ways it must abort, one way it may fire ---------------------------

@pytest.fixture
def spy(monkeypatch):
    sent = []

    def compacts(name, engine="claude", path=None):
        sent.append(name)
        return True  # the send took — `submit` proved the box cleared

    monkeypatch.setattr(cod, "send_compact", compacts)
    monkeypatch.setattr(cod, "POLL_SECS", 0.01)
    monkeypatch.setattr(cod, "MAX_WAIT_SECS", 0.05)
    monkeypatch.setattr(cod, "SEAM_WAIT_SECS", 0.05)
    return sent


def test_sends_once_the_box_is_clear(near_ceiling, spy, monkeypatch):
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    size = os.path.getsize(near_ceiling)
    assert cod.wait_and_send("jr-x", near_ceiling, 1000, size, "claude") == "sent"
    assert spy == ["jr-x"]


def test_never_types_into_a_message_somebody_is_writing(near_ceiling, spy, monkeypatch):
    """The money test. Typing for longer than the window means no compaction, not a
    compaction appended to their sentence — and the next delivery reconsiders."""
    monkeypatch.setattr(cod, "pane", lambda name: screen("wait, first check the"))
    size = os.path.getsize(near_ceiling)
    assert cod.wait_and_send("jr-x", near_ceiling, 1000, size, "claude") == "busy"
    assert spy == []


def test_a_new_turn_supersedes_the_decision(near_ceiling, spy, monkeypatch):
    """The user answered while we waited. The turn we measured is over and this is stale —
    never send into a session that is working."""
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    size = os.path.getsize(near_ceiling)
    append(near_ceiling, PROMPT)
    assert cod.wait_and_send("jr-x", near_ceiling, 1000, size, "claude") == "superseded"
    assert spy == []


def test_a_reply_landing_supersedes_too(near_ceiling, spy, monkeypatch):
    """An assistant line after Stop means the session is already answering something."""
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    size = os.path.getsize(near_ceiling)
    append(near_ceiling, REPLY)
    assert cod.wait_and_send("jr-x", near_ceiling, 1000, size, "claude") == "superseded"
    assert spy == []


def test_the_clients_own_stop_lines_do_not_supersede(near_ceiling, spy, monkeypatch):
    """The phantom that killed half of all legitimate compactions, pinned.

    The client appends `stop_hook_summary` and `turn_duration` system lines right after
    the Stop hooks return — so judged by SIZE, every transcript "grew" a beat after the
    child started watching, and whether the child's first check beat the client's write
    was a coin flip. Seven compactions in a row died as `superseded` on one session
    (523d4c89, 2026-09-03); each deferred boundary then fired at the NEXT Stop instead —
    a person mid-conversation, watching 40e8265e compact over a question they asked three
    minutes earlier. New bytes are not a new turn; only new words are."""
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    size = os.path.getsize(near_ceiling)
    append(near_ceiling,
           {"type": "system", "subtype": "stop_hook_summary", "hookCount": 2},
           {"type": "system", "subtype": "turn_duration", "durationMs": 137})
    assert cod.wait_and_send("jr-x", near_ceiling, 1000, size, "claude") == "sent"
    assert spy == ["jr-x"]


def test_does_not_compact_what_was_already_compacted(near_ceiling, spy, monkeypatch):
    """The reading fell on its own — the client got there first, or the user ran /compact."""
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    size = os.path.getsize(near_ceiling)
    huge = HEAVY * 10
    assert cod.wait_and_send("jr-x", near_ceiling, huge, size, "claude") == "already-compacted"
    assert spy == []


def test_a_vanished_transcript_aborts(spy, monkeypatch):
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    assert cod.wait_and_send("jr-x", "/nope/gone.jsonl", 1000, 10, "claude") == "gone"
    assert spy == []


# --- the hook itself: it runs on every single turn, so it must never be the thing that
#     breaks one -------------------------------------------------------------------------

@pytest.mark.parametrize("stdin", ["", "not json", "{}", '{"session_id":"x"}',
                                   '{"transcript_path":"/nope.jsonl","session_id":"x"}'])
def test_malformed_or_missing_input_is_silent_not_fatal(stdin):
    p = subprocess.run(CHILD, input=stdin, capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, f"hook died on {stdin!r}: {p.stderr}"
    assert not p.stdout.strip(), "a Stop hook's stdout is surfaced — it must stay quiet"


def test_the_lock_stops_a_second_waiter_on_the_SAME_session():
    """Two deliveries from one session must not both send. The lock is reclaimable so a
    crashed child cannot switch the mechanism off for the rest of the session."""
    first = cod.held("sess-alpha")
    assert first is not None
    assert cod.held("sess-alpha") is None, "a second waiter took the lock while one ran"
    first.close()
    assert cod.held("sess-alpha") is not None, "lock not released — next delivery is dead"


def test_one_sessions_compaction_does_not_block_anothers():
    """The regression that cost another session a seam by three seconds (f89e6b9b behind 6b2457f0,
    2026-09-11). The lock was machine-wide; a holder lives up to BOUNDARY_WAIT_SECS with
    keepalive refreshing it, so an unrelated session's Stop inside that window failed to
    acquire and its child returned having done nothing. Parked with the marker as its
    closing line, it had no next Stop to retry from."""
    mine = cod.held("sess-alpha")
    assert mine is not None
    try:
        theirs = cod.held("sess-gamma")
        assert theirs is not None, "one session's compaction blocked an unrelated session"
        theirs.close()
    finally:
        mine.close()


def test_a_long_wait_does_not_look_like_a_crash():
    """The child now outlives STALE_LOCK_SECS: a compaction alone ran 145s against a 120s
    staleness window. Without the touch, the next delivery reclaims a lock that is still
    held and two children type into one pane."""
    first = cod.held("sess-alpha")
    try:
        os.utime(cod.lock_path("sess-alpha"), (0, 0))  # as old as a crashed lock ever gets
        cod.keepalive()
        assert cod.held("sess-alpha") is None, "a live child's lock was reclaimed mid-wait"
    finally:
        first.close()


def test_contention_is_recorded_not_silent(tmp_path, monkeypatch):
    """Every other path in the hook writes a line; this one returned having written
    nothing, so a dropped delivery was indistinguishable from a child that ran and decided
    against compacting — a dangling `candidate` and no successor."""
    log = tmp_path / "compaction.jsonl"
    monkeypatch.setenv("JSTACK_COMPACT_LOG", str(log))
    cod.record(sid="abc12345", decision="skip",
               reason="this session already has a child deciding this delivery")
    rows = [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]
    assert rows and rows[-1]["decision"] == "skip"
    assert "already has a child" in rows[-1]["reason"]


# --- across the boundary: compacting and stopping there is what left a session parked ------

BOUNDARY = {"type": "system", "subtype": "compact_boundary",
            "compactMetadata": {"preTokens": 224314, "postTokens": 14818}}

#: The four user-role lines `/compact` writes about itself, as the client really writes
#: them (a real transcript, 2026-09-02). None of them is anybody speaking.
ARTIFACTS = [
    {"type": "user", "isCompactSummary": True,
     "message": {"role": "user", "content": "This session is being continued from a…"}},
    {"type": "user", "isMeta": True,
     "message": {"role": "user", "content": "<local-command-caveat>Caveat: …"}},
    {"type": "user",
     "message": {"role": "user", "content": "<command-name>/compact</command-name>"}},
    {"type": "user",
     "message": {"role": "user", "content": "<local-command-stdout>Compacted…"}},
    {"type": "attachment", "timestamp": "2026-09-02T18:43:09.905Z"},
]

PROMPT = {"type": "user", "message": {"role": "user", "content": "actually hold on"}}

#: The row the box writes the instant it takes our `/compact` — and, in a real session, the
#: ONLY thing written between the send and the boundary. Every entry in `ARTIFACTS` is
#: flushed after the boundary line, whatever its own timestamp claims.
SENT = {"type": "user", "message": {"role": "user", "content": "/compact"}}

#: The session answering — an assistant line with WORDS in it. `turn_of` reads a turn off
#: the text, so this is the only assistant shape that counts as the session having spoken.
REPLY = {"type": "assistant", "message": {"id": "m9", "content": [
    {"type": "text", "text": "On it."}]}}

#: An assistant line with no words: a tool call, which is the session working and not the
#: session speaking. It carries usage, because that is the shape whose reading is live
#: while its turn is not — the exact pair the two guards have to tell apart.
TOOL_ONLY = {"type": "assistant", "message": {
    "id": "t1", "content": [{"type": "tool_use", "id": "tu1", "name": "Bash", "input": {}}],
    "usage": {"input_tokens": 190_000}}}


def append(path, *entries):
    """Append transcript lines, returning the offset they begin at — what the child holds."""
    offset = os.path.getsize(path)
    with open(path, "a") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")
    return offset


@pytest.fixture
def docket(tmp_path, monkeypatch):
    """The client's task store, one dir per session."""
    monkeypatch.setattr(cod, "TASKS_DIR", tmp_path / "tasks")

    def write(sid, *statuses):
        d = tmp_path / "tasks" / sid
        d.mkdir(parents=True, exist_ok=True)
        for i, status in enumerate(statuses):
            (d / f"{i}.json").write_text(json.dumps({"id": str(i), "status": status}))
        return sid
    return write


@pytest.mark.parametrize("statuses,state", [
    pytest.param(["in_progress", "completed"], "open", id="mid-task-still-reads-open"),
    pytest.param(["completed", "pending"], "open", id="pending-counts-too"),
    pytest.param(["completed", "completed"], "done", id="worked-its-list-to-the-end"),
    pytest.param([], "unknown", id="a-store-with-nothing-in-it"),
])
def test_the_docket_is_logged_evidence(docket, statuses, state):
    assert cod.docket(docket("sid1", *statuses)) == state


def test_no_task_store_at_all_is_unknown_not_finished(docket):
    """The store is written by 10 sessions out of 2,215, so absence asserts nothing. It is
    logged beside the declaration, never obeyed — a disagreement between the two is the
    first place to look when a boundary lands where it should not have."""
    assert cod.docket("never-had-one") == "unknown"


def test_unreadable_task_files_are_skipped_not_fatal(docket, tmp_path):
    """A row that will not parse is skipped; the readable ones still answer."""
    sid = docket("sid2", "completed")
    (tmp_path / "tasks" / sid / "junk.json").write_text("{not json")
    assert cod.docket(sid) == "done"


def test_a_wholly_unreadable_store_asserts_nothing(docket, tmp_path):
    sid = docket("sid3")
    (tmp_path / "tasks" / sid / "junk.json").write_text("{not json")
    assert cod.docket(sid) == "unknown"


# --- the fork between the two modes: did the session ask to be continued -------------

def closing(tmp_path, name="closing.jsonl", tail=None):
    """A transcript whose closing assistant message says `tail` — heavy, light floor."""
    path = transcript(tmp_path / name, 30000, [HEAVY - 5000, HEAVY + 9000])
    append(path, {"type": "assistant", "message": {"id": "final", "content": [
        {"type": "text", "text": tail if tail is not None
         else f"Parking here — committed and pushed.\n\n{cod.CONTINUE_MARK}"}]}})
    return path


def test_a_declared_wrap_compacts_mid_run(tmp_path, monkeypatch):
    """the mid-run mode, end to end at the gate: the ceiling injection told the session
    to stop, it parked the docket at a seam and closed with the marker, and the boundary
    is taken unconditionally — no switch consulted, nothing else asked."""
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    monkeypatch.setattr(cod, "compacts_when_done",
                        lambda cwd: pytest.fail("mid-run must not consult the switch"))
    path = closing(tmp_path)
    assert cod.asked_to_continue(path, "claude") is True
    assert cod.decide(path, "ad79f32f-1111", "claude", None, True)[0] == "jr-ad79f32f"


def test_silence_is_a_finished_delivery_and_is_left_alone(near_ceiling, monkeypatch):
    """the shipped default, the whole point of the redesign. A heavy turn that ended without
    the marker is finished work, and finished work does not compact — the previous
    design's opposite default is what ghost-compacted 40e8265e over a question they asked
    three minutes earlier, then nudged it into announcing its work was already done."""
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    name, why = cod.decide(near_ceiling, "ad79f32f-1111", "claude", None, False)
    assert name is None and "finished" in why


def test_the_switch_takes_the_boundary_on_a_finished_delivery(near_ceiling, monkeypatch):
    """Compact When Done, per agent, off unless that agent's user turned it on — the only thing that
    compacts a finished delivery, and `wait_and_continue` never resumes one."""
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    monkeypatch.setattr(cod, "compacts_when_done", lambda cwd: True)
    assert cod.decide(near_ceiling, "ad79f32f-1111", "claude", "/any/seat", False)[0] == "jr-ad79f32f"


def test_the_marker_must_be_the_sessions_last_word(tmp_path, monkeypatch):
    """Wrapped on one turn, resumed, then finished — that session is done. Only the
    closing message is read, so an older wrap cannot compact a finished delivery."""
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    path = closing(tmp_path, "then-done.jsonl")
    append(path, {"type": "assistant", "message": {"id": "next", "content": [
        {"type": "text", "text": "All follow-ups closed out. Shipped."}]}})
    assert cod.asked_to_continue(path, "claude") is False
    assert cod.decide(path, "ad79f32f-1111", "claude", None, False)[0] is None


def test_prose_alone_never_asks_to_be_continued(tmp_path):
    """The classifier that was not built, from the other side. These are closing lines a
    working session might really write, and none is a declaration — which is exactly why
    the marker is exact-match."""
    for line in ("More to do — picking this up next turn.",
                 "To be continued.", "Parking here for now.",
                 "I'll carry on after the compaction.", "Continuing shortly."):
        assert cod.asked_to_continue(closing(tmp_path, "prose.jsonl", tail=line), "claude") is False


def test_an_unreadable_transcript_reads_as_finished():
    """Every failure falls to "finished", which leaves the session alone. Being wrong that
    way costs a seam the client's own auto-compact backs up — never a ghost boundary over
    the only copy of a finished conversation, which is the direction that cost the most."""
    assert cod.asked_to_continue("/no/such/transcript.jsonl", "claude") is False


def test_a_report_that_explains_the_marker_declares_nothing(tmp_path, monkeypatch):
    """The regression, in the words that caused it, surviving the inversion.

    The substring version declared its own author: a message ABOUT the mechanism tripped
    the mechanism. Under the inverted marker the same mistake would ghost-compact and
    ghost-resume a finished delivery that merely described the design — every doc, every
    handoff and every review that names the marker is the same message."""
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    said = ("So it's declared, not inferred. A session parking mid-run work ends with "
            f"`{cod.CONTINUE_MARK}`. `context_ceiling.py` hands a heavy session that "
            "instruction in the same message that tells it to end the turn.")
    path = closing(tmp_path, "explained.jsonl", tail=said)
    assert cod.asked_to_continue(path, "claude") is False
    assert cod.decide(path, "ad79f32f-1111", "claude", None, False)[0] is None


def test_the_closing_line_is_what_declares(tmp_path):
    """Backticks count -- `context_ceiling` shows the marker inside them, so a session
    copying the instruction verbatim means it. Trailing blank lines count. A marker with
    prose after it does not: that session carried on talking."""
    for tail, declared in (
            (f"Parking here.\n\n{cod.CONTINUE_MARK}", True),
            (f"Parking here.\n\n`{cod.CONTINUE_MARK}`", True),
            (f"Parking here.\n\n{cod.CONTINUE_MARK}\n\n", True),
            (f"{cod.CONTINUE_MARK}\n\nOne more thing I should flag.", False),
            (f"Pausing — {cod.CONTINUE_MARK} — see you on the other side.", False)):
        path = closing(tmp_path, "closing.jsonl", tail=tail)
        assert cod.asked_to_continue(path, "claude") is declared, tail


def test_a_prompt_newer_than_the_last_reply_means_the_turn_has_not_written_yet(tmp_path):
    """The race, at the level it is detectable. Stop fires at the end of the turn but the
    client writes that assistant line asynchronously, and a read that lands in between
    answers off the PREVIOUS turn's closing message."""
    path = closing(tmp_path, "inflight.jsonl")
    assert cod._closing_message(path, "claude")[1] is False
    append(path, PROMPT)
    assert cod._closing_message(path, "claude")[1] is True, "a real prompt means a reply is owed"


def test_neither_tool_results_nor_the_compactions_own_lines_are_prompts(tmp_path):
    """Both are user-role, neither is anybody speaking. Counting either would make the
    hook wait out its settle on every turn that ended in a tool call."""
    path = closing(tmp_path, "noise.jsonl")
    append(path, {"type": "user", "message": {"content": [{"type": "tool_result"}]}})
    append(path, *ARTIFACTS)
    assert cod._closing_message(path, "claude")[1] is False


def test_a_final_message_still_in_flight_is_waited_for(monkeypatch):
    """One transcript gave two answers 2m21s apart -- one to the parent, another to the
    child -- because the first read happened before the line existed. The settle is what
    makes the two reads agree; it is only ever paid above the heavy cut."""
    answers = [(None, True), (None, True), (f"parking\n{cod.CONTINUE_MARK}", False)]
    monkeypatch.setattr(cod, "_closing_message", lambda path, engine: answers.pop(0))
    monkeypatch.setattr(cod, "SETTLE_POLL", 0)
    assert cod.asked_to_continue("/anything", "claude", settle=5) is True
    assert answers == [], "it stopped reading before the message landed"


def test_the_wait_for_it_is_bounded(monkeypatch):
    """A message that never arrives must not hold Stop past its 10s timeout. Giving up
    answers "finished", which leaves the session alone."""
    monkeypatch.setattr(cod, "_closing_message", lambda path, engine: (None, True))
    monkeypatch.setattr(cod, "SETTLE_POLL", 0)
    started = time.time()
    assert cod.asked_to_continue("/anything", "claude", settle=0.2) is False
    assert time.time() - started < 5, "the settle is not bounded by its own deadline"


def test_nothing_is_waited_for_when_the_reply_is_already_on_file(tmp_path):
    """The common case, and the reason this costs nothing. Nothing pending, no sleep --
    a settle paid on every Stop would be a hook nobody keeps."""
    path = closing(tmp_path, "settled.jsonl")
    started = time.time()
    assert cod.asked_to_continue(path, "claude", settle=30) is True
    assert time.time() - started < 5, "it waited on a transcript that was already complete"


def test_the_child_can_be_started_with_the_arguments_the_parent_sends(near_ceiling):
    """The one seam the tests above cannot see. They call `run_child()` directly, so the
    argv the parent actually builds is exercised by nothing — and the child is detached
    with its output thrown away, so a wrong arity there is a TypeError nobody ever reads
    and a mechanism that silently stops compacting. Adding a flag to that list is exactly
    the edit that breaks it."""
    handoff = {"path": near_ceiling, "sid": "dead-0000", "agent": "",
               "engine": "claude", "size": os.path.getsize(near_ceiling)}
    r = subprocess.run(CHILD + ["--child", json.dumps(handoff)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr, r.stderr


def test_the_parent_never_reads_the_declaration():
    """The 2510c844 regression, pinned at the source. The client writes the turn's closing
    message only AFTER the Stop hooks return — measured: the hook read, decided and logged
    at 15:16:12.0 and the message it was judging landed at 15:16:12.716 — so a parent that
    reads the declaration always reads a transcript the message is missing from, and a
    parent that waits for it may be the very thing the client is waiting on. The read
    belongs to the detached child, once, handed to gate, log and continue alike."""
    import inspect
    src = inspect.getsource(cod.main)
    assert "asked_to_continue" not in src and "settle_declaration" not in src \
        and "_closing_message" not in src, "the parent is judging a message that does not exist yet"
    assert "asked_to_continue" not in inspect.getsource(cod.wait_and_continue), \
        "the continue is re-deriving a decision it was handed"


def test_a_closing_line_that_lands_after_stop_is_still_read(near_ceiling, monkeypatch):
    """The race itself, replayed with real files: the marker lands 0.3s after the child
    starts settling, and the child must find it — a full-file read at Stop time answers
    off a mid-turn status note instead and calls a parked session finished."""
    import threading
    monkeypatch.setattr(cod, "SETTLE_SECS", 3.0)
    append(near_ceiling, {"type": "assistant", "message": {"id": "mid", "content": [
        {"type": "text", "text": "124 green — committing, then parking at the seam."}]}})
    offset = os.path.getsize(near_ceiling)
    timer = threading.Timer(0.3, lambda: append(near_ceiling, {
        "type": "assistant", "message": {"id": "final", "content": [
            {"type": "text", "text": f"Parking here.\n\n{cod.CONTINUE_MARK}"}]}}))
    timer.start()
    try:
        wants, baseline = cod.settle_declaration(near_ceiling, offset, "claude")
    finally:
        timer.join()
    assert wants is True, "the closing line landed after Stop and was not read"
    assert baseline == os.path.getsize(near_ceiling), \
        "the baseline must move past the line just read, or turn_moved calls it a new turn"


def test_a_message_that_never_lands_falls_back_to_whats_on_file(tmp_path, monkeypatch):
    """No text after the offset means the client wrote the closing message BEFORE Stop —
    the newest text on file is that message, and it still answers. Bounded: the child
    gives up after its window rather than holding the lock forever."""
    monkeypatch.setattr(cod, "SETTLE_SECS", 0.3)
    path = closing(tmp_path, "prelanded.jsonl")
    offset = os.path.getsize(path)
    started = time.time()
    wants, baseline = cod.settle_declaration(path, offset, "claude")
    assert wants is True and baseline >= offset
    assert time.time() - started < 5


def test_the_child_skips_a_finished_delivery_without_typing(near_ceiling, monkeypatch):
    """The whole redesign, end to end at the child: closing message lands after Stop with
    no marker, agent not opted in — nothing is sent, and the log says why."""
    monkeypatch.setattr(cod, "SETTLE_SECS", 0.5)
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    monkeypatch.setattr(cod, "wait_and_send",
                        lambda *a: pytest.fail("typed at a finished delivery"))
    offset = os.path.getsize(near_ceiling)
    append(near_ceiling, {"type": "assistant", "message": {"id": "final", "content": [
        {"type": "text", "text": "Shipped and pushed. Standing by."}]}})
    why = cod.run_child(near_ceiling, "ad79f32f-1111", None, "claude", offset)
    assert "finished" in why


def test_the_child_compacts_a_declared_wrap(near_ceiling, monkeypatch):
    """And the marked path fires: the child reads the marker off the line that landed
    after Stop, compacts, and steps across."""
    monkeypatch.setattr(cod, "SETTLE_SECS", 0.5)
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    monkeypatch.setattr(cod, "wait_and_send", lambda *a: "sent")
    monkeypatch.setattr(cod, "wait_and_continue", lambda n, p, o, wants, rows=False, engine="claude":
                        "continued" if wants else pytest.fail("the declaration was lost"))
    offset = os.path.getsize(near_ceiling)
    append(near_ceiling, {"type": "assistant", "message": {"id": "final", "content": [
        {"type": "text", "text": f"Parking at the seam.\n\n{cod.CONTINUE_MARK}"}]}})
    assert cod.run_child(near_ceiling, "ad79f32f-1111", None, "claude", offset) == "sent/continued"


# --- the seam that needs no boundary --------------------------------------------------
#
# Every test below exists because of one session. 6b2457f0 closed a turn at 146,143 with
# `Next Move: Task #6` and the marker, 13,857 under the cut. The parent skipped on the
# number, the child that reads markers was never spawned, and the session sat parked with
# four open task rows until somebody asked why it had not been fixed. The marker is two claims
# and only the first had a path through this file.


@pytest.fixture
def typed(monkeypatch):
    """Everything an in-place continue could type, captured instead of sent."""
    out = []

    def submit(name, text, engine="claude", path=None):
        out.append((name, text))
        return True  # the send took — `submit` proved the box cleared

    monkeypatch.setattr(cod, "submit", submit)
    monkeypatch.setattr(cod, "POLL_SECS", 0.01)
    monkeypatch.setattr(cod, "MAX_WAIT_SECS", 0.05)
    monkeypatch.setattr(cod, "SEAM_WAIT_SECS", 0.05)
    return out


def test_a_parked_session_is_handed_back_without_a_boundary(near_ceiling, typed, monkeypatch):
    """The repair, at its smallest: no `/compact`, no waiting for a boundary, one prompt."""
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    monkeypatch.setattr(cod, "send_compact",
                        lambda name, engine="claude", path=None: pytest.fail("compacted anyway"))
    offset = os.path.getsize(near_ceiling)
    assert cod.continue_in_place("jr-x", near_ceiling, "ad79f32f-1111", offset) \
        == "in-place/continued"
    assert [name for name, _ in typed] == ["jr-x"]


def test_the_in_place_nudge_never_claims_a_compaction_that_did_not_happen(
        near_ceiling, typed, monkeypatch):
    """The two nudges cannot be one string. `CONTINUE` tells the session its context was
    compacted at the seam it picked; on this path nothing was, and a session told it lost
    what it still has goes looking for it — the re-discovery waste this whole mechanism
    exists to cut."""
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    cod.continue_in_place("jr-x", near_ceiling, "ad79f32f-1111",
                          os.path.getsize(near_ceiling))
    (_, text), = typed
    assert "your context was compacted" not in text, \
        "it is telling a session it lost context it still has"
    assert "nothing was compacted" in text, \
        "silence is not enough — a nudge that reads like the other one will be read as it"
    assert "not the user" in text[:60], "it arrives as a user turn and must never read as them"
    assert cod.CONTINUE_IN_PLACE != cod.CONTINUE


def test_the_user_speaking_first_outranks_an_in_place_continue(near_ceiling, typed, monkeypatch):
    """Same rule as the boundary path, and the same reason: a real turn after the Stop
    offset means they are driving."""
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    offset = os.path.getsize(near_ceiling)
    append(near_ceiling, PROMPT)
    assert cod.continue_in_place("jr-x", near_ceiling, "ad79f32f-1111", offset) \
        == "in-place/taken"
    assert typed == []


def test_an_in_place_continue_never_types_over_a_half_written_message(
        near_ceiling, typed, monkeypatch):
    """The money test, restated for the new path. A parked session left alone is a stall
    the sweep picks up; a prompt appended to somebody's sentence is not recoverable."""
    monkeypatch.setattr(cod, "pane", lambda name: screen("wait, first check the"))
    assert cod.continue_in_place("jr-x", near_ceiling, "ad79f32f-1111",
                                 os.path.getsize(near_ceiling)) == "in-place/busy"
    assert typed == []


def test_the_in_place_nudge_points_at_the_rows_when_there_are_rows(
        near_ceiling, typed, monkeypatch):
    """The rows survive a boundary because they are files — and they survive NOT taking
    one for the same reason. The pointer belongs on both nudges."""
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    cod.continue_in_place("jr-x", near_ceiling, "ad79f32f-1111",
                          os.path.getsize(near_ceiling), has_rows=True)
    assert cod.DOCKET_LINE in typed[0][1]


def test_the_child_hands_back_a_light_park_in_place(tmp_path, typed, monkeypatch):
    """End to end at the child, on 6b2457f0's own weight: the marker lands after Stop, the
    session is under the cut, and what comes back is the prompt with no boundary attached."""
    monkeypatch.setattr(cod, "SETTLE_SECS", 0.5)
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    monkeypatch.setattr(cod, "wait_and_send",
                        lambda *a: pytest.fail("compacted a session that had room"))
    path = transcript(tmp_path / "parked-light.jsonl", 30000, [40000, 146143])
    offset = os.path.getsize(path)
    append(path, {"type": "assistant", "message": {"id": "final", "content": [
        {"type": "text", "text": f"**Next Move** Task #6.\n\n{cod.CONTINUE_MARK}"}]}})
    assert cod.run_child(path, "ad79f32f-1111", None, "claude", offset) == "in-place/continued"
    assert len(typed) == 1


def test_the_log_tells_the_two_kinds_of_seam_apart(tmp_path, typed, monkeypatch):
    """`decision` is what "what did the hook actually do" is answered from, and "skip" for
    a session that was handed back would make the fix invisible in its own diagnostic."""
    monkeypatch.setattr(cod, "SETTLE_SECS", 0.5)
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    path = transcript(tmp_path / "parked-light.jsonl", 30000, [40000, 146143])
    offset = os.path.getsize(path)
    append(path, {"type": "assistant", "message": {"id": "final", "content": [
        {"type": "text", "text": f"Parking.\n\n{cod.CONTINUE_MARK}"}]}})
    cod.run_child(path, "ad79f32f-1111", None, "claude", offset)
    rows = [json.loads(line) for line in open(cod.log_path())]
    assert [r for r in rows if r.get("decision") == "continue"], \
        f"the in-place seam is not distinguishable in the log: {rows}"
    assert [r for r in rows if r.get("outcome") == "in-place/continued"]


def test_the_parent_no_longer_ends_the_decision_on_the_weight():
    """The branch that dropped 6b2457f0, pinned at the source.

    The parent cannot know whether the turn parked — the closing line does not exist when
    Stop fires (see `test_the_parent_never_reads_the_declaration`) — so any branch there
    that returns on the number is ending the decision on the one question it is blind to.
    The weight questions belong downstream of the marker, not upstream of it."""
    import inspect
    src = inspect.getsource(cod.main)
    assert "HEAVY" not in src, "the parent is gating on a weight it cannot interpret again"


def test_both_hooks_carry_the_same_marker():
    """The drift that would switch this off silently. `context_ceiling` is where a session
    is told the marker; this file is where it is read. A one-character disagreement means
    every mid-run wrap is missed and nothing anywhere says so."""
    from jstack_host import context_ceiling as cc
    assert cod.CONTINUE_MARK in cc.DECLARE, "the instruction no longer teaches the marker read"
    assert cod.CONTINUE_MARK in cc.HEAVY and cod.CONTINUE_MARK in cc.EXTREME, \
        "a session told to end the turn must be told how to ask for its work back"


def test_the_instruction_separates_stopping_early_from_a_full_backlog():
    """The one way the marker is misread, pinned so a reword cannot lose it.

    b7e61868 (2026-09-04 16:01) delivered, committed three shas, pushed and wrote its
    report — then closed with the marker because the report's `Next Move` block named two
    open threads. The hook obeyed the declaration it was given, compacted a finished
    conversation, and the resumed session answered "nothing left". The instruction said
    "nothing waiting on you", which a backlog answers. It has to ask about THIS TURN.
    """
    from jstack_host import context_ceiling as cc
    said = cc.DECLARE.lower()
    assert "resumed in this run" in said, \
        "the marker must say it asks for a resume HERE, not that work exists somewhere"
    assert "stopped early" in said, "the test the session applies must be about this turn"
    assert "next move" in said, \
        "the block that names later work must be named as NOT a park -- it is what tripped"


def test_the_ceiling_no_longer_teaches_the_retired_done_marker():
    """`<!-- delivered -->` was the old, inverted marker. A session still being taught it
    would write it in good faith and be read as silent — harmless — but the instruction
    surviving anywhere means two contradictory protocols are live at once."""
    from jstack_host import context_ceiling as cc
    for text in (cc.DECLARE, cc.HEAVY, cc.EXTREME):
        assert "<!-- delivered -->" not in text


def test_the_decision_does_not_infer_what_it_must_be_told():
    """Pinned at the source. The task store had an opinion about 10 of 2,215 transcripts
    and gating on it made this mechanism dead for every session a person ran; re-reading it
    here would pass every test above by accident on a store that happens to be empty."""
    import inspect
    src = inspect.getsource(cod.decide)
    assert "docket" not in src, "the store that knows about 0.5% of sessions is back"
    assert "wants_resume" in src and "compacts_when_done" in src
    assert "wants_resume" in inspect.signature(cod.decide).parameters, \
        "the declaration must be handed in, so the log and the child quote the same read"


# --- reading the landing --------------------------------------------------------------

def test_waiting_while_the_compaction_runs(near_ceiling):
    """142s of it, on the session this was built for. Nothing on file yet."""
    assert cod.resume_state(near_ceiling, os.path.getsize(near_ceiling), "claude") == "waiting"


def test_landed_once_the_boundary_is_written(near_ceiling):
    offset = append(near_ceiling, BOUNDARY, *ARTIFACTS)
    assert cod.resume_state(near_ceiling, offset, "claude") == "landed"


def test_the_compactions_own_lines_are_not_somebody_speaking(near_ceiling):
    """The summary, the caveat, the echoed command and its stdout all arrive with role
    user. Counting any one of them as the user would abort every continue there will ever be."""
    for artifact in ARTIFACTS:
        offset = append(near_ceiling, BOUNDARY, artifact)
        assert cod.resume_state(near_ceiling, offset, "claude") == "landed", artifact


def test_a_real_turn_after_the_boundary_takes_the_session_back(near_ceiling):
    offset = append(near_ceiling, BOUNDARY, *ARTIFACTS, PROMPT)
    assert cod.resume_state(near_ceiling, offset, "claude") == "taken"


def test_a_reply_after_the_boundary_also_takes_it(near_ceiling):
    """The user typed and the session is already answering — never type into a working pane."""
    offset = append(near_ceiling, BOUNDARY, REPLY)
    assert cod.resume_state(near_ceiling, offset, "claude") == "taken"


def test_a_turn_before_the_boundary_is_not_the_session_being_taken(near_ceiling):
    """Only what follows the landing counts. The lines our own `/compact` was typed
    after are history."""
    offset = append(near_ceiling, PROMPT, BOUNDARY, *ARTIFACTS)
    assert cod.resume_state(near_ceiling, offset, "claude") == "landed"


def test_an_unreadable_transcript_never_types():
    assert cod.resume_state("/nope/gone.jsonl", 0, "claude") == "taken"


# --- the continue ---------------------------------------------------------------------

@pytest.fixture
def nudged(monkeypatch):
    sent = []

    def continues(name, has_rows=False, engine="claude", path=None):
        sent.append((name, has_rows) if has_rows else name)
        return True

    monkeypatch.setattr(cod, "send_continue", continues)
    monkeypatch.setattr(cod, "POLL_SECS", 0.01)
    monkeypatch.setattr(cod, "MAX_WAIT_SECS", 0.05)
    monkeypatch.setattr(cod, "SEAM_WAIT_SECS", 0.05)
    monkeypatch.setattr(cod, "BOUNDARY_WAIT_SECS", 0.5)
    return sent


def test_the_nudge_points_a_resumed_session_at_rows_it_still_has(docket, monkeypatch):
    """The half of the carry that is not a reconstruction.

    A summary is written from the conversation and loses what it does not think to keep.
    The task store is not in the context window at all -- it is files under
    `~/.claude/tasks/<sid>/`, so the boundary cannot touch them. That makes the rows the
    only part of the plan that crosses at full fidelity, and until this line the session
    on the far side was never told they were still there.
    """
    typed = []
    monkeypatch.setattr(cod, "submit",
                        lambda name, text, engine="claude", path=None:
                        typed.append(text) or True)

    assert cod.send_continue("jr-x", cod.docket(docket("s", "in_progress")) == "open")
    assert cod.DOCKET_LINE in typed[-1]
    assert typed[-1].startswith("[compact-on-delivery hook, not the user]")


@pytest.mark.parametrize("statuses", [
    pytest.param(["completed"], id="worked-its-list-to-the-end"),
    pytest.param([], id="never-used-the-task-tools"),
])
def test_a_session_with_no_open_rows_is_not_sent_looking_for_them(docket, monkeypatch,
                                                                  statuses):
    """Conditional, because an unconditional pointer is a lie most of the time -- 12 of
    42 session dirs on this Mac hold any rows at all. Sending a session after an empty
    store teaches it to distrust the rest of the nudge, which is the part that works."""
    typed = []
    monkeypatch.setattr(cod, "submit",
                        lambda name, text, engine="claude", path=None:
                        typed.append(text) or True)

    assert cod.send_continue("jr-x", cod.docket(docket("s", *statuses)) == "open")
    assert cod.DOCKET_LINE not in typed[-1]
    assert typed[-1] == cod.CONTINUE


def test_the_store_is_read_once_and_the_answer_passed_down(near_ceiling, monkeypatch):
    """One read, one answer -- the rule the declaration already lives by, for the same
    reason. `run_child` asks `docket()` for the log; the nudge must quote THAT answer
    rather than asking again, because a store that changes between two reads produces a
    nudge promising rows the session cannot find."""
    reads = []
    monkeypatch.setattr(cod, "docket",
                        lambda sid, path=None, engine="claude": reads.append(sid) or "open")
    monkeypatch.setattr(cod, "SETTLE_SECS", 0.5)
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    monkeypatch.setattr(cod, "wait_and_send", lambda *a: "sent")
    monkeypatch.setattr(cod, "wait_and_continue",
                        lambda n, p, o, wants, rows=False, engine="claude":
                        "continued" if rows else
                        pytest.fail("the docket answer was dropped on the way down"))
    offset = os.path.getsize(near_ceiling)
    append(near_ceiling, {"type": "assistant", "message": {"id": "final", "content": [
        {"type": "text", "text": f"Parking.\n\n{cod.CONTINUE_MARK}"}]}})

    assert cod.run_child(near_ceiling, "ad79f32f-1111", None, "claude", offset) == "sent/continued"
    assert len(reads) == 1, f"the store was read {len(reads)} times, not once"


def test_the_landing_is_read_off_the_boundary_not_off_the_reading(near_ceiling, nudged,
                                                                  monkeypatch):
    """The regression that would have shipped a continue which never fires.

    A compaction writes no assistant turn, so `context_ceiling.scan` — which reads usage
    off assistant lines — still reports the pre-compact weight until the session next
    replies. Here the boundary is on file, the session is at 15k, and the reading is still
    the 170k it weighed before. Anything gated on the number sees no change, forever.
    """
    offset = append(near_ceiling, BOUNDARY, *ARTIFACTS)
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    assert cod.reading(near_ceiling) > HEAVY, "fixture no longer pins the stale reading"
    assert cod.wait_and_continue("jr-x", near_ceiling, offset, True) == "continued"
    assert nudged == ["jr-x"]


def test_a_switch_boundary_never_resumes(near_ceiling, nudged, monkeypatch):
    """What Compact When Done means: completely done, will not resume unless the user
    says something new. The boundary that switch took has nothing waiting behind it, so
    nothing is typed — and no ghost turn announces work that was already finished."""
    offset = append(near_ceiling, BOUNDARY, *ARTIFACTS)
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    assert cod.wait_and_continue("jr-x", near_ceiling, offset, False) == "delivered"
    assert nudged == []


def test_every_delivery_leaves_a_line_saying_what_it_decided(near_ceiling, nudged, docket,
                                                             monkeypatch, tmp_path):
    """The gap that let "it's fixed" stand for a day on a mechanism that had never fired.

    Every path in this hook ends in silence by design, so nothing on disk said whether a
    delivery was even considered. "Why didn't it compact" could only be answered by
    re-running `decide()` by hand — which is how a fix verified against the one session
    that hit the narrow path got reported as working for all of them.
    """
    offset = append(near_ceiling, BOUNDARY, *ARTIFACTS)
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    monkeypatch.setattr(cod, "wait_and_send", lambda *a: "sent")
    monkeypatch.setattr(cod, "wait_and_continue", lambda *a: "continued")
    cod.run("jr-x", near_ceiling, docket("s", "in_progress"), HEAVY, offset, True)

    rows = [json.loads(ln) for ln in open(cod.log_path()).read().splitlines() if ln.strip()]
    assert rows and rows[-1]["outcome"] == "sent/continued"
    assert rows[-1]["ts"], "a decision with no timestamp cannot be lined up against anything"


def test_the_log_never_takes_the_compaction_down_with_it(monkeypatch):
    """A full disk costs the diagnostic, never the boundary. Every caller is unguarded."""
    monkeypatch.setenv("JSTACK_COMPACT_LOG", "/proc/nonexistent/nowhere/decisions.jsonl")
    cod.record(sid="s", outcome="sent/continued")  # must not raise


def test_the_continue_claims_only_what_every_caller_knows():
    """It is only ever sent to a session that closed with the marker, so it may say so —
    but it must not assert a task list (nobody checked one) and it must keep the exit for
    a session whose summary turns out to read as finished."""
    assert "task list" not in cod.CONTINUE
    assert cod.CONTINUE.startswith("[compact-on-delivery hook, not the user]")
    assert "asking to be continued" in cod.CONTINUE
    assert "inventing work" in cod.CONTINUE


def test_never_nudges_over_a_message_the_user_is_writing(near_ceiling, nudged, monkeypatch):
    """Same money test as the compaction's, one phase later — and a likelier collision,
    because the boundary lands minutes after the turn ended rather than seconds."""
    offset = append(near_ceiling, BOUNDARY, *ARTIFACTS)
    monkeypatch.setattr(cod, "pane", lambda name: screen("wait, first check the"))
    assert cod.wait_and_continue("jr-x", near_ceiling, offset, True) == "busy"
    assert nudged == []


def test_the_user_getting_there_first_ends_it(near_ceiling, nudged, monkeypatch):
    offset = append(near_ceiling, BOUNDARY, *ARTIFACTS, PROMPT)
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    assert cod.wait_and_continue("jr-x", near_ceiling, offset, True) == "taken"
    assert nudged == []


def test_a_compaction_that_never_lands_gives_up_quietly(near_ceiling, nudged, monkeypatch):
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    offset = os.path.getsize(near_ceiling)
    assert cod.wait_and_continue("jr-x", near_ceiling, offset, True) == "no-boundary"
    assert nudged == []


def test_the_artifacts_it_writes_are_the_proof_it_is_still_running(near_ceiling):
    """The count is the signal `wait_and_continue` had no way to ask for.

    A compaction writes nothing to the transcript for its whole run EXCEPT the lines it
    writes about itself, so those lines are the only in-band evidence that the `/compact`
    we typed is being executed at all -- which is the difference between a delay and a
    drop, and the difference the flat budget could not see.
    """
    offset = append(near_ceiling, *ARTIFACTS[1:2])
    assert cod.resume_scan(near_ceiling, offset, "claude") == ("waiting", 1)
    append(near_ceiling, *ARTIFACTS[2:])
    assert cod.resume_scan(near_ceiling, offset, "claude") == ("waiting", 3)
    append(near_ceiling, BOUNDARY, ARTIFACTS[0])
    assert cod.resume_scan(near_ceiling, offset, "claude") == ("landed", 4)
    append(near_ceiling, PROMPT)
    assert cod.resume_scan(near_ceiling, offset, "claude") == ("taken", 4)


def test_the_command_we_typed_is_a_sign_of_life(near_ceiling):
    """b2e6524a, 2026-09-24, and the reason the budget restart has never once fired.

    `ARTIFACTS` is what the previous fix counted, and a real client writes ALL of it after
    the boundary: in that session's transcript the caveat and the echoed command carry
    00:20:32 timestamps and sit BELOW the 00:22:49 boundary line in the file. So between
    the send and the boundary the only row on disk is the submitted command — and it was
    skipped, because `turn_of` reads it as a user turn like anything a person types.
    `signs` stayed 0 for every compaction ever run and `BOUNDARY_WAIT_SECS` was a flat
    budget from the send: 17:16:07 sent into a busy pane, 17:20:32 taken, 17:21:08
    `sent/no-boundary`, 17:22:49 the boundary, on a compaction that worked.
    """
    offset = append(near_ceiling, SENT)
    assert cod.resume_scan(near_ceiling, offset, "claude") == ("waiting", 1), \
        "the only row a running compaction writes has to count as one"
    append(near_ceiling, BOUNDARY, ARTIFACTS[0])
    assert cod.resume_scan(near_ceiling, offset, "claude") == ("landed", 2)
    append(near_ceiling, PROMPT)
    assert cod.resume_scan(near_ceiling, offset, "claude") == ("taken", 2), \
        "a real turn after the boundary still ends it"


def test_a_queued_send_does_not_eat_the_boundary_budget(near_ceiling, nudged, monkeypatch):
    """The whole failure, driven through the real scanner — no stubbed `resume_scan`.

    The test that was supposed to cover this monkeypatched the counter to rise on its own,
    so it passed against a function that in production returned 0 forever. Here the rows
    are written to a real file and the count comes off them, and the pane holds the command
    QUEUED for three times the flat window before taking it.
    """
    appended = []
    monkeypatch.setattr(cod, "pane",
                        lambda name: QUEUED_PANE if not appended else screen())
    monkeypatch.setattr(cod, "BOUNDARY_WAIT_SECS", 0.4)
    monkeypatch.setattr(cod, "MAX_BOUNDARY_WAIT_SECS", 30)
    monkeypatch.setattr(cod, "POLL_SECS", 0.05)
    offset = os.path.getsize(near_ceiling)
    start = time.time()
    real_scan = cod.resume_scan

    def scan(path, off, engine="claude"):
        elapsed = time.time() - start
        if elapsed > 1.2 and not appended:
            append(near_ceiling, SENT)      # the box finally takes it, past the window
            appended.append(elapsed)
        # Relative to the take, not to `start`: the renewed budget begins when the
        # first sign lands, so a second absolute threshold 0.4s later leaves the
        # boundary to arrive in the last poll before the deadline — which it does
        # or does not depending on how loaded the machine is. Under the full suite
        # it did not, and a flaky gate on main is a gate nobody reads.
        elif len(appended) == 1 and elapsed > appended[0] + 0.1:
            append(near_ceiling, BOUNDARY)  # and the compaction finishes
            appended.append(elapsed)
        return real_scan(path, off, engine)

    monkeypatch.setattr(cod, "resume_scan", scan)
    assert cod.wait_and_continue("jr-x", near_ceiling, offset, True) == "continued"
    assert nudged == ["jr-x"]


def test_a_slow_compaction_still_gets_its_continue(near_ceiling, nudged, monkeypatch):
    """6c467018, 2026-09-23. The send landed at 12:17:38, the CLI wrote the command's
    caveat at 12:18:25, and the compaction did not actually run until 12:21:18 -- finishing
    at 12:23:02. One flat 300s from the send had to cover all of that; the wait to START
    ate 220s of it, the child logged `sent/no-boundary` at 12:22:39, and the boundary
    arrived 23 seconds after there was anything left to step across it. the user found the
    session sitting at its own summary with the marker unanswered.

    So the budget counts from the last sign of life. Here the signs keep landing well past
    the flat window and the boundary comes after it -- the exact shape that was dropped.
    """
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    start, calls = time.time(), []

    def scan(path, offset, engine="claude"):
        calls.append(1)
        elapsed = time.time() - start
        if elapsed > cod.BOUNDARY_WAIT_SECS * 4:
            return "landed", 9
        return "waiting", int(elapsed / (cod.BOUNDARY_WAIT_SECS / 2)) + 1

    monkeypatch.setattr(cod, "resume_scan", scan)
    assert cod.wait_and_continue("jr-x", near_ceiling, 0, True) == "continued"
    assert nudged == ["jr-x"]


def test_an_aborted_compaction_is_taken_back_out_of_the_box(near_ceiling, nudged, monkeypatch):
    """6c467018, and the reason it looked from outside like the send never fired.

    Escape during a `/compact` does not just cancel it -- the CLI hands the command back to
    the composer, where typing ARGUES it rather than replacing it: the next thing the person
    writes becomes `/compact <their sentence>`. Measured on 2.1.280 in a throwaway session.
    The child used to sit out its whole window waiting for a boundary that was never
    coming, with that box armed the entire time.
    """
    wiped = []
    monkeypatch.setattr(cod, "clear_line", lambda name: wiped.append(name))
    monkeypatch.setattr(cod, "pane", lambda name: screen(composer="/compact"))
    assert cod.wait_and_continue("jr-x", near_ceiling, 0, True) == "aborted"
    assert wiped == ["jr-x"]
    assert nudged == []


def test_typing_into_the_armed_box_makes_it_their_line(near_ceiling, nudged, monkeypatch):
    """The money test, and the reason the match is exact. The instant they type, the box
    reads `/compact hello there` -- damaged, but THEIRS. Wiping it there would delete
    their words to clean up our mess, which is strictly worse than leaving the command in."""
    wiped = []
    monkeypatch.setattr(cod, "clear_line", lambda name: wiped.append(name))
    monkeypatch.setattr(cod, "pane", lambda name: screen(composer="/compact hello there"))
    assert cod.wait_and_continue("jr-x", near_ceiling, 0, True) == "no-boundary"
    assert wiped == []
    assert nudged == []


def test_a_running_compaction_is_not_an_aborted_one(near_ceiling):
    """On 2.1.280 a live compaction keeps drawing the box, EMPTY, with the echoed
    `❯ /compact` above it and `Compacting conversation…` on screen. Reading the echo as a
    re-armed composer would wipe a compaction that was working perfectly."""
    live = screen(composer="", body="❯ /compact\n✽ Compacting conversation… (20s)")
    assert cod.was_aborted(live, cod.COMPACT_CMD) is False
    assert cod.was_aborted(screen(composer="/compact"), cod.COMPACT_CMD) is True


def test_the_cli_argument_hint_is_screen_text_not_typed_text():
    """Caught by the first real-pane run of `was_aborted`, which returned False on a
    session that had just been aborted. The box reads `/compact  <optional custom
    summarization instructions>` and the hint is a 256-palette grey (`38;5;241`), not SGR
    2 -- so `_typed`, which splits on DIMNESS, keeps it, and the bare-command comparison
    missed. a person's own words are never in angle brackets, so stripping a trailing `<...>`
    cannot swallow them."""
    armed = "/compact  <optional custom summarization instructions>"
    assert cod.was_aborted(screen(composer=armed), cod.COMPACT_CMD) is True
    assert cod.was_aborted(screen(composer="/compact hello there"), cod.COMPACT_CMD) is False


def test_the_restart_cannot_keep_a_child_alive_forever(near_ceiling, nudged, monkeypatch):
    """A budget that restarts on evidence needs a ceiling, or a CLI that keeps writing
    about itself owns a python process for the rest of the machine's uptime."""
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    monkeypatch.setattr(cod, "MAX_BOUNDARY_WAIT_SECS", 0.2)
    signs = itertools.count(1)
    monkeypatch.setattr(cod, "resume_scan", lambda p, o, engine="claude": ("waiting", next(signs)))
    began = time.time()
    assert cod.wait_and_continue("jr-x", near_ceiling, 0, True) == "no-boundary"
    assert time.time() - began < cod.BOUNDARY_WAIT_SECS * 4
    assert nudged == []


def test_the_nudge_cannot_be_mistaken_for_the_user(near_ceiling):
    """It arrives as a user turn. A bare "continue" would leave a transcript claiming the user
    asked for something nobody asked for — worse than a misplaced `/compact`, because
    that one has no voice."""
    assert cod.CONTINUE.startswith("[compact-on-delivery hook, not the user]")
    assert "nobody has said anything new" in cod.CONTINUE
    assert "stop rather than inventing work" in cod.CONTINUE, "no exit for a done session"


def test_the_two_phases_run_under_one_lock(near_ceiling, nudged, monkeypatch):
    """The continue is the back half of the same decision. A second delivery must not be
    able to start its own half of it while this one is waiting out the compaction."""
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    sent = []

    def compacts(name, engine="claude", path=None):  # the client writes the boundary; the child watches
        sent.append(name)
        append(near_ceiling, BOUNDARY, *ARTIFACTS)
        return True

    monkeypatch.setattr(cod, "send_compact", compacts)
    size = os.path.getsize(near_ceiling)
    assert cod.run("jr-x", near_ceiling, "sid-x", 1000, size, True) == "sent/continued"
    assert sent == ["jr-x"] and nudged == ["jr-x"]


def test_a_compaction_that_never_fired_never_continues(near_ceiling, spy, nudged,
                                                       monkeypatch):
    """Every abort in the first phase is also an abort in the second."""
    monkeypatch.setattr(cod, "pane", lambda name: screen("someone is typing"))
    assert cod.run("jr-x", near_ceiling, "sid-x", 1000,
                   os.path.getsize(near_ceiling), True) == "busy"
    assert spy == [] and nudged == []


# --- how long a promise is waited out --------------------------------------------------


def test_a_declared_seam_outlasts_the_short_wait(near_ceiling, spy, monkeypatch):
    """The regression, and the whole of it. Every `busy` row the real decision log has ever
    carried — 244ca668, 27df58cc, fe02ed56, 2dec5785 — sits directly under a `resume: true`
    decision: the pane was unready for twenty seconds, the child walked away, and a session
    that had asked to be carried sat at its seam. It ends no further turn, so the Stop that
    twenty seconds counts on to retry never fires."""
    monkeypatch.setattr(cod, "MAX_WAIT_SECS", 0.02)
    panes = itertools.chain([screen("half a sentence")] * 6, itertools.repeat(screen()))
    monkeypatch.setattr(cod, "pane", lambda name: next(panes))
    size = os.path.getsize(near_ceiling)
    assert cod.wait_and_send("jr-x", near_ceiling, 1000, size, "claude", 5.0) == "sent"
    assert spy == ["jr-x"]


def test_the_budget_is_the_promise_the_decision_made(near_ceiling, monkeypatch):
    """Patience is the seam's, not every delivery's: a finished turn nobody is waiting on
    still gets the short window, because for it the next Stop really does reconsider."""
    seen = []
    monkeypatch.setattr(cod, "wait_and_send",
                        lambda *args: seen.append(args[-1]) or "gone")
    size = os.path.getsize(near_ceiling)
    cod.run("jr-x", near_ceiling, "sid-x", 1000, size, True)
    cod.run("jr-x", near_ceiling, "sid-x", 1000, size, False)
    assert seen == [cod.SEAM_WAIT_SECS, cod.MAX_WAIT_SECS]


@pytest.mark.parametrize("captured,gate", [
    pytest.param(screen(), "", id="ready"),
    pytest.param(WORKING_PANE, "working", id="a-turn-is-running"),
    pytest.param(COMPACTING_PANE, "busy-mark: Compacting conversation", id="compacting"),
    pytest.param("\n".join(["⏺ done", RULE, "❯ Press up to edit queued messages", RULE,
                            "", FOOTER]),
                 "busy-mark: Press up to edit queued messages", id="holding-a-queue"),
    pytest.param(screen("half a sentence"),
                 f"composer-holds: {len('half a sentence')} chars", id="somebody-typing"),
    pytest.param("\n".join(["⏺ done", RULE, "❯ ", RULE]), "no-footer", id="no-live-tui"),
    pytest.param(None, "no-pane", id="no-session"),
])
def test_why_not_ready_names_the_gate_that_refused(captured, gate):
    """The label and the readiness answer come from the same walk, in the same order — a
    gate added to one and not the other is a `busy` nobody can explain."""
    assert cod.why_not_ready(captured) == gate
    assert cod.pane_is_ready(captured) == (gate == "")


def test_the_outcome_line_says_what_the_pane_refused_on(near_ceiling, spy, monkeypatch):
    """`busy` named the outcome and never the cause, so every one of the four cost an
    afternoon of reading transcripts by hand to guess at a screen nobody kept."""
    monkeypatch.setattr(cod, "pane", lambda name: screen("wait, first check the"))
    size = os.path.getsize(near_ceiling)
    assert cod.run("jr-x", near_ceiling, "sid-x", 1000, size, True) == "busy"
    rows = [json.loads(line) for line in
            open(os.environ["JSTACK_COMPACT_LOG"]).read().splitlines() if line.strip()]
    assert rows[-1]["outcome"] == "busy"
    assert rows[-1]["blocked"].startswith("composer-holds:")
    assert rows[-1]["waited"] >= 0


def test_the_note_never_lands_on_a_later_outcome(near_ceiling, spy, monkeypatch):
    """One note, one outcome. A stale `blocked` on a delivery that worked would be a
    diagnostic lying about the one thing it exists to explain."""
    monkeypatch.setattr(cod, "pane", lambda name: screen("wait, first check the"))
    cod.run("jr-x", near_ceiling, "sid-x", 1000, os.path.getsize(near_ceiling), True)
    assert cod.block_note() == {}


# --- the send has to prove it landed --------------------------------------------------

@pytest.fixture
def keys(monkeypatch):
    """Record every tmux call the hook makes, and drive the box it is watching."""
    typed, box = [], {"text": ""}

    def fake_send_text(name, text):
        typed.append(("text", name, text))
        box["text"] = box["sticks"] if "sticks" in box else ""

    def fake_clear(name):
        typed.append(("clear", name))
        box["text"] = ""

    monkeypatch.setattr(cod, "send_text", fake_send_text)
    monkeypatch.setattr(cod, "clear_line", fake_clear)
    monkeypatch.setattr(cod, "pane", lambda name: screen(box["text"]))
    monkeypatch.setattr(cod, "POLL_SECS", 0.01)
    monkeypatch.setattr(cod, "SUBMIT_CHECK_SECS", 0.05)
    return typed, box


def test_a_send_that_took_leaves_the_box_alone(keys):
    typed, _ = keys
    assert cod.submit("jr-x", "/compact") is True
    assert typed == [("text", "jr-x", "/compact")], "nothing to clean up, so no ^U"


def test_a_slash_command_left_in_the_box_is_taken_back_out(keys):
    """An Enter is not a submission. Typing `/compact` opens the CLI's autocomplete — three
    entries on this Mac now that superpowers ships two completion skills — and a pane that
    turns out to be busy holds the line instead of running it. Left there it is a loaded
    gun: the next Enter is the user's, and it fires a compaction they never asked for."""
    typed, box = keys
    box["sticks"] = "/compact"
    assert cod.submit("jr-x", "/compact") is False
    assert typed[-1] == ("clear", "jr-x")


def test_a_failed_send_is_reported_as_not_sent(near_ceiling, keys, monkeypatch):
    """And the caller must not go on to wait for a boundary that is never coming."""
    _, box = keys
    box["sticks"] = "/compact"
    monkeypatch.setattr(cod, "MAX_WAIT_SECS", 0.05)
    monkeypatch.setattr(cod, "SEAM_WAIT_SECS", 0.05)
    size = os.path.getsize(near_ceiling)
    assert cod.wait_and_send("jr-x", near_ceiling, 1000, size, "claude") == "not-taken"


def test_it_never_wipes_what_the_user_typed_into_the_gap(keys):
    """The box is only cleared when it is holding OUR text. A person starting a message in the
    second after the Enter keeps those keystrokes — the whole point of this hook's caution."""
    typed, box = keys
    box["sticks"] = "no wait, first check"
    cod.submit("jr-x", "/compact")
    assert ("clear", "jr-x") not in typed
    assert box["text"] == "no wait, first check", "their line survives untouched"


def test_a_session_that_vanished_mid_send_is_not_chased(keys, monkeypatch):
    typed, box = keys
    box["sticks"] = "/compact"
    monkeypatch.setattr(cod, "pane", lambda name: None)
    assert cod.submit("jr-x", "/compact") is False
    assert ("clear", "jr-x") not in typed, "no session, nothing to type ^U at"


# --- never twice: the guard the reading could not be ----------------------------------

def test_a_boundary_newer_than_the_last_reply_stops_a_second_compaction(near_ceiling,
                                                                        monkeypatch):
    """The hole this closes. `reading()` comes off assistant lines and a compaction writes
    none, so ours leaves the number reporting the pre-compact weight — 202,643 on a session
    now sitting at 10,591 — until the session next replies. Any Stop in that window clears
    the heavy cut on a stale figure and sends a SECOND `/compact` at a session that was
    just compacted. `decide` must read the boundary, not the number."""
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    assert cod.decide(near_ceiling, "ad79f32f-1111", "claude", None, True)[0] is not None, \
        "fixture must fire"

    append(near_ceiling, BOUNDARY, *ARTIFACTS)
    assert cod.reading(near_ceiling) > HEAVY, "the stale reading is the whole hazard"
    name, why = cod.decide(near_ceiling, "ad79f32f-1111", "claude", None, True)
    assert name is None and "newest thing on file" in why


def test_the_session_speaking_again_makes_the_reading_live(near_ceiling,
                                                           monkeypatch):
    """The guard is 'newer than the last reply', not 'ever compacted'. A session that has
    compacted and then worked its way back over the cut must be compactable again."""
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    append(near_ceiling, BOUNDARY, *ARTIFACTS)
    append(near_ceiling, {"type": "assistant", "message": {
        "id": "after", "usage": {"input_tokens": HEAVY + 9000},
        "content": [{"type": "text", "text": "Back over the cut."}]}})
    assert cod.decide(near_ceiling, "ad79f32f-1111", "claude", None, True)[0] is not None


def test_a_tool_call_after_the_boundary_is_not_the_session_speaking(near_ceiling,
                                                                    monkeypatch):
    """Working is not speaking, and the reading alone does not clear the guard.

    A tool-only assistant line carries usage, so `reading()` sees the session climb back
    over the cut while `freshly_compacted` still reports a boundary newer than anything
    said. The pair looks like a missed seam and is not one: the hook runs at Stop, and a
    turn cannot end without a closing message, so by the time `decide` is asked the session
    has always spoken. Pinned because the opposite reading — counting a tool call as a turn
    — is what would let a second `/compact` fire on the stale pre-boundary figure.
    """
    monkeypatch.setattr(cod, "pane", lambda name: screen())
    append(near_ceiling, BOUNDARY, *ARTIFACTS, TOOL_ONLY)
    assert cod.freshly_compacted(near_ceiling, "claude") is True
    assert cod.reading(near_ceiling, "claude") == 190_000, "the reading is live regardless"
    name, why = cod.decide(near_ceiling, "ad79f32f-1111", "claude", None, True)
    assert name is None and "newest thing on file" in why


def test_a_transcript_with_no_boundary_at_all_reads_false(near_ceiling):
    assert cod.freshly_compacted(near_ceiling, "claude") is False


# ── the locks are reaped, not merely taken (jStack#45) ──

def test_a_delivery_reaps_every_lock_nobody_is_keeping_alive():
    """The leak the per-sid lock created and nothing closed.

    A sid never comes back, so the only unlink in the hook — `held()` clearing the path
    it is about to take — never touched another session's. 66 ownerless zero-byte locks
    had piled up by the time jStack#45 counted them.
    """
    live, dead = cod.lock_path("sess-live"), cod.lock_path("sess-dead")
    os.makedirs(cod.lock_dir(), exist_ok=True)
    for path in (live, dead):
        open(path, "w").close()
    old = time.time() - cod.STALE_LOCK_SECS - 10
    os.utime(dead, (old, old))

    cod.reap()

    assert not os.path.exists(dead), "an ownerless lock survived a delivery"
    assert os.path.exists(live), "a lock a child is keeping alive was reaped under it"


def test_reaping_leaves_everything_that_is_not_a_session_lock():
    """It runs in the host's state dir now, beside the store and the token. A sweep
    that widened past its own filenames would take the host out."""
    os.makedirs(cod.lock_dir(), exist_ok=True)
    bystander = os.path.join(cod.lock_dir(), "internal-token")
    with open(bystander, "w") as fh:
        fh.write("jr1.host-internal.secret")
    old = time.time() - cod.STALE_LOCK_SECS - 10
    os.utime(bystander, (old, old))

    cod.reap()

    assert os.path.exists(bystander), "the reaper deleted a file that is not its lock"


def test_the_locks_live_where_the_host_does():
    """Not `~/.local/state/jremote` — a literal, and a directory no host on this Mac has
    ever served. That is what put 66 locks beside a second host's `host-id`."""
    from jstack_host import hostenv

    # Resolved at CALL time, which is what makes this readable at all: the suite's autouse
    # fixture overrides it by environment for every test here, so the default is whatever
    # the function answers with the variable dropped.
    # Both variables, because the log and the locks are two overrides of one default and
    # dropping only one of them tests the fixture rather than the function.
    saved = {name: os.environ.pop(name, None)
             for name in ("JSTACK_COMPACT_LOCK_DIR", "JSTACK_COMPACT_LOG")}
    try:
        assert cod.lock_dir() == hostenv.state_dir(), cod.lock_dir()
        assert str(cod.log_path()) == str(hostenv.state_dir() / cod.LOG_NAME)
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value


def test_the_child_is_handed_the_state_dir_its_parent_resolved(near_ceiling, monkeypatch):
    """One delivery, one state dir -- and the parent's is the one that counts.

    The parent arrives through `jstack-host`, which adopts the installed host's environment
    (`cli._adopt`) and so answers with the state dir this Mac actually serves. The child is
    started as `python -m` and runs none of that: left to resolve for itself it picked the
    DEFAULT profile and wrote its decision, its outcome, its lock and its
    `compacts_when_done` read into `~/.local/state/jremote` while the parent's `candidate`
    row sat in the host's own state dir. Session 244ca668 on 2026-09-23 is the receipt --
    two files, neither of which answers "what did the hook do to that session", and a
    per-agent switch read from a store the app never writes.
    """
    from jstack_host import hostenv
    monkeypatch.delenv("JREMOTE_STATE_DIR", raising=False)
    seen = {}

    def spy(argv, **kwargs):
        seen["argv"], seen["env"] = argv, kwargs.get("env")
        return None

    monkeypatch.setattr(cod.subprocess, "Popen", spy)
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps(
        {"transcript_path": near_ceiling, "session_id": "sess-state-dir"})))

    cod.main()

    assert seen.get("argv"), "the parent never detached a child"
    assert seen["env"] is not None, "the child was left to resolve the state dir itself"
    assert seen["env"]["JREMOTE_STATE_DIR"] == str(hostenv.state_dir()), seen["env"].get(
        "JREMOTE_STATE_DIR")
