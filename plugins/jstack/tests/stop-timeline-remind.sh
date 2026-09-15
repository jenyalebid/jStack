#!/usr/bin/env bash
# jStack live test — hooks/stop-timeline-remind.py (auto-session timeline reminder).
#
# Pipes fixture Stop-hook JSON through the real hook and checks the decision:
#   - auto session (cron transcript, big enough) → block ONCE with a log_event
#     reminder; the marker makes the second stop pass through (no loop)
#   - stop_hook_active=true → never blocks (harness anti-loop honored)
#   - user-engaged transcript (typed / TUI) → never blocks
#   - tiny transcript (no-op wake) → never blocks
#   - SKIP_SESSION_HOOK=1 → never blocks
#
# Exit 0 = all pass, exit 1 = any fail.

set -u

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$PLUGIN_ROOT/hooks/stop-timeline-remind.py"

[[ -x "$HOOK" ]] || { echo "FAIL: $HOOK not executable" >&2; exit 1; }

TMP=$(mktemp -d /tmp/jstack-stopremind-test.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

python3 - "$HOOK" "$TMP" <<'PY'
import json
import os
import subprocess
import sys
from pathlib import Path

HOOK, TMP = sys.argv[1], Path(sys.argv[2])
agents = TMP / "Agents"
seat = agents / "Gamma/social"
seat.mkdir(parents=True)
(agents / "Gamma/CLAUDE.md").write_text("# Gamma")
(seat / "CLAUDE.md").write_text("# Social")
cfg = TMP / "review.json"
cfg.write_text(json.dumps({"agent_root": str(agents)}))
os.environ["JSTACK_REVIEW_CONFIG"] = str(cfg)
os.environ["JSTACK_REVIEW_STATE"] = str(TMP / "state")
os.environ.pop("SKIP_SESSION_HOOK", None)
os.environ.pop("JSTACK_TIMELINE_REMIND_DISABLED", None)

fails = []
def check(name, cond):
    print(("ok" if cond else "FAIL") + f": {name}")
    if not cond:
        fails.append(name)

PAD = {"type": "assistant", "message": {"content": [
    {"type": "text", "text": "x" * 2000}]}}   # bulk so size gate passes

def mk_transcript(name, entries, pad=15):
    f = TMP / name
    f.write_text("\n".join(json.dumps(e) for e in entries + [PAD] * pad) + "\n")
    return f

def run_hook(session_id, transcript, stop_active=False, cwd=None, env_extra=None):
    payload = {"session_id": session_id, "transcript_path": str(transcript),
               "stop_hook_active": stop_active,
               "cwd": cwd or str(seat)}
    env = os.environ.copy()
    env.update(env_extra or {})
    r = subprocess.run([HOOK], input=json.dumps(payload), env=env,
                       capture_output=True, text=True, timeout=20)
    out = r.stdout.strip()
    return json.loads(out) if out else None

CRON = [
    {"type": "user", "message": {"content": "[cron:x Wake] /social_reply post=1"}},
    {"type": "last-prompt"},
]

# auto session → block once with a log_event reminder, sourced from cwd agent
t = mk_transcript("auto.jsonl", CRON)
d = run_hook("sid-auto-1", t)
check("auto session blocked once", d is not None and d.get("decision") == "block")
check("reminder names log_event", d is not None and "log_event" in d.get("reason", ""))
check("reminder carries agent source", d is not None and "log_event gamma" in d.get("reason", ""))
# the entry must bind to THIS session so the dashboard can reopen it — the
# session id the hook holds has to reach the log_event command it hands the model
check("reminder threads --session <this session id>",
      d is not None and "--session sid-auto-1" in d.get("reason", ""))
check("reminder allows the no-op out", d is not None and "do nothing and stop" in d.get("reason", ""))
# an entry without a tag is invisible to `tag show` — auto seats are most of the
# fleet, so the tag step has to ride the SAME reminder the entry does
check("reminder names the tag vocabulary read",
      d is not None and "log_event tag list" in d.get("reason", ""))
check("reminder threads --session into tag set",
      d is not None and "tag set <name> --session sid-auto-1" in d.get("reason", ""))
# 12 auto sessions a day per seat would shred a shared vocabulary if each minted
check("reminder biases tagging to reuse over minting",
      d is not None and "reusing it is the point" in d.get("reason", ""))

# second stop of the SAME session → marker present → pass through (no loop)
d2 = run_hook("sid-auto-1", t)
check("second stop passes (marker)", d2 is None)

# harness anti-loop flag honored even without a marker
d = run_hook("sid-auto-2", t, stop_active=True)
check("stop_hook_active never blocks", d is None)

# user-engaged transcripts never block
t = mk_transcript("typed.jsonl", CRON + [
    {"type": "user", "promptSource": "typed", "message": {"content": "hey"}}])
check("typed session never blocks", run_hook("sid-user-1", t) is None)
t = mk_transcript("tui.jsonl", CRON + [{"type": "mode"}])
check("TUI session never blocks", run_hook("sid-user-2", t) is None)

# tiny transcript (no-op wake) never blocks
t = mk_transcript("tiny.jsonl", CRON, pad=0)
check("tiny transcript never blocks", run_hook("sid-tiny-1", t) is None)

# plumbing guards
t = mk_transcript("skip.jsonl", CRON)
check("SKIP_SESSION_HOOK honored",
      run_hook("sid-skip-1", t, env_extra={"SKIP_SESSION_HOOK": "1"}) is None)
check("kill switch honored",
      run_hook("sid-kill-1", t, env_extra={"JSTACK_TIMELINE_REMIND_DISABLED": "1"}) is None)

# unknown cwd → generic source, still blocks
t = mk_transcript("nocwd.jsonl", CRON)
d = run_hook("sid-auto-3", t, cwd="/tmp/somewhere")
check("non-agent cwd falls back to 'auto' source",
      d is not None and "log_event auto" in d.get("reason", ""))

print()
if fails:
    print(f"stop-timeline-remind: {len(fails)} FAILED", file=sys.stderr)
    sys.exit(1)
print("stop-timeline-remind: all pass")
PY
