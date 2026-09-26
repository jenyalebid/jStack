#!/usr/bin/env bash
# jStack live test — bin/session-review-spawn engine.
#
# Imports the real shipped engine (hermetic: JSTACK_REVIEW_CONFIG pointed at a
# temp config so CFG never reads the machine's real one) and exercises the
# pure logic that gates every review:
#   - output validator: good output passes; missing section, evidence-free
#     sections, and log_event-claimed-but-file-didn't-grow all reject;
#     'no user turns' (+ known cron paraphrases) accepted for empty walks
#   - agent resolution: umbrella project dirs, project_dir_map, $HOME →
#     default_agent, non-reviewable miss
#   - claim dedup: second claim on a live pid loses; stale (dead-pid) claim
#     is taken over
#   - log line format matches the `SPAWN <sid8> → <agent>` dashboard contract
#   - resume-delta boundary: computed from the reviewed offset (never-reviewed
#     → None; content past the offset doesn't move it); delta note lands in
#     the selfwrite prompt only when a boundary exists
#   - stale-dub sweep: dead-owner selfwrite dubs reaped, live-owner dubs kept
#   - host hooks: selfwrite_extra_prompt rides the one turn verbatim;
#     post_selfwrite_cmd runs after SELFWRITE_DONE only, with the session id and
#     the self-write's still-present transcript, logs ok|fail|timeout, and never
#     changes the self-write's own outcome
#
# Exit 0 = all pass, exit 1 = any fail.

set -u

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENGINE="$PLUGIN_ROOT/bin/session-review-spawn"

[[ -x "$ENGINE" ]] || { echo "FAIL: $ENGINE not executable" >&2; exit 1; }

