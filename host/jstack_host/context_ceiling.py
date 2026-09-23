"""PreToolUse: tell a session it has got heavy, while ending the turn is still cheap.

THE BANDS ARE THE LOAD METER'S, NOT THE CLIENT'S. What matters is what a turn costs to keep
paying -- `heavy` at 160k, `extreme` at 200k, the same cuts the app and the dashboard chip
draw. Auto-compact is a backstop that should never be reached: by the time the client takes
its own boundary the session has already been overpaying for tens of turns, and the boundary
lands between two tool calls because the client tests for it beside every model call.

This used to derive its bands from `autoCompactWindow` and that was the bug. A running
session resolves its window once, at startup, and never re-reads the setting -- so editing
settings.json moved the hook's idea of the trigger and not the client's. The hook computed
267k while the client was still using 167k, and called a session 40% clear at the moment the
client was showing 4% left. A check that lies is worse than no check.

WHAT THE SESSION IS SUPPOSED TO DO ABOUT IT. End the turn. `compact_delivery` compacts a
delivered turn that is over the heavy cut, so ending is what puts the boundary at a seam
instead of inside the next task. That is the whole loop: this hook makes the session want to
stop, and the Stop hook makes stopping worth something.

Warns on the CROSSING, not on the level -- the last two readings are compared, and each band
fires the once. No state anywhere: the transcript already knows, and a warning repeated on 40
consecutive tool calls is a warning switched off. It re-arms after a compaction for free,
because the reading drops below the band on its own. In practice `extreme` fires only when
delivery compaction could not run, which makes the loud band self-silencing when the system
is working.

The cuts are `compaction`'s -- one definition of what a session weighs, kept a leaf module
so callers like this can use it. Reading a transcript is this module's, in both dialects a
machine may run: Claude writes usage onto `assistant` entries, Codex writes `token_count`
events into a rollout. The reader and the bands stay together, in the package, so the hook
that fires on every tool call and the Stop hook that acts on the same numbers can never be
reading two different definitions of heavy.
"""
import json
import os
import sys

from . import compaction  # leaf module, no host import chain

#: The two dialects, named so a caller passes an engine rather than spelling a string.
CLAUDE = "claude"
CODEX = "codex"

#: The bands that speak, heaviest first so a turn that jumps both takes the louder one.
#: `light` and `working` say nothing: a meter that always speaks is a meter nobody reads.
BANDS = [("extreme", compaction.EXTREME), ("heavy", compaction.HEAVY)]

#: The newest readings are at the end, so the tail is read rather than the file — but a
#: session whose tool results are enormous can hold only one assistant turn in a fixed
#: window (the 64MB brian/chat transcript holds exactly one in 512KB), and one reading
#: cannot show a crossing. So the window doubles until it has two, or gives up.
TAIL_BYTES = 512 * 1024
MAX_SCAN = 8 * 1024 * 1024


