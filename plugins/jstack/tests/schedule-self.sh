#!/bin/bash
# The self-wake primitive: an agent's promise to itself. "I'll check back at
# three" is a booked one-shot or it is a lie the agent does not know it is
# telling — and without this file, the booking only ever works on the machine
# the tool grew up on: identity against a hardcoded tree, times in a
# hardcoded zone, a venv interpreter, a private registry. Each check below is
# one way that lie came back: a wake that books nowhere, books for the wrong
# seat, books a fork that cannot find its own conversation, or pins a
# timeout that overrides the category runway and kills a long run mid-flight.
#
# Runs under the same dateutil blocker as scheduler-bare.sh: a one-shot is
# how a self-scheduled follow-up is delivered, so it must book on a stock
# interpreter — and that has to be proved on machines that DO have the
# package too, or it only ever gets tested where it was already fine.

set -u
unset CODEX_THREAD_ID
PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SS="$PLUGIN_ROOT/bin/schedule-self"
PY="${JSTACK_PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || { echo "FAIL: no python3 on PATH (set JSTACK_PYTHON)"; exit 1; }

TMP=$(mktemp -d /tmp/jstack-schedule-self.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

# Bare root: ONLY a seat, plus a subdir a session might be standing in. Every
# answer must come from inside this tmpdir — a run that passes because it
# silently found the machine's own agent tree proves nothing.
BARE="$TMP/root"
mkdir -p "$BARE/Agents/alice/chat/notes" "$TMP/sched/config" "$TMP/home"
touch "$BARE/Agents/alice/chat/CLAUDE.md"

# A configured timezone the machine is (almost certainly) not in: the tz that
# rides into the booked job must be READ from this file, not defaulted from
# the region the code grew up in.
cat > "$TMP/sched/config/scheduler.json" <<'EOF'
{ "timezone": "UTC" }
EOF

# Block dateutil for every interpreter started under this PYTHONPATH — the
# scheduler-bare.sh technique, verbatim.
mkdir -p "$TMP/shim"
cat > "$TMP/shim/sitecustomize.py" <<'EOF'
import sys

import jstack_pin  # noqa: F401 — this shim shadows lib/sitecustomize.py


class _NoDateutil:
    """Refuse dateutil the way a machine without it does."""

    def find_module(self, name, path=None):
        return self if name == "dateutil" or name.startswith("dateutil.") else None

    def find_spec(self, name, path=None, target=None):
        if name == "dateutil" or name.startswith("dateutil."):
            raise ImportError(f"No module named {name!r}")
        return None


sys.meta_path.insert(0, _NoDateutil())
for mod in [m for m in sys.modules if m == "dateutil" or m.startswith("dateutil.")]:
    del sys.modules[mod]
EOF

export JSTACK_ROOT="$BARE"
export SCHEDULER_HOME="$TMP/sched"
export HOME="$TMP/home"
# Pin first, then front the dateutil blocker. Only the FIRST sitecustomize on
# the path is imported, so the shim shadows lib/sitecustomize.py and carries
# `import jstack_pin` itself; the re-assert is what proves it still does.
. "$PLUGIN_ROOT/tests/lib/pin-plugin-root.sh"
export PYTHONPATH="$TMP/shim:$PYTHONPATH"
jstack_assert_pinned || exit 1
# Hermetic host config: the machine's real review.json may aim mail at a live
# interpreter and a live registry; an empty one keeps every booking in $TMP.
export JSTACK_REVIEW_CONFIG="$TMP/review.json"
echo '{}' > "$JSTACK_REVIEW_CONFIG"
# The ambient session's id must not leak into the --resume checks.
unset CLAUDE_CODE_SESSION_ID

REG="$TMP/sched/config/schedule.json"
SEAT="$BARE/Agents/alice/chat"

fails=0
fail() { echo "FAIL: $1" >&2; fails=$((fails+1)); }
pass() { echo "ok: $1"; }

COUNT() { "$PY" -c "import json,sys; print(len(json.load(open(sys.argv[1])).get('jobs',[])))" "$REG" 2>/dev/null || echo 0; }

[ -x "$SS" ] || { echo "FAIL: $SS not executable" >&2; exit 1; }

# The shim has to actually work, or every check below passes for the wrong
# reason on a machine that never had dateutil in the first place.
if "$PY" -c "import dateutil" 2>/dev/null; then
    fail "the dateutil blocker did not take — the stock-interpreter guarantee is unproved"
    echo "$fails check(s) failed"; exit 1
fi
pass "dateutil is blocked for this run"

# --- the bare-root proof ------------------------------------------------------

out=$(cd "$SEAT" && "$PY" "$SS" "2099-01-01 09:00" "follow up on the rollout" 2>&1); rc=$?
if [ $rc -eq 0 ]; then
    pass "a one-shot books from a bare root on a stock interpreter"
else
    fail "booking from the bare seat failed (rc=$rc): $out"
fi

out=$("$PY" - "$REG" "$BARE" <<'EOF' 2>&1
import json, sys
reg = json.load(open(sys.argv[1])); bare = sys.argv[2]
jobs = [j for j in reg.get("jobs", []) if j["name"] == "alice-chat wake 2099-01-01 09:00"]
assert len(jobs) == 1, [j.get("name") for j in reg.get("jobs", [])]
j = jobs[0]
assert j["agent_id"] == "alice-chat", j["agent_id"]
assert j["workspace"] == f"{bare}/Agents/alice/chat", j["workspace"]
assert j["payload"]["message"] == "follow up on the rollout", j["payload"]
assert j["schedule"]["kind"] == "once", j["schedule"]
assert j["schedule"]["delete_after_run"] is True, j["schedule"]
assert j["schedule"]["tz"] == "UTC", j["schedule"]  # read from config, not a hardcoded region
assert "resume_session_id" not in j["payload"], j["payload"]
print("OK")
EOF
)
if [ "$out" = "OK" ]; then
    pass "the job is REALLY in the registry: right agent, workspace, message, configured tz"
else
    fail "registry content: $out"
fi

# Booked from deep inside the seat: the wake runs in the seat, not in
# whatever subdirectory the session was standing in.
out=$(cd "$SEAT/notes" && "$PY" "$SS" "2099-01-01 09:10" "note from deep inside" 2>&1); rc=$?
ws=$("$PY" -c "
import json,sys
j=[x for x in json.load(open(sys.argv[1]))['jobs'] if x['name']=='alice-chat wake 2099-01-01 09:10']
print(j[0]['workspace'] if j else '(missing)')" "$REG" 2>&1)
if [ $rc -eq 0 ] && [ "$ws" = "$SEAT" ]; then
    pass "a booking from a seat subdirectory still pins the seat as workspace"
else
    fail "subdirectory booking (rc=$rc, workspace=$ws): $out"
fi

# --- agent root, no seat ------------------------------------------------------

out=$(cd "$BARE/Agents/alice" && "$PY" "$SS" "2099-01-02 09:00" "agent-level wake" 2>&1); rc=$?
res=$("$PY" -c "
import json,sys
j=[x for x in json.load(open(sys.argv[1]))['jobs'] if x['name']=='alice wake 2099-01-02 09:00']
print(f\"{j[0]['agent_id']} {j[0]['workspace']}\" if j else '(missing)')" "$REG" 2>&1)
if [ $rc -eq 0 ] && [ "$res" = "alice $BARE/Agents/alice" ]; then
    pass "called from Agents/<id> with no seat: agent id and workspace are the agent dir"
else
    fail "agent-root booking (rc=$rc, got '$res'): $out"
fi

# --- outside the agent tree ---------------------------------------------------

before=$(COUNT)
out=$(cd "$TMP" && "$PY" "$SS" "2099-01-03 09:00" "lost" 2>&1); rc=$?
if [ $rc -ne 0 ] && printf '%s' "$out" | grep -qF "$BARE/Agents" && [ "$(COUNT)" = "$before" ]; then
    pass "outside agents_dir: fails naming what it looked for, books nothing"
else
    fail "outside-tree call (rc=$rc, jobs $before->$(COUNT)): $out"
fi

# --- a past time --------------------------------------------------------------

before=$(COUNT)
out=$(cd "$SEAT" && "$PY" "$SS" "2001-01-01 09:00" "too late" 2>&1); rc=$?
if [ $rc -ne 0 ] && printf '%s' "$out" | grep -qi "past" && [ "$(COUNT)" = "$before" ]; then
    pass "a past time is an error that says so, and books nothing"
else
    fail "past time (rc=$rc, jobs $before->$(COUNT)): $out"
fi

# --- resume -------------------------------------------------------------------

SID="cccccccc-3333-3333-3333-333333333333"
mkdir -p "$HOME/.claude/projects/proj-$SID"
printf '{"cwd":"%s","type":"user"}\n' "$SEAT" > "$HOME/.claude/projects/proj-$SID/$SID.jsonl"

out=$(cd "$SEAT" && CLAUDE_CODE_SESSION_ID="$SID" "$PY" "$SS" "2099-01-04 09:00" "verify the deploy landed" --resume 2>&1); rc=$?
res=$("$PY" - "$REG" "$SEAT" "$SID" <<'EOF' 2>&1
import json, sys
reg = json.load(open(sys.argv[1]))
jobs = [j for j in reg["jobs"] if j["name"] == "alice-chat wake 2099-01-04 09:00 (resume)"]
assert len(jobs) == 1, [j.get("name") for j in reg["jobs"]]
j = jobs[0]
assert j["payload"]["resume_session_id"] == sys.argv[3], j["payload"]
assert j["workspace"] == sys.argv[2], j["workspace"]  # pinned to the calling cwd
print("OK")
EOF
)
if [ $rc -eq 0 ] && [ "$res" = "OK" ]; then
    pass "--resume pins the workspace to the calling cwd and carries the session id"
else
    fail "--resume booking (rc=$rc, check: $res): $out"
fi

before=$(COUNT)
out=$(cd "$SEAT" && "$PY" "$SS" "2099-01-05 09:00" "phantom resume" --resume 2>&1); rc=$?
if [ $rc -ne 0 ] && printf '%s' "$out" | grep -q "CLAUDE_CODE_SESSION_ID" && [ "$(COUNT)" = "$before" ]; then
    pass "--resume without \$CLAUDE_CODE_SESSION_ID fails rather than booking a fresh session"
else
    fail "resume without session id (rc=$rc, jobs $before->$(COUNT)): $out"
fi

before=$(COUNT)
out=$(cd "$BARE/Agents/alice" && CLAUDE_CODE_SESSION_ID="$SID" "$PY" "$SS" "2099-01-05 10:00" "wrong dir resume" --resume 2>&1); rc=$?
if [ $rc -ne 0 ] && printf '%s' "$out" | grep -qF "$SEAT" && [ "$(COUNT)" = "$before" ]; then
    pass "--resume from a different dir refuses, naming where the session started"
else
    fail "resume from wrong dir (rc=$rc, jobs $before->$(COUNT)): $out"
fi

before=$(COUNT)
out=$(cd "$SEAT" && CLAUDE_CODE_SESSION_ID="dddddddd-4444-4444-4444-444444444444" "$PY" "$SS" "2099-01-05 11:00" "no transcript" --resume 2>&1); rc=$?
if [ $rc -ne 0 ] && printf '%s' "$out" | grep -q "transcript" && [ "$(COUNT)" = "$before" ]; then
    pass "--resume with no transcript on disk fails — nothing a fork could resume"
else
    fail "resume without transcript (rc=$rc, jobs $before->$(COUNT)): $out"
fi

# --- timeout: explicit reaches the job, omitted stays omitted -----------------

out=$(cd "$SEAT" && "$PY" "$SS" "2099-01-06 09:00" "long run" 3600 2>&1); rc=$?
res=$("$PY" - "$REG" <<'EOF' 2>&1
import json, sys
reg = json.load(open(sys.argv[1]))
j = [x for x in reg["jobs"] if x["name"] == "alice-chat wake 2099-01-06 09:00"][0]
assert j["timeout_seconds"] == 3600, j.get("timeout_seconds")

# The omitted case, on the very first booking above. add-once today writes
# the omitted flag as an explicit null; the resolver (scheduler/resolve.py::
# _present) reads null and absent as the same fact — no per-job layer, the
# category's runway governs. What must NEVER appear here is an integer: that
# is the call site pinning the global default and silently overriding the
# category, which kills a legitimately-long wake mid-flight.
j = [x for x in reg["jobs"] if x["name"] == "alice-chat wake 2099-01-01 09:00"][0]
assert not isinstance(j.get("timeout_seconds"), int), j.get("timeout_seconds")
assert j.get("timeout_seconds") is None, j.get("timeout_seconds")
print("OK")
EOF
)
if [ $rc -eq 0 ] && [ "$res" = "OK" ]; then
    pass "explicit timeout reaches the job; omitted leaves no per-job timeout at all"
else
    fail "timeout handling (rc=$rc, check: $res): $out"
fi

before=$(COUNT)
out=$(cd "$SEAT" && "$PY" "$SS" "2099-01-06 10:00" "strangled run" 60 2>&1); rc=$?
if [ $rc -ne 0 ] && printf '%s' "$out" | grep -qi "runway" && [ "$(COUNT)" = "$before" ]; then
    pass "a timeout below the global runway is refused at the booking"
else
    fail "below-floor timeout (rc=$rc, jobs $before->$(COUNT)): $out"
fi

# --- category passes through; '' for timeout means omitted --------------------

out=$(cd "$SEAT" && "$PY" "$SS" "2099-01-07 09:00" "categorized" "" "reviews" 2>&1); rc=$?
res=$("$PY" -c "
import json,sys
j=[x for x in json.load(open(sys.argv[1]))['jobs'] if x['name']=='alice-chat wake 2099-01-07 09:00']
print(j[0].get('category') if j else '(missing)')" "$REG" 2>&1)
if [ $rc -eq 0 ] && [ "$res" = "reviews" ]; then
    pass "an explicit category rides into the job; '' for timeout still means omitted"
else
    fail "category pass-through (rc=$rc, category=$res): $out"
fi

# --- idempotency --------------------------------------------------------------

before=$(COUNT)
out=$(cd "$SEAT" && "$PY" "$SS" "2099-01-01 09:00" "follow up on the rollout" 2>&1); rc=$?
if [ $rc -eq 0 ] && printf '%s' "$out" | grep -q "already booked" && [ "$(COUNT)" = "$before" ]; then
    pass "re-running the same booking is a no-op success, never a double fire"
else
    fail "dedupe (rc=$rc, jobs $before->$(COUNT)): $out"
fi

echo
if [ "$fails" -eq 0 ]; then
    echo "PASS — a stated check-back is a real one-shot on any machine, booked for the seat that said it"
    exit 0
fi
echo "$fails check(s) failed"
exit 1
