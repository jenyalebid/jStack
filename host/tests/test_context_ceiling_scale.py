"""Context ceiling: the four contracts a hand-written payload cannot reach.

`plugins/jstack/tests/context-ceiling.sh` pins the band grammar as a live contract —
which readings fire, what the note says, which dialect it says it in. What it cannot
reach is anything about SCALE or about the module's own shape: how often the warning
speaks across a whole climb, whether the cuts are still measured rather than derived
from the client's configured window, and whether the readings are still found at the
sizes real transcripts reach. Those need the module in the same process, or a file
large enough that building it in a shell heredoc is the wrong tool.

The failure each one guards is silence at scale. A hook that reads the head of a file
in bytes finds no floor behind a 400KB instruction block and drops the saving from its
own advice; a hook that re-derives its cuts from a setting reports a different meaning
for the same number in two sessions; a hook that nags every turn gets muted, which is
the same as not shipping it.
"""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOK = str(REPO / "plugins/jstack/hooks/context-ceiling.py")

sys.path.insert(0, str(REPO / "host"))
from jstack_host import context_ceiling as cc  # noqa: E402

#: The measured cuts, restated here so a one-sided edit to the module fails loudly.
HEAVY = 160_000
EXTREME = 200_000

#: How many transcript lines one assistant message really writes.
DUPES = 3


def write_transcript(path, floor, turns, dupes=DUPES):
    """A transcript whose opening turn is `floor`, then one message per reading.

    Each message is written `dupes` times, as the client actually does — one message
    writes its usage to several lines, and counting them separately makes prev == cur
    so nothing ever fires.
    """
    with path.open("w") as fh:
        for _ in range(dupes):
            fh.write(json.dumps({"type": "assistant", "message": {
                "id": "m0", "usage": {"input_tokens": floor}}}) + "\n")
        for i, tokens in enumerate(turns):
            for _ in range(dupes):
                fh.write(json.dumps({"type": "assistant", "message": {
                    "id": f"m{i + 1}", "usage": {"input_tokens": tokens}}}) + "\n")
    return path


def write_rollout(path, floor, turns, lead=""):
    """A Codex rollout: `session_meta`, then one `token_count` event per reading."""
    def event(ordinal, tokens):
        return json.dumps({
            "timestamp": "2026-09-22T17:58:56.000Z", "ordinal": ordinal,
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {
                "model_context_window": 258400,
                # Cached is a SUBSET of input on this engine, never an addend.
                "last_token_usage": {"input_tokens": tokens,
                                     "cached_input_tokens": tokens // 2,
                                     "output_tokens": 100,
                                     "total_tokens": tokens + 100}}}})
    with path.open("w") as fh:
        fh.write(json.dumps({"type": "session_meta", "payload": {"id": "s"}}) + "\n")
        if lead:
            fh.write(lead + "\n")
        for i, tokens in enumerate([floor, *turns]):
            fh.write(json.dumps({"type": "response_item", "payload": {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "..."}]}}) + "\n")
            fh.write(event(i * 7 + 19, tokens) + "\n")
    return path


def fire(path, env=None):
    """Run the shipped hook; the injected text, or None when it stayed silent."""
    proc = subprocess.run([HOOK], input=json.dumps({"transcript_path": str(path)}),
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, f"hook errored: {proc.stderr}"
    if not proc.stdout.strip():
        return None
    return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]