TMP=$(mktemp -d /tmp/jstack-review-test.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

python3 - "$ENGINE" "$TMP" <<'PY'
import importlib.util
import importlib.machinery
import json
import os
import sys
from pathlib import Path

engine_path, tmp = sys.argv[1], Path(sys.argv[2])

# Hermetic config BEFORE import (engine loads CFG at import time)
agent_root = tmp / "Agents"
for name in ("Alpha", "Beta"):
    (agent_root / name).mkdir(parents=True)
    (agent_root / name / "CLAUDE.md").write_text("# agent\n")
# Seat-only layout: nothing at the agent top, the CLAUDE.md one level down.
# This is what a fresh install looks like, and the engine's private copy of the
# gate found no agents at all on it — so no session anywhere on such a machine
# ever got its running memory written.
(agent_root / "SeatOnly" / "chat").mkdir(parents=True)
(agent_root / "SeatOnly" / "chat" / "CLAUDE.md").write_text("# seat\n")
# Not an agent: no CLAUDE.md at the top and none in the subdir either — the
# seat scan must not turn any directory that merely has children into an agent.
(agent_root / "NoClaudeMd" / "notes").mkdir(parents=True)

cfg_path = tmp / "review.json"
cfg_path.write_text(json.dumps({
    "agent_root": str(agent_root),
    "default_agent": "alpha",
    "project_dir_map": {"-Users-x-Some-Project": "gamma"},
    "state_dir": str(tmp / "state"),
    "timeline_dir": str(tmp / "Timeline"),
}))
os.environ["JSTACK_REVIEW_CONFIG"] = str(cfg_path)

loader = importlib.machinery.SourceFileLoader("review_engine", engine_path)
spec = importlib.util.spec_from_loader("review_engine", loader)
eng = importlib.util.module_from_spec(spec)
loader.exec_module(eng)

fails = []
def check(name, cond):
    print(("ok: " if cond else "FAIL: ") + name)
    if not cond:
        fails.append(name)

# ---- validator ----------------------------------------------------------
GOOD = """## TRANSCRIPT_WALK
- turn 1 [10:02]: "fix the thing" → resolved-in-session → commit landed

## DOC_RECONCILE
- clean — examined: active.md (2 topic matches), active/ (1 file); all consistent.

## ACTIONS_TAKEN
- Edit active.md:4 — removed fossil entry

## TIMELINE
- log_event alpha --at 10:30 "Thing fixed"

## SUMMARY
Fixed the thing.
"""
CORE = eng.DEFAULTS["required_sections"]

ok, why = eng.validate_review_output(GOOD, CORE, timeline_grew=True)
check(f"good output passes ({why or 'ok'})", ok)

ok, why = eng.validate_review_output(GOOD.replace("## SUMMARY", "## WRAP"), CORE)
check("missing section rejected", not ok and "SUMMARY" in why)

ok, why = eng.validate_review_output(GOOD, CORE, timeline_grew=False)
check("log_event without file growth rejected", not ok and "did not grow" in why)

none_tl = GOOD.replace('- log_event alpha --at 10:30 "Thing fixed"',
                       "- none — routine maintenance")
ok, why = eng.validate_review_output(none_tl, CORE, timeline_grew=False)
check(f"timeline 'none — reason' passes without growth ({why or 'ok'})", ok)

empty_walk = GOOD.replace(
    '- turn 1 [10:02]: "fix the thing" → resolved-in-session → commit landed',
    "no user turns — cron-triggered session (skill payload only)")
ok, why = eng.validate_review_output(empty_walk, CORE, timeline_grew=True)
check("'no user turns' literal accepted", ok)

paraphrase = GOOD.replace(
    '- turn 1 [10:02]: "fix the thing" → resolved-in-session → commit landed',
    "cron-triggered wake, zero user prose in transcript")
ok, why = eng.validate_review_output(paraphrase, CORE, timeline_grew=True)
check("cron-spawn paraphrase accepted", ok)

bare_walk = GOOD.replace(
    '- turn 1 [10:02]: "fix the thing" → resolved-in-session → commit landed',
    "(nothing)")
ok, why = eng.validate_review_output(bare_walk, CORE, timeline_grew=True)
check("evidence-free TRANSCRIPT_WALK rejected", not ok)

extra = ["TRANSCRIPT_WALK", "J_LIST_LIVE", "DOC_RECONCILE", "ACTIONS_TAKEN", "TIMELINE", "SUMMARY"]
ok, why = eng.validate_review_output(GOOD, extra, timeline_grew=True)
check("host-extended section list enforced", not ok and "J_LIST_LIVE" in why)

# ---- timeline growth gate ------------------------------------------------
# log_event may file under a PRIOR day (--date of the last real message when a
# session ends days later) — the growth gate watches the store's max row id,
# so any new row counts whatever date it filed under.
import sqlite3 as _sq
tl_dir = tmp / "Timeline"
tl_dir.mkdir(parents=True, exist_ok=True)
_con = _sq.connect(tl_dir / "timeline.db")
_con.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY AUTOINCREMENT,"
             " date TEXT, time TEXT, agent TEXT, headline TEXT)")
_con.execute("INSERT INTO entries (date, time, agent, headline)"
             " VALUES ('2026-07-10', '09:00', 'x', 'old entry')")
_con.commit()
pre = eng._timeline_max_id(tl_dir)
_con.execute("INSERT INTO entries (date, time, agent, headline)"
             " VALUES ('2026-07-10', '15:39', 'x', 'late-filed entry')")
_con.commit()
_con.close()
post = eng._timeline_max_id(tl_dir)
check("prior-day row growth detected store-wide", post > pre)
check("missing timeline db → zero", eng._timeline_max_id(tmp / "NoSuchDir") == 0)

# ---- session limit (rate limit) ----------------------------------------
# A review spawn that only hit the Claude usage limit exits 1 with a one-line
# limit message. That is transient — not a review-content failure — so it must
# be recognized and NOT retried/escalated as a broken review (ISS-0076).
check("session-limit output recognized",
      eng.is_session_limit("You've hit your session limit · resets 7am (America/Los_Angeles)"))
check("normal review output not flagged as session-limit", not eng.is_session_limit(GOOD))