def tail(path, span):
    """The last `span` bytes as text, whole lines only — None when unreadable."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > span:
                fh.seek(size - span)
                fh.readline()  # discard the partial line the seek landed inside
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None


#: How far into a file to look before answering `engine_of`. Codex names itself on line 1;
#: the allowance is for a transcript opening with a record neither reader recognises.
SNIFF_LINES = 50

#: How many lines to read looking for the floor. A Codex rollout's first `token_count` lands
#: within 25 lines but as deep as 340KB, because the instruction blocks above it are single
#: enormous lines — so the head is bounded in LINES, never in bytes.
HEAD_LINES = 400


def engine_of(path):
    """Which client wrote this transcript, asked of the file and never of config.

    Codex opens a rollout with a `session_meta` record and writes every turn as a
    `response_item`; a Claude transcript writes `"type": "assistant"` entries. Reading the
    dialect off the path the hook was actually handed is the point: a config that said
    "codex" beside a Claude transcript would have the scanner find no readings at all and
    report a healthy session it never read, which is the silent lie this file exists
    against. An unrecognised shape answers CLAUDE, which is what every reading here did
    unconditionally before Codex was read at all.
    """
    try:
        with open(path, "r", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= SNIFF_LINES:
                    break
                if '"session_meta"' in line or '"response_item"' in line:
                    return CODEX
                if '"assistant"' in line:
                    return CLAUDE
    except OSError:
        pass
    return CLAUDE


def claude_readings(blob):
    """Readings in turn order, one per assistant MESSAGE.

    One message writes its usage to several transcript lines — a text block and each
    tool_use block carry the same figures, 99 records across 35 messages in a live session.
    Counting those as separate readings makes the newest two the same turn twice, so
    `prev == cur`, so nothing ever crosses and the whole check silently never fires. Dedupe
    by message id, keeping the largest figure seen for it (a streamed message grows).
    """
    per_message = {}
    for i, line in enumerate(blob.splitlines()):
        if '"usage"' not in line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if entry.get("type") != "assistant":
            continue
        msg = entry.get("message") or {}
        tokens = compaction.request_tokens(msg.get("usage") or {})
        if tokens <= compaction.MIN_READING:
            continue
        key = msg.get("id") or f"__anon{i}"
        per_message[key] = max(per_message.get(key, 0), tokens)
    return list(per_message.values())


def codex_readings(blob):
    """Readings in turn order, one per `token_count` event, repeats collapsed.

    Codex writes exactly one of these per model call, so there is no duplicate-per-message
    problem to solve — but it does re-emit an unchanged figure when a turn ends without a
    new request (ordinals 1103 and 1106 of the 2026-09-22 17:58 rollout both read 36,351).
    Two equal readings make `prev == cur` and `crossed` needs a rise, so a repeat landing on
    a cut would eat the one warning that cut ever gives. Collapsing repeats keeps the rise
    visible; a genuine plateau still fires once, because crossing needs `prev < cut <= cur`.

    `input_tokens` is taken whole rather than summed with its neighbours: Codex reports
    `cached_input_tokens` as a SUBSET of it, not an addend — 27,027 input of which 12,032
    cached, total_tokens 27,136 = input + output — so adding them would call a 27k session
    39k.
    """
    out = []
    for line in blob.splitlines():
        if '"token_count"' not in line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if entry.get("type") != "event_msg":
            continue
        payload = entry.get("payload") or {}
        if payload.get("type") != "token_count":
            continue
        last = (payload.get("info") or {}).get("last_token_usage") or {}
        try:
            tokens = int(last.get("input_tokens") or 0)
        except (TypeError, ValueError):
            continue
        if tokens <= compaction.MIN_READING or (out and out[-1] == tokens):
            continue
        out.append(tokens)
    return out


SCANNERS = {CLAUDE: claude_readings, CODEX: codex_readings}


def scan(blob, engine=CLAUDE):
    """Readings in turn order, oldest first, in whichever dialect wrote them."""
    return SCANNERS.get(engine, claude_readings)(blob)


def first_reading(path, engine):
    """The session's fixed overhead, read off its opening turn — 0 when unmeasured."""
    if engine != CODEX:
        return compaction.first_reading(path, json.loads)
    try:
        with open(path, "r", errors="replace") as fh:
            head = [line for _, line in zip(range(HEAD_LINES), fh)]
    except OSError:
        return 0
    readings = codex_readings("".join(head))
    return readings[0] if readings else 0