def test_speaks_once_per_10k_line_not_once_per_turn(tmp_path):
    """How often does this nag? Once per 10k line, which on a real climb is one turn in
    twelve — and the bands still get the turn they cross on.

    This is the contract that decides whether the hook survives contact with a user. A note
    on every turn above the cut is a note that gets switched off, and a hook switched off is
    worth exactly nothing however right each line was. The opposite failure is the one that
    actually happened: bands alone gave 47b6d55d two lines in thirty-three minutes and then
    nothing at all from 160k to 223k.
    """
    climb = list(range(120_000, 180_001, 800))   # ~800 tokens a turn, a measured rate
    spoke = []
    for i in range(1, len(climb)):
        path = write_transcript(tmp_path / f"c{i}.jsonl", 30_000, climb[i - 1:i + 1])
        note = fire(path)
        if note is not None:
            spoke.append((climb[i], note))

    lines = [r for r, _ in spoke]
    assert len(lines) == 6, f"expected one line per 10k crossed, got {lines}"
    assert [r // 10_000 for r in lines] == [13, 14, 15, 16, 17, 18]
    assert len(spoke) < len(climb) // 10, "speaking this often is speaking every turn"

    # The band's full note on the turn that crosses it, the one-sentence tick everywhere
    # else. 160,000 is itself a 10k line, so the two are due together and the band wins.
    at_heavy = next(n for r, n in spoke if r // 10_000 == 16)
    assert "heavy band" in at_heavy and "END THE TURN" in at_heavy
    for reading, note in spoke:
        if reading // 10_000 == 16:
            continue
        assert note.count(".") <= 2 and "\n" not in note, f"a tick grew a paragraph: {note}"


def test_a_tick_above_the_cut_names_the_cut(tmp_path):
    """Between 160k and 200k the reading alone is not the whole fact — the session is over
    a cut it was already told about, and a bare number reads like everything is fine."""
    below = fire(write_transcript(tmp_path / "b.jsonl", 30_000, [138_000, 142_000]))
    over = fire(write_transcript(tmp_path / "o.jsonl", 30_000, [168_000, 172_000]))
    past = fire(write_transcript(tmp_path / "p.jsonl", 30_000, [208_000, 212_000]))
    assert below == "CONTEXT — 142,000 tokens."
    assert "still over the heavy cut" in over
    assert "still past the extreme cut" in past


def test_an_unflushed_tail_does_not_speak_the_same_figure_twice(tmp_path):
    """The regression from 47b6d55d: the PreToolUse hook of a tool call can run before the
    client has flushed the usage line of the message making that call — 359ms measured —
    and the reader then hands back the PREVIOUS pair, whose crossing has already been
    injected. The injection is in the transcript by then, in both the spellings the client
    writes it, so the figure is looked for before it is spoken again.
    """
    path = write_transcript(tmp_path / "t.jsonl", 30_000, [HEAVY - 5_000, HEAVY + 5_000])
    first = fire(path)
    assert first is not None

    for record in (json.dumps({"type": "attachment", "attachment": {
                        "type": "hook_additional_context", "content": [first]}}),
                   json.dumps({"type": "attachment", "attachment": {
                        "type": "hook_success", "stdout": json.dumps(
                            {"hookSpecificOutput": {"additionalContext": first}})}})):
        again = tmp_path / "again.jsonl"
        again.write_text(path.read_text() + record + "\n")
        assert fire(again) is None, "the same figure spoke twice"

    # A genuinely new reading still speaks, one 10k line further up.
    moved = tmp_path / "moved.jsonl"
    moved.write_text(path.read_text())
    with moved.open("a") as fh:
        for _ in range(DUPES):
            fh.write(json.dumps({"type": "assistant", "message": {
                "id": "mx", "usage": {"input_tokens": HEAVY + 15_000}}}) + "\n")
    assert fire(moved) is not None


def test_the_cuts_do_not_move_with_the_clients_window(tmp_path, monkeypatch):
    """The regression this replaced. The bands used to be the client's configured
    auto-compact window minus a margin, so the same reading meant different things in
    two sessions started either side of a settings edit — and meant nothing at all in a
    session that predated it, because the client resolves that window once at startup
    and never re-reads it. A measured cut is the same number everywhere."""
    assert cc.BANDS == [("extreme", EXTREME), ("heavy", HEAVY)]
    assert not hasattr(cc, "trigger_point"), "the window-derived threshold is back"

    import os
    env = dict(os.environ, CLAUDE_CODE_MAX_CONTEXT_TOKENS="50000")
    crossing = [HEAVY - 5000, HEAVY + 5000]
    note = fire(write_transcript(tmp_path / "a.jsonl", 30_000, crossing), env=env)
    assert note is not None, "a configured window silenced a measured cut"


def test_reads_only_the_tail_of_a_huge_transcript(tmp_path):
    """It runs on every tool call, so it must not read a 60MB file whole — but it must
    still find two readings when one turn's tool results are enormous. Those two demands
    pull opposite ways, and the bug they meet in is a tail bounded so tightly that a
    session with fat tool results reports no crossing it could act on."""
    path = tmp_path / "huge.jsonl"
    padding = "x" * 200_000  # one fat tool result per turn, ~200KB each
    with path.open("w") as fh:
        for i in range(60):
            fh.write(json.dumps({"type": "user", "pad": padding}) + "\n")
            for _ in range(DUPES):
                fh.write(json.dumps({"type": "assistant", "message": {
                    "id": f"m{i}", "usage": {"input_tokens": 130_000 + i * 200}}}) + "\n")
    assert path.stat().st_size > 12_000_000
    # The two newest turns straddle no band here; what is asserted is that it finds them.
    assert len(cc.last_readings(str(path))) == 2


def test_the_codex_floor_is_found_behind_enormous_lines(tmp_path):
    """A rollout's first reading sits about twenty lines in but as deep as 340KB, because
    the instruction blocks above it are single enormous lines. A head bounded in bytes
    misses the floor, and the advice then quotes a generic saving instead of this
    session's own — which is the difference between a number somebody acts on and one
    they scroll past."""
    lead = json.dumps({"type": "response_item", "payload": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "x" * 400_000}]}})
    path = write_rollout(tmp_path / "r.jsonl", 27_000,
                         [HEAVY - 5000, HEAVY + 5000], lead=lead)
    assert cc.first_reading(str(path), cc.CODEX) == 27_000
    note = fire(path)
    assert "27,000-token floor" in note
    # The measured landing on this engine, not Claude's: a smaller summary cost.
    assert f"{27_000 + cc.CODEX_SUMMARY_COST:,}" in note