# ---- agent resolution ---------------------------------------------------
agents = eng.reviewable_agents(agent_root)
check("reviewable = root.is_agent, so a seat-only workspace counts",
      sorted(agents) == ["alpha", "beta", "seatonly"])
check("a directory with children but no CLAUDE.md anywhere is still not an agent",
      "noclaudemd" not in agents)
check("casing is preserved for the project-dir encoding",
      agents["seatonly"] == "SeatOnly")

enc_root = str(agent_root).replace("/", "-").replace(".", "-")
def res(dirname):
    return eng.resolve_agent(dirname, agent_root, agents,
                             {"-Users-x-Some-Project": "beta"}, "alpha")

check("umbrella sub-mode resolves", res(f"{enc_root}-Alpha-chat") == "alpha")
check("umbrella root resolves", res(f"{enc_root}-Beta") == "beta")
check("deep mission path resolves", res(f"{enc_root}-Alpha-missions-200-dau") == "alpha")
check("non-reviewable workspace misses", res(f"{enc_root}-NoClaudeMd-chat") is None)
check("a seat-only agent's session resolves to it",
      res(f"{enc_root}-SeatOnly-chat") == "seatonly")
seat_jsonl = tmp / "seatonly.jsonl"
seat_jsonl.write_text(json.dumps({"cwd": str(agent_root / "SeatOnly" / "chat")}) + "\n")
check("a seat-only agent's seat is named, not dropped",
      eng.resolve_submode(seat_jsonl, agent_root, "SeatOnly") == "chat")
check("project_dir_map resolves", res("-Users-x-Some-Project") == "beta")
home_enc = str(Path.home()).replace("/", "-").replace(".", "-")
check("home dir → default_agent", res(home_enc) == "alpha")
check("unrelated dir misses", res("-Users-x-Random-Thing") is None)

# ---- claim dedup --------------------------------------------------------
check("first claim wins", eng.claim_session("test-sid-1"))
check("second claim loses (live pid)", not eng.claim_session("test-sid-1"))
stale = eng.CFG["state_dir"] / "claims" / "test-sid-2"
stale.parent.mkdir(parents=True, exist_ok=True)
stale.write_text("999999999\n")  # dead pid
check("stale claim taken over", eng.claim_session("test-sid-2"))

# ---- auto-session gate (user-engaged classifier) -------------------------
import json as _json

def _mk_jsonl(name, entries):
    f = Path(eng.CFG["state_dir"]) / name
    f.write_text("\n".join(_json.dumps(e) for e in entries) + "\n")
    return f

CRON_LINES = [
    {"type": "queue-operation", "operation": "enqueue", "content": "[cron:x] /social_reply"},
    {"type": "user", "message": {"content": "[cron:x Wake] /social_reply post=1"}},
    {"type": "last-prompt"},
    {"type": "assistant", "timestamp": "2026-07-02T20:00:00Z",
     "message": {"content": [{"type": "text", "text": "Done with this round — 5 replies posted."}]}},
]
f = _mk_jsonl("cron.jsonl", CRON_LINES)
check("cron session is not user-engaged", not eng.is_user_engaged(f))

f = _mk_jsonl("typed.jsonl", CRON_LINES + [
    {"type": "user", "promptSource": "typed", "message": {"content": "shorter, close it"}}])
check("typed prompt → user-engaged", eng.is_user_engaged(f))

f = _mk_jsonl("tui.jsonl", CRON_LINES + [{"type": "permission-mode"}])
check("TUI attach (permission-mode) → user-engaged", eng.is_user_engaged(f))

# ---- auto-review carve-out (timeline-critical crons) ---------------------
# Auto sessions are normally skipped (their timeline is written in-session by the
# Stop hook), EXCEPT the purpose-built recurring crons named here — they get the
# engine self-write instead (a purpose-prompted seat-tagged timeline line + the
# dashboard stamp). Same match form as reviewed_submodes: "agent/submode" or
# "*/submode".
AUTO_LIST = ["beta/pm", "gamma/nightly-review", "*/meta"]
check("auto-review: nightly PM cron carved in",
      eng.auto_reviewed("beta", "pm", AUTO_LIST))