def last_readings(path, want=2, engine=None):
    """The `want` newest per-message readings, oldest first — `[]` when it can't be read.

    A compaction between two readings needs no special case: it drops the newer below the
    older, and a fall crosses nothing.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    if engine is None:
        engine = engine_of(path)
    span = TAIL_BYTES
    while True:
        blob = tail(path, span)
        if blob is None:
            return []
        readings = scan(blob, engine)
        if len(readings) >= want or span >= MAX_SCAN or span >= size:
            return readings[-want:]
        span = min(span * 2, MAX_SCAN)


def crossed(prev, cur):
    """The band this turn just crossed into, or None. Heaviest wins a double jump."""
    for name, cut in BANDS:
        if prev < cut <= cur:
            return name, cut
    return None


#: The one thing the session has to tell us, and the only thing it can tell us that we
#: cannot work out for ourselves. Whether work is still standing decides whether a boundary
#: at the end of this turn is a rescue or an imposition, and every attempt to infer it has
#: failed: the harness's task store has an opinion about 10 sessions out of 2,215, and the
#: closing prose does not separate "Standing by." from "Looking at that transcript."
#:
#: So the session that is PARKING work declares it -- it is the one reading this injection
#: right now, and writing the marker is part of obeying it. Silence means finished, which
#: is the default: a finished delivery is left alone, never ghost-compacted over the
#: only copy of its conversation. A forgotten marker costs a seam at worst; the client's
#: own auto-compact is the backstop.
#:
#: THE ONE WAY IT IS MISREAD IS "WORK REMAINS SOMEWHERE". b7e61868 delivered the board-scan
#: fix, committed three shas, pushed, wrote the report -- and closed it with the marker,
#: because the report's `Next Move` block named two open threads. The hook obeyed, as it
#: must: it compacted a finished conversation and prompted the session to carry on, which
#: answered "nothing left". Nothing in the mechanism was wrong; the sentence below was, by
#: saying "nothing waiting on you" and letting a backlog answer it. The test is whether
#: THIS TURN stopped early, so the wording asks that and names the near misses by name.
DECLARE = (
    "HOW TO END THE TURN. If you are stopping mid-work BECAUSE of this notice — parking "
    "the docket at a good seam — make `<!-- to-be-continued -->` the LAST line of your "
    "final message, alone on that line. That is what triggers the compaction and hands "
    "the work back to you: the delivery hook compacts at the seam you picked, then "
    "prompts you to carry on from the summary. Only the closing line is read, so "
    "mentioning it mid-sentence declares nothing. The marker asks to be RESUMED IN THIS "
    "RUN, so the question it answers is whether YOU STOPPED EARLY — not whether work "
    "remains in the world. Open issues, follow-ups, a `Next Move` naming what comes later, "
    "a backlog you are handing back: none of those are parked work. If you delivered and "
    "pushed what you were asked for, you are finished — end the turn normally and write no "
    "marker, however much is still on the board. A finished delivery is left alone."
)

#: The Codex half, and it is a different instruction because the machine is different.
#: Codex compacts SERVER-SIDE: the `compacted` record carries `message: ''` and an opaque
#: `encrypted_content`, and the window it replaces is rebuilt from the developer prompts
#: plus the user's own messages verbatim — every assistant message and every tool result
#: dropped. There is no summarizer to instruct: a marker planted through `compact_prompt`
#: never reached the other side, and while PreCompact and PostCompact both fire on 0.156,
#: neither carries an `additionalContext` wire to instruct one through. So the only thing a
#: Codex session can do about the REASONING it is carrying is put it somewhere that is not
#: the conversation. the carry hook reads the mechanical residue back off the rollout at
#: the boundary, and that is deliberately not advertised here — a session told its context
#: comes back writes less of it down, and what comes back is the commands it ran, never why.
CODEX_DECLARE = (
    "WHAT SURVIVES. Codex compacts server-side: when the window fills it is rebuilt from "
    "the developer prompts, your user's own messages verbatim, and a summary you never see. "
    "Every assistant message and every tool result in this session is dropped — what you "
    "read, what you ruled out, what you were part-way through. Nothing can steer that "
    "summary, so the only place parked work survives is on disk. Commit and push what you "
    "changed. Anything not committable — what you ruled out and why, what you were about "
    "to do next, a wake you booked — write it into the file or the issue it belongs to "
    "before you end the turn. A sentence in your closing message does not survive; a sha "
    "does."
)

#: Why the heavy band is worth interrupting for. One sentence, both engines.
COST = (
    "That is the heavy band: every turn from here re-reads several times a fresh session's "
    "whole footprint, and most of what it re-reads is spent — dead ends, superseded reads, "
    "decisions already made."
)

HEAVY = (
    "CONTEXT — {cur:,} tokens. " + COST + "\n\n"
    "Finish the unit of work you are on, commit and push it, then END THE TURN. "
    "The client's own boundary lands inside your next task; ending is what puts one at a "
    "seam instead. Don't open a new thread of work, "
    "don't start a broad search, and don't read a large file you could grep. {recovery}"
    "\n\n" + DECLARE
)

EXTREME = (
    "CONTEXT — {cur:,} tokens, past the extreme cut. The seam compaction has not taken "
    "this session down, so nothing is going to unless you stop.\n\n"
    "Nothing new starts now. Write the file you are part-way through, stage and push what "
    "you changed, and end the turn. Name anything unfinished in your reply: a summary keeps "
    "what you wrote down, not what you were about to do. {recovery}"
    "\n\n" + DECLARE
)

CODEX_HEAVY = (
    "CONTEXT — {cur:,} tokens. " + COST + "\n\n"
    "Finish the unit of work you are on, commit and push it, then END THE TURN. "
    "Don't open a new thread of work, don't start a broad search, and don't read a large "
    "file you could grep. {recovery}"
    "\n\n" + CODEX_DECLARE
)

CODEX_EXTREME = (
    "CONTEXT — {cur:,} tokens, past the extreme cut. Codex takes its own boundary only with "
    "the window nearly full, so it will land inside whatever you are doing then — stopping "
    "now is what keeps it off a half-finished unit.\n\n"
    "Nothing new starts now. Write the file you are part-way through, stage and push what "
    "you changed, and end the turn. {recovery}"
    "\n\n" + CODEX_DECLARE
)

#: The text a band gets, per dialect. An engine with no entry falls back to Claude's, which
#: is what every session was handed before Codex was read at all.
NOTES = {
    (CLAUDE, "heavy"): HEAVY,
    (CLAUDE, "extreme"): EXTREME,
    (CODEX, "heavy"): CODEX_HEAVY,
    (CODEX, "extreme"): CODEX_EXTREME,
}

#: What a compaction would actually buy, which is the number that decides whether compacting
#: is even the right move. It is also the only part of this that is per-session: the cuts are
#: fixed, the saving is measured, and a heavy session on a heavy floor gets told the truth.
RECOVERS = (
    "A compaction here lands you around {landing:,} — this session's own {floor:,}-token "
    "floor plus the summary — freeing about {freed:,} off every remaining turn. Worth taking."
)

#: No handoff advice, at any size. Measured across 76 real compactions, handing
#: off lands a median 10k BELOW a compaction of the same session and pays that 10k with the
#: entire live thread; `test_neither_surface_prescribes_handoff_by_size` guards the app
#: against the same regression. When little is reclaimable the honest answer is that the
#: weight is structural and compacting is not the lever — not that a fresh session is.
RECOVERS_LITTLE = (
    "Note: this session's fixed overhead is already {floor:,} tokens, so a compaction lands "
    "at {landing:,} and frees only about {freed:,}. The weight here is structural — carry on "
    "if the work needs it, but keep it tight; compacting is not the lever."
)


#: What a compaction costs on Codex, measured over the 45 real compactions in
#: ~/.codex/sessions the same way `compaction.SUMMARY_COST` was measured over Claude's:
#: median tokens the post-compaction reading sits above the session's own floor. Codex
#: lands a third as high — 3,343 against 10,000 — because it keeps no tail of real turns.
CODEX_SUMMARY_COST = 3_500

#: Median opening reading across those same 45, and a fallback that should never be
#: reached: a rollout writes its first `token_count` inside two dozen lines.
CODEX_TYPICAL_FLOOR = 26_500


def landing(floor, engine):
    """Where a compaction would leave this session: its own floor plus the summary."""
    if engine != CODEX:
        return compaction.landing(floor)
    return (floor if floor > 0 else CODEX_TYPICAL_FLOOR) + CODEX_SUMMARY_COST


def recovery_note(path, cur, engine):
    """How much a compaction would free, or '' when the floor couldn't be measured."""
    floor = first_reading(path, engine)
    if floor <= 0:
        return ""
    lands = landing(floor, engine)
    freed = max(0, cur - lands)
    tmpl = RECOVERS_LITTLE if freed < compaction.LITTLE else RECOVERS
    return tmpl.format(floor=floor, landing=lands, freed=freed)


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    path = payload.get("transcript_path")
    if not path:
        return 0

    engine = engine_of(path)
    readings = last_readings(path, engine=engine)
    if len(readings) < 2:
        return 0

    prev, cur = readings
    hit = crossed(prev, cur)
    if not hit:
        return 0

    band, _ = hit
    template = NOTES.get((engine, band)) or NOTES[(CLAUDE, band)]
    note = template.format(cur=cur, recovery=recovery_note(path, cur, engine))
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": note.strip(),
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
