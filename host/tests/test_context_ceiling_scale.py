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


def test_at_most_two_warnings_per_climb(tmp_path):
    """How often does this nag? Crossings only: twice per climb, not once per turn.

    A session climbing straight through both cuts gets one line at heavy and one at
    extreme; every other turn is silent. Because delivery-time compaction fires at the
    heavy cut, a session where that mechanism works never reaches the second one — the
    loud band is a backstop that goes quiet on its own when nothing is broken. A
    compaction re-arms both, which is correct: the climb after it is a new climb.

    This is the contract that decides whether the hook survives contact with a user.
    A note on every turn above the cut is a note that gets switched off, and a hook
    switched off is worth exactly nothing, however right each individual line was.
    """
    climb = [80_000, 120_000, 150_000, 165_000, 180_000, 195_000, 210_000, 240_000]
    spoke = []
    for i in range(1, len(climb)):
        path = write_transcript(tmp_path / f"c{i}.jsonl", 30_000, climb[i - 1:i + 1])
        if fire(path) is not None:
            spoke.append(climb[i])
    assert spoke == [165_000, 210_000], f"expected two warnings, got {len(spoke)}: {spoke}"


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