check("auto-review: wildcard sub-mode carved in",
      eng.auto_reviewed("alpha", "meta", AUTO_LIST))
check("auto-review: ordinary social wake stays skipped",
      not eng.auto_reviewed("beta", "social", AUTO_LIST))
check("auto-review: empty/None allowlist reviews nothing auto",
      not eng.auto_reviewed("beta", "pm", None))
check("auto-review: None sub-mode never carved in",
      not eng.auto_reviewed("beta", None, AUTO_LIST))

# ---- log format contract (dashboard parses SPAWN lines) -----------------
import re
eng._log("SPAWN abcd1234 → alpha (attempt 1, workspace: review)")
line = eng.CFG["log_file"].read_text().strip().splitlines()[-1]
m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) SPAWN (\w{8})\S* . (\w+)", line)
check("SPAWN log line matches dashboard regex", bool(m) and m.group(3) == "alpha")

# ---- self-write path (default session_end_action) -----------------------
check("session_end_action defaults to selfwrite",
      eng.DEFAULTS.get("session_end_action") == "selfwrite")

# The one-turn resume prompt must format cleanly with all placeholders and
# name the three writes it drives.
sw = eng.SELFWRITE_PROMPT.format(
    marker=eng.SELFWRITE_MARKER, agent="alpha", agent_title="Alpha",
    submode="chat", session_id="abcd1234-....",
    stamp_step=eng.SELFWRITE_STAMP_STEP.format(session_id="abcd1234-...."),
    delta_note="", compact_note="",
)
check("selfwrite prompt formats + names seat source", "log_event alpha/chat" in sw)
check("selfwrite prompt links the session",          "--session abcd1234-...." in sw)
check("selfwrite prompt does NOT name continuity",   "continuity" not in sw)
check("selfwrite prompt names the review stamp",     "stamp abcd1234-.... assistant" in sw)
check("first-review prompt has no delta scope",      "RESUME DELTA" not in sw)
# The tag is assigned by the turn that still knows what the session was about,
# and it must read from the vocabulary before writing to it — a prompt that
# names `tag set` without `tag list` mints a private tag every session.
check("selfwrite prompt reads the tag list first",   "log_event tag list" in sw)
check("selfwrite prompt tags the ORIGINAL session",
      "tag set <name> --session abcd1234-...." in sw)
# BOTH halves name the original: Claude Code overrides CLAUDE_CODE_SESSION_ID with
# the running session's own id, so inside the dub a bare `tag list` reports the
# dub's filings — none — and a session already tagged by hand or by an earlier end
# gets a second tag set beside the right one.
check("selfwrite prompt reads the ORIGINAL session's filings",
      "tag list --session abcd1234-...." in sw)
# Steps renumber when one is added; a duplicate number sends the writer looking
# for a step that isn't there.
check("selfwrite steps are numbered once each",
      [sw.count(f"\n{n}. ") for n in (1, 2, 3)] == [1, 1, 1])

# Hosts without the dashboard's review_sessions.py get NO stamp step — the
# prompt must not instruct a command that does not exist on that machine.
sw_bare = eng.SELFWRITE_PROMPT.format(
    marker=eng.SELFWRITE_MARKER, agent="alpha", agent_title="Alpha",
    submode="chat", session_id="abcd1234-....", stamp_step="", delta_note="",
    compact_note="",
)
check("stampless prompt omits the host-only command", "review_sessions" not in sw_bare)
check("stampless prompt keeps the timeline step",     "log_event alpha/chat" in sw_bare)

# A session reviewed at a previous end (resumed, ended again) gets the
# engine-computed boundary injected — the entry scopes to the delta after it.
sw_delta = eng.SELFWRITE_PROMPT.format(
    marker=eng.SELFWRITE_MARKER, agent="alpha", agent_title="Alpha",
    submode="chat", session_id="abcd1234-....", stamp_step="",
    delta_note=eng.SELFWRITE_DELTA_NOTE.format(boundary="2026-07-15 10:30"),
    compact_note="",
)
check("resume-delta prompt names the boundary",
      "RESUME DELTA" in sw_delta and "2026-07-15 10:30" in sw_delta)
check("uncompacted prompt says nothing about compaction", "COMPACTED" not in sw_delta)

# ---- compacted sessions -------------------------------------------------
# A compact makes the dub's context a summary of the early turns instead of the
# turns. The prompt's opening rule is "do not re-read files" — correct in every
# other case, wrong here, so the note must lift it for the transcript and hand
# over the exact command, or the writer obeys the rule and files an entry for
# the tail of the session.
sw_compact = eng.SELFWRITE_PROMPT.format(
    marker=eng.SELFWRITE_MARKER, agent="alpha", agent_title="Alpha",
    submode="chat", session_id="abcd1234-....", stamp_step="", delta_note="",
    compact_note=eng.SELFWRITE_COMPACT_NOTE.format(
        count_phrase="twice", when="2026-07-15 14:05",
        jsonl_path="/tmp/x/abcd1234.jsonl"),
)
check("compact note names the count and the last compact",
      "twice" in sw_compact and "2026-07-15 14:05" in sw_compact)
check("compact note points at the session's own transcript",
      "/tmp/x/abcd1234.jsonl" in sw_compact)
check("compact note lifts the no-re-read rule",
      "no-re-read rule above is lifted" in sw_compact)
check("compact note hands over a runnable jq", 'jq -r ' in sw_compact
      and 'gsub("\\\\s+"; " ")' in sw_compact)
check("compact note still leads to the same one entry",
      "log_event alpha/chat" in sw_compact)

# Detection is the engine's job, from the file — the writer cannot see its own
# missing context. A compact_boundary line is `type: system` + that subtype;
# a plain system line is not one, and a boundary already covered by a previous
# review (before the offset) is not this write's problem.
sys_line = _json.dumps({"type": "system", "subtype": "info",
                        "timestamp": "2026-07-15T16:00:00Z"}) + "\n"
cb1 = _json.dumps({"type": "system", "subtype": "compact_boundary",
                   "parentUuid": None,
                   "timestamp": "2026-07-15T18:00:00Z"}) + "\n"
cb2 = _json.dumps({"type": "system", "subtype": "compact_boundary",
                   "parentUuid": None,
                   "timestamp": "2026-07-15T21:05:00Z"}) + "\n"
cf = Path(eng.CFG["state_dir"]) / "compact.jsonl"
cf.write_text(sys_line)
check("compact_scope: none in a clean session", eng.compact_scope(cf, 0) == (0, None))
cf.write_text(sys_line + cb1 + cb2)
n, when = eng.compact_scope(cf, 0)
from datetime import datetime as _dt
want_c = _dt.fromisoformat("2026-07-15T21:05:00+00:00").astimezone().strftime("%Y-%m-%d %H:%M")
check(f"compact_scope: counts every boundary, dates the last ({n}, {when})",
      (n, when) == (2, want_c))
n2, _w2 = eng.compact_scope(cf, len((sys_line + cb1).encode()))
check("compact_scope: boundaries before the reviewed offset don't count", n2 == 1)
check("compact_scope: unreadable transcript → no claim",
      eng.compact_scope(Path(eng.CFG["state_dir"]) / "nope.jsonl", 0) == (0, None))

# ---- resume-delta boundary (mechanical, from the reviewed offset) --------
# The boundary is the last user/assistant timestamp WITHIN the recorded
# offset — the span the previous review covered. Offset 0 (never reviewed)
# → None; content past the offset must not move the boundary.
first = _json.dumps({"type": "user", "timestamp": "2026-07-15T17:00:00Z",
                     "message": {"content": "do the thing"}}) + "\n"
second = _json.dumps({"type": "assistant", "timestamp": "2026-07-15T19:30:00Z",
                      "message": {"content": [{"type": "text", "text": "done"}]}}) + "\n"
bf = Path(eng.CFG["state_dir"]) / "boundary.jsonl"
bf.write_text(first + second)
check("boundary: never reviewed → None",
      eng.last_reviewed_boundary(bf, 0) is None)
b1 = eng.last_reviewed_boundary(bf, len(first.encode()))
from datetime import datetime as _dt, timezone as _tz
want = _dt.fromisoformat("2026-07-15T17:00:00+00:00").astimezone().strftime("%Y-%m-%d %H:%M")
check(f"boundary: offset-scoped to the reviewed span ({b1})", b1 == want)
b2 = eng.last_reviewed_boundary(bf, len((first + second).encode()))
want2 = _dt.fromisoformat("2026-07-15T19:30:00+00:00").astimezone().strftime("%Y-%m-%d %H:%M")
check(f"boundary: full span reads the last message ({b2})", b2 == want2)

# ---- stale-dub sweep -----------------------------------------------------
# A selfwrite dub whose owning engine died mid-spawn must be reaped by the
# next engine run; a dub whose owner is alive must be left alone.
dubs = eng._dubs_dir()
dead_dub = tmp / "dead-dub.jsonl";  dead_dub.write_text("{}\n")
live_dub = tmp / "live-dub.jsonl";  live_dub.write_text("{}\n")
(dubs / "dead-dub-id").write_text(f"999999999 {dead_dub}\n")
(dubs / "live-dub-id").write_text(f"{os.getpid()} {live_dub}\n")
eng.sweep_stale_dubs()
check("stale dub swept (file + record)",
      not dead_dub.exists() and not (dubs / "dead-dub-id").exists())
check("live-owner dub untouched",
      live_dub.exists() and (dubs / "live-dub-id").exists())

# The SELFWRITE log line the engine emits must match the (updated) dashboard
# regex, which accepts SELFWRITE alongside legacy SPAWN.
eng._log("SELFWRITE abcd1234 → alpha/chat")
line = eng.CFG["log_file"].read_text().strip().splitlines()[-1]
m2 = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (?:SELFWRITE|SPAWN) (\w{8})\S* . (\w+)", line)
check("SELFWRITE log line matches dashboard regex", bool(m2) and m2.group(3) == "alpha")

# ---- selfwrite outcome reporting ----------------------------------------
# A clean exit is not evidence a timeline row landed. The three outcomes must be
# distinguishable in the log, or a seat that never gets written keeps booting
# cold behind a SELFWRITE_DONE that means nothing.
import sqlite3 as _sq
import subprocess as _sp

_tldir = Path(eng.CFG["timeline_dir"])
_tldir.mkdir(parents=True, exist_ok=True)
_con = _sq.connect(_tldir / "timeline.db")
_con.execute("CREATE TABLE IF NOT EXISTS entries "
             "(id INTEGER PRIMARY KEY AUTOINCREMENT, headline TEXT)")
_con.commit(); _con.close()

_DUB = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_proj = tmp / "sw-proj"; _proj.mkdir(exist_ok=True)
_jsonl = _proj / "sw.jsonl"; _jsonl.write_text('{"cwd":"%s"}\n' % tmp)
_real_run = eng.subprocess.run

def _selfwrite_case(stdout, writes_row):
    """spawn_selfwrite with the dub + `claude --print` calls stubbed; returns its log line."""
    (_proj / f"{_DUB}.jsonl").write_text("{}\n")

    def fake_run(cmd, **kw):
        if "dub-session" in str(cmd[0]):
            return _sp.CompletedProcess(cmd, 0, _DUB, "")
        if writes_row:
            c = _sq.connect(_tldir / "timeline.db")
            c.execute("INSERT INTO entries (headline) VALUES ('written')")
            c.commit(); c.close()
        return _sp.CompletedProcess(cmd, 0, stdout, "")

    eng.subprocess.run = fake_run
    try:
        eng.spawn_selfwrite("11111111-2222-3333-4444-555555555555",
                            "alpha", "Alpha", "chat", _jsonl)
    finally:
        eng.subprocess.run = _real_run
    lines = eng.CFG["log_file"].read_text().strip().splitlines()
    return next((l for l in reversed(lines) if " SELFWRITE_" in l), "")

_r = _selfwrite_case("wrote it with log_event", True)
check("selfwrite: row landed → SELFWRITE_DONE", "SELFWRITE_DONE" in _r and "+1 entry" in _r)

_r = _selfwrite_case("I ran log_event for the seat.", False)
check("selfwrite: claimed log_event but no row → SELFWRITE_NOROW", "SELFWRITE_NOROW" in _r)

_r = _selfwrite_case("Nothing timeline-worthy this session.", False)
check("selfwrite: deliberate skip → SELFWRITE_NOENTRY", "SELFWRITE_NOENTRY" in _r)

_r = _selfwrite_case("", False)
check("selfwrite: silent empty output → SELFWRITE_NOENTRY", "SELFWRITE_NOENTRY" in _r)

# ---- host hooks around the self-write ----------------------------------
# selfwrite_extra_prompt is appended verbatim to the one turn's prompt;
# post_selfwrite_cmd runs as <cmd> <sid> <transcript> after SELFWRITE_DONE,
# while the dub (the self-write's own transcript) still exists to be read.
_SID = "11111111-2222-3333-4444-555555555555"
_hook_out = tmp / "post-hook.out"
_hook = tmp / "post hook.sh"
_hook.write_text('#!/bin/sh\n'
                 'printf "%s|%s|%s|%s\\n" "$1" "$2" "$3" "$(cat "$3")" >> "$HOOK_OUT"\n'
                 'echo hooked\n')
_hook.chmod(0o755)
_fail_hook = tmp / "fail-hook.sh"
_fail_hook.write_text("#!/bin/sh\necho nope >&2\nexit 3\n"); _fail_hook.chmod(0o755)
_slow_hook = tmp / "slow-hook.sh"
_slow_hook.write_text("#!/bin/sh\nsleep 5\n"); _slow_hook.chmod(0o755)
os.environ["HOOK_OUT"] = str(_hook_out)

def _hook_case(stdout, writes_row, extra_prompt=None, post_cmd=None):
    """spawn_selfwrite with the hook keys set; returns (result, prompt, log lines)."""
    (_proj / f"{_DUB}.jsonl").write_text('{"reply":"DECISIONS: none"}\n')
    seen = {}

    def fake_run(cmd, **kw):
        if "dub-session" in str(cmd[0]):
            return _sp.CompletedProcess(cmd, 0, _DUB, "")
        if cmd[0] == "claude":
            seen["prompt"] = cmd[cmd.index("-p") + 1]
            if writes_row:
                c = _sq.connect(_tldir / "timeline.db")
                c.execute("INSERT INTO entries (headline) VALUES ('written')")
                c.commit(); c.close()
            return _sp.CompletedProcess(cmd, 0, stdout, "")
        return _real_run(cmd, **kw)       # the host command runs for real

    saved = {k: eng.CFG.get(k) for k in ("selfwrite_extra_prompt", "post_selfwrite_cmd")}
    eng.CFG["selfwrite_extra_prompt"] = extra_prompt
    eng.CFG["post_selfwrite_cmd"] = post_cmd
    before = len(eng.CFG["log_file"].read_text().splitlines())
    eng.subprocess.run = fake_run
    try:
        res = eng.spawn_selfwrite(_SID, "alpha", "Alpha", "chat", _jsonl)
    finally:
        eng.subprocess.run = _real_run
        eng.CFG.update(saved)
    lines = eng.CFG["log_file"].read_text().splitlines()[before:]
    return res, seen.get("prompt", ""), lines

check("hook keys are known config (not dropped by load_config)",
      "selfwrite_extra_prompt" in eng.DEFAULTS and "post_selfwrite_cmd" in eng.DEFAULTS)

_EXTRA = "4. HOST STEP — end with a line `MARK: yes`."
_res, _prompt, _lines = _hook_case("log_event done", True, extra_prompt=_EXTRA)
check("extra prompt appended verbatim at the end of the self-write prompt",
      _prompt.rstrip().endswith(_EXTRA) or (_EXTRA in _prompt and _prompt.index(_EXTRA) > _prompt.index("That is every step")))
check("no post cmd configured → no POST_SELFWRITE line",
      not any("POST_SELFWRITE" in l for l in _lines))
_res, _prompt, _ = _hook_case("log_event done", True)
check("unset extra prompt leaves the prompt as shipped", "HOST STEP" not in _prompt)

_hook_out.unlink(missing_ok=True)
_res, _, _lines = _hook_case("log_event done", True, post_cmd=f"'{_hook}' --flag")
_got = _hook_out.read_text().strip() if _hook_out.exists() else ""
check("post cmd ran after SELFWRITE_DONE",
      any("SELFWRITE_DONE" in l for l in _lines) and bool(_got))
check("post cmd argv = <cmd args> <session_id> <transcript>",
      _got.startswith(f"--flag|{_SID}|{_proj / (_DUB + '.jsonl')}|"))
check("post cmd read the self-write transcript before the dub was dropped",
      _got.endswith("DECISIONS: none\"}"))
check("POST_SELFWRITE ok <sid8> logged", any(f"POST_SELFWRITE ok {_SID[:8]}" in l for l in _lines))
check("dub still deleted after the post cmd", not (_proj / f"{_DUB}.jsonl").exists())
check("self-write outcome unchanged by an ok hook", _res is True)

_res, _, _lines = _hook_case("log_event done", True, post_cmd=str(_fail_hook))
check("failing post cmd → POST_SELFWRITE fail", any(f"POST_SELFWRITE fail {_SID[:8]}" in l for l in _lines))
check("failing post cmd does not change the self-write outcome", _res is True)

_saved_to = eng.POST_SELFWRITE_TIMEOUT_SECS
eng.POST_SELFWRITE_TIMEOUT_SECS = 1
try:
    _res, _, _lines = _hook_case("log_event done", True, post_cmd=str(_slow_hook))
finally:
    eng.POST_SELFWRITE_TIMEOUT_SECS = _saved_to
check("slow post cmd → POST_SELFWRITE timeout", any(f"POST_SELFWRITE timeout {_SID[:8]}" in l for l in _lines))
check("timed-out post cmd does not change the self-write outcome", _res is True)
check("post cmd timeout is 120s", _saved_to == 120)

_hook_out.unlink(missing_ok=True)
_res, _, _lines = _hook_case("Nothing timeline-worthy.", False, post_cmd=str(_hook))
check("no entry filed (SELFWRITE_NOENTRY) → post cmd not run",
      not _hook_out.exists() and not any("POST_SELFWRITE" in l for l in _lines))
_res, _, _lines = _hook_case("I ran log_event.", False, post_cmd=str(_hook))
check("SELFWRITE_NOROW → post cmd not run", not _hook_out.exists())

# ---- black-hole detection ----------------------------------------------
# A session dir with no transcript = the CLI never persisted the session
# (e.g. leaked CLAUDE_CODE_CHILD_SESSION). Must be reported, not skipped.
fake_projects = tmp / "projects"
(fake_projects / "-Users-x-Agents-Alpha-chat" / "bh-session-id").mkdir(parents=True)
eng.CLAUDE_PROJECTS = fake_projects
check("black hole reported (session dir, no jsonl)",
      eng.report_blackhole("bh-session-id") is True)
check("BLACKHOLE line logged",
      "BLACKHOLE bh-sessi" in eng.CFG["log_file"].read_text())
check("no session dir → not a black hole",
      eng.report_blackhole("never-existed-id") is False)
# A session WITH a transcript never reaches report_blackhole via main (guarded
# by `not jsonl_path`), and a jsonl-less id with no dir stays a benign skip.

print()
if fails:
    print(f"session-review: {len(fails)} FAILED", file=sys.stderr)
    sys.exit(1)
print("session-review: all pass")
PY
