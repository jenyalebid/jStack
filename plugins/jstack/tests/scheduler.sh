#!/usr/bin/env bash
# jStack live test — the scheduler package and its install seam.
#
# Runs the real shipped package against a hermetic temp SCHEDULER_HOME (never
# touches a real registry, state dir, or running daemon). Verifies the contract
# that makes one package serve every machine:
#   - config/scheduler.json drives timezone, spawn env, and spawn PATH
#   - workspace resolution: job override > resolver hook > registry > agents dir,
#     with seat_rules applied only when the seat is real
#   - data dirs: SCHEDULER_*_DIR > SCHEDULER_HOME > JSTACK_ROOT derivation >
#     ~/.scheduler, and the derivation layer never moves an install that
#     predates it
#   - a broken resolver spec raises instead of silently running elsewhere
#   - permission_mode defaults to bypassPermissions and resolves job>category>default
#   - a run killed by an unrefreshable OAuth session scores `auth_expired`
#     (spawned and adopted alike), defers a retry, and parks past the cap
#   - a printed job time is labelled with the zone abbreviation of THAT
#     instant, not of now — both sides of the DST transition, in one run
#   - VTIMEZONE is derived from the zone (DST, last-Sunday, and no-DST cases)
#   - registry round-trip: add a job, read it back, remove it
#   - the daemon actually boots and serves /health
#
# Exit 0 = all pass, exit 1 = any fail.

set -u

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

PY="${JSTACK_PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || { echo "FAIL: no python3 on PATH (set JSTACK_PYTHON)"; exit 1; }

# python-dateutil is needed for RRULE expansion and for NOTHING ELSE. Recurring
# jobs require it; one-shot jobs must not, because a one-shot is how a message
# wake and a self-scheduled follow-up are delivered — the paths a fresh install
# needs on day one, before anyone has installed anything. The rest of this file
# exercises recurrence and so needs the package; the portability check below
# runs first and deliberately runs WITHOUT it.
if ! "$PY" -c "import dateutil.rrule" >/dev/null 2>&1; then
    echo "SKIP: python-dateutil not installed for $PY — the recurrence checks need it."
    echo "      Install it (pip install python-dateutil) or point JSTACK_PYTHON at the venv that runs the daemon."
    echo "      NOTE: the one-shot path does not need it; see tests/scheduler-bare.sh."
    exit 0
fi

TMP=$(mktemp -d /tmp/jstack-scheduler-test.XXXXXX)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/config" "$TMP/agents/demo-social/chat" "$TMP/agents/plain"
# `plain` and `demo-social` stay bare on purpose: a workspace is not required
# to be a declared agent. The fallback's bar is "a directory that exists", and
# these two check it stays that low.

export SCHEDULER_HOME="$TMP"
# `from scheduler import …` below must be THIS tree's package: the gate's
# interpreter carries a .pth fronting the main checkout, and it outranks
# PYTHONPATH. See tests/lib/pin-plugin-root.sh.
. "$PLUGIN_ROOT/tests/lib/pin-plugin-root.sh"

cat > "$TMP/config/scheduler.json" <<EOF
{
  "timezone": "America/Los_Angeles",
  "spawn_env": {"JSTACK_TIMELINE_ORIGIN": "indirect", "MARKER": "set"},
  "spawn_path_prepend": ["$TMP/tools"],
  "spawn_dirs": ["/usr/bin"],
  "agent_root": "$TMP/agents",
  "seat_rules": [{"agent_id_suffix": "-social", "seat": "chat"}]
}
EOF

fails=0
fail() { echo "FAIL: $1" >&2; fails=$((fails+1)); }
pass() { echo "ok: $1"; }

check() {  # check <name> <python expression asserting truth>
    local name="$1"; shift
    # sys.path, not PYTHONPATH alone. An install is entitled to drop a .pth in
    # site-packages that inserts ITS copy of the plugin at position 0 — the
    # scheduler's own installer does exactly that — and site processes .pth
    # files after PYTHONPATH, so the installed copy wins. A suite that imports
    # it is green about a checkout nobody pointed it at: it passes here and the
    # edit under test is never executed. PLUGIN_ROOT is the subject, so it goes
    # first, and the one-line preamble offsets reported line numbers by one.
    if out=$("$PY" -c "import sys; sys.path.insert(0, '$PLUGIN_ROOT')
$1" 2>&1); then
        pass "$name"
    else
        fail "$name — $out"
    fi
}

# ── install config drives the machine-specific bits ──

check "timezone comes from scheduler.json" '
from scheduler import config
assert config.default_tz() == "America/Los_Angeles", config.default_tz()
'

check "spawn env + PATH prepend come from scheduler.json" '
from scheduler import spawn
env = spawn.run_env({"PATH": "/inherited"})
assert env["MARKER"] == "set", env.get("MARKER")
assert env["JSTACK_TIMELINE_ORIGIN"] == "indirect"
assert env["PATH"].endswith("/usr/bin:/inherited"), env["PATH"]
'

# ── workspace resolution ──

check "job workspace override wins" '
from scheduler import spawn
from pathlib import Path
assert spawn.resolve_workspace({"agent_id": "x", "workspace": "/tmp/override"}) == Path("/tmp/override")
'

check "agent_root fallback resolves" '
import os
from scheduler import spawn
from pathlib import Path
want = Path(os.environ["SCHEDULER_HOME"]) / "agents" / "plain"
assert spawn.resolve_workspace({"agent_id": "plain"}) == want
'

check "seat rule redirects into a real seat" '
import os
from scheduler import spawn
from pathlib import Path
root = Path(os.environ["SCHEDULER_HOME"]) / "agents" / "demo-social"
(root / "chat" / "CLAUDE.md").write_text("## Machine\n")
assert spawn.resolve_workspace({"agent_id": "demo-social"}) == root / "chat"
'

check "seat rule ignored when the seat has no CLAUDE.md" '
import os
from scheduler import spawn
from pathlib import Path
root = Path(os.environ["SCHEDULER_HOME"]) / "agents" / "demo-social"
(root / "chat" / "CLAUDE.md").unlink()
assert spawn.resolve_workspace({"agent_id": "demo-social"}) == root
'

check "broken resolver spec raises rather than falling back" '
from scheduler import config, spawn
config.reset_install_cache()
inst = config.install()
inst["workspace_resolver"] = "not-a-spec"
try:
    spawn.resolve_workspace({"agent_id": "plain"})
except ValueError:
    pass
else:
    raise AssertionError("silently fell back instead of raising")
finally:
    config.reset_install_cache()
'

# ── workspace fallback: real directories, not fabricated joins ──
# These need the parent env minus any ambient JSTACK_* declarations — the
# resolution under test must come from scheduler.json's agent_root, not from
# whatever the machine running this file happens to export.

mkdir -p "$TMP/agents/CasedAgent" "$TMP/agents/snake_agent"
touch "$TMP/agents/CasedAgent/CLAUDE.md" "$TMP/agents/snake_agent/CLAUDE.md"

if out=$(env -u JSTACK_ROOT -u JSTACK_AGENTS_DIR "$PY" -c '
import os
from pathlib import Path
from scheduler import spawn
base = Path(os.environ["SCHEDULER_HOME"]) / "agents"
assert spawn.resolve_workspace({"agent_id": "casedagent"}) == base / "CasedAgent"
assert spawn.resolve_workspace({"agent_id": "snake-agent"}) == base / "snake_agent"
' 2>&1); then
    pass "fallback tolerates case and -/_ spelling of a real agent dir"
else
    fail "agent id folding — $out"
fi

if out=$(env -u JSTACK_ROOT -u JSTACK_AGENTS_DIR "$PY" -c '
import os
from pathlib import Path
from scheduler import spawn
base = Path(os.environ["SCHEDULER_HOME"]) / "agents"
# `plain` has no CLAUDE.md, so it is not an agent — and still resolves. A
# workspace only has to exist; requiring a declaration would refuse to run a
# job in a directory that is sitting right there.
assert spawn.resolve_workspace({"agent_id": "plain"}) == base / "plain"
' 2>&1); then
    pass "a real directory resolves even when nothing declared it an agent"
else
    fail "undeclared-but-real workspace — $out"
fi

if out=$(env -u JSTACK_ROOT -u JSTACK_AGENTS_DIR "$PY" -c '
from scheduler import spawn
try:
    spawn.resolve_workspace({"agent_id": "gh0st"})
except ValueError as e:
    msg = str(e)
    assert "gh0st" in msg, msg            # the id that missed
    assert "/agents" in msg, msg          # the dir that was searched
    assert "CasedAgent" in msg, msg       # a real neighbouring agent id
else:
    raise AssertionError("a nonexistent agent resolved to a fabricated path")
' 2>&1); then
    pass "a miss raises at the mistake, naming the searched dir and the real ids"
else
    fail "workspace miss error — $out"
fi

if out=$(env -u JSTACK_ROOT -u JSTACK_AGENTS_DIR "$PY" -c '
from pathlib import Path
from scheduler import spawn
got = spawn.resolve_workspace({"agent_id": "gh0st", "workspace": "/tmp/pinned"})
assert got == Path("/tmp/pinned"), got
' 2>&1); then
    pass "an explicit job workspace wins even when the agent does not exist"
else
    fail "workspace override for a missing agent — $out"
fi

# ── data dirs: the JSTACK_ROOT derivation layer ──
# A NEW layer between SCHEDULER_HOME and the ~/.scheduler default. Each check
# states its own environment outright: the layer's whole contract is
# precedence, and precedence only shows under a controlled one.

JROOT="$TMP/jstack-root"; LEGACY_HOME="$TMP/legacy-home"
mkdir -p "$JROOT" "$LEGACY_HOME"

# No JSTACK_ROOT: byte-identical to the pre-derivation chain. This is the
# regression that protects the running daemon.
if out=$(env -u JSTACK_ROOT -u SCHEDULER_CONFIG_DIR -u SCHEDULER_STATE_DIR \
        -u SCHEDULER_CREDENTIALS_DIR SCHEDULER_HOME="$TMP" "$PY" -c '
import os
from pathlib import Path
from scheduler import config
home = Path(os.environ["SCHEDULER_HOME"])
assert config.CONFIG_DIR == home / "config", config.CONFIG_DIR
assert config.STATE_DIR == home / "state" / "scheduler", config.STATE_DIR
assert config.CREDENTIALS_DIR == home / "Credentials", config.CREDENTIALS_DIR
' 2>&1); then
    pass "no JSTACK_ROOT: SCHEDULER_HOME keeps the exact legacy shapes"
else
    fail "SCHEDULER_HOME legacy regression — $out"
fi

# The interpreter that runs a live daemon may declare SCHEDULER_HOME itself
# through a site hook (a .pth setdefault), so `env -u` alone cannot produce
# the unset state these checks are about. -S keeps site processing out of a
# subprocess whose whole premise is that the variable is absent.
if out=$(env -u JSTACK_ROOT -u SCHEDULER_HOME -u SCHEDULER_CONFIG_DIR \
        -u SCHEDULER_STATE_DIR -u SCHEDULER_CREDENTIALS_DIR \
        HOME="$LEGACY_HOME" "$PY" -S -c '
import os
from pathlib import Path
from scheduler import config
home = Path(os.environ["HOME"]) / ".scheduler"
assert config.CONFIG_DIR == home / "config", config.CONFIG_DIR
assert config.STATE_DIR == home / "state" / "scheduler", config.STATE_DIR
assert config.CREDENTIALS_DIR == home / "Credentials", config.CREDENTIALS_DIR
' 2>&1); then
    pass "no JSTACK_ROOT, no SCHEDULER_HOME: the ~/.scheduler default is untouched"
else
    fail "bare-default legacy regression — $out"
fi

if out=$(env -u SCHEDULER_HOME -u SCHEDULER_CONFIG_DIR -u SCHEDULER_STATE_DIR \
        -u SCHEDULER_CREDENTIALS_DIR -u JSTACK_CONFIG_DIR -u JSTACK_STATE_DIR \
        -u JSTACK_CREDENTIALS_DIR JSTACK_ROOT="$JROOT" "$PY" -S -c '
import os
from pathlib import Path
from scheduler import config
r = Path(os.environ["JSTACK_ROOT"])
assert config.CONFIG_DIR == r / "Config", config.CONFIG_DIR
assert config.STATE_DIR == r / "State" / "scheduler", config.STATE_DIR
assert config.CREDENTIALS_DIR == r / "Credentials", config.CREDENTIALS_DIR
' 2>&1); then
    pass "JSTACK_ROOT alone: dirs derive as Config / State/scheduler / Credentials"
else
    fail "JSTACK_ROOT derivation — $out"
fi

if out=$(env -u SCHEDULER_HOME -u SCHEDULER_CONFIG_DIR -u SCHEDULER_CREDENTIALS_DIR \
        -u JSTACK_CONFIG_DIR -u JSTACK_STATE_DIR -u JSTACK_CREDENTIALS_DIR \
        JSTACK_ROOT="$JROOT" SCHEDULER_STATE_DIR="$TMP/live-state" "$PY" -S -c '
import os
from pathlib import Path
from scheduler import config
assert config.STATE_DIR == Path(os.environ["SCHEDULER_STATE_DIR"]), config.STATE_DIR
assert config.CONFIG_DIR == Path(os.environ["JSTACK_ROOT"]) / "Config", config.CONFIG_DIR
' 2>&1); then
    pass "SCHEDULER_STATE_DIR outranks JSTACK_ROOT; its siblings still derive"
else
    fail "explicit-dir precedence over JSTACK_ROOT — $out"
fi

# ── permission_mode ──

check "permission_mode defaults to bypassPermissions" '
from scheduler import config, runner
argv = runner.build_argv("claude", "opus", "sid", "msg")
assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
assert config.BUILTIN_DEFAULTS["permission_mode"] == "bypassPermissions"
'

check "permission_mode resolves job > category > default" '
from scheduler import resolve
d = {"permission_mode": "bypassPermissions"}
c = {"tight": {"permission_mode": "acceptEdits"}}
assert "permission_mode" in resolve.INHERITED_KEYS
assert resolve.resolve_setting({"category": "tight"}, "permission_mode", d, c) == "acceptEdits"
assert resolve.resolve_setting({"category": "tight", "permission_mode": "plan"}, "permission_mode", d, c) == "plan"
'

# ── auth-expiry classification and deferred retry ──
#
# The failure these cover: a run whose claude child died on
# "Failed to authenticate: OAuth session expired and could not be refreshed"
# used to score `error`, the hard-fault status — no retry, and a daily job
# dropped its whole day silently (2026-09-07 and 09-08, same job, both times).

check "auth expiry matches the family, not one literal, and not a dead login" '
from scheduler import runner
line = "Failed to authenticate: OAuth session expired and could not be refreshed"
assert runner.is_auth_expired(line)
assert runner.is_auth_expired("Skipping: OAuth token expired and refresh failed (re-login required)")
assert runner.is_auth_expired("error: the oauth session has expired")
# "Please run /login" is a login no retry can fix — it must stay a hard error.
assert not runner.is_auth_expired("Please run /login")
assert not runner.is_auth_expired("Failed to authenticate. Fetching token: bad request")
# and the arm does not poach the neighbouring families
assert not runner.is_auth_expired("API Error: 529 Overloaded")
assert not runner.is_usage_limit(line)
assert not runner.is_transient_api_error(line)
'

check "auth_expired is its own status, ordered ahead of api_error" '
from datetime import datetime, timezone
from scheduler import config, runner
r = runner.Run(job={"id": "j", "payload": {"message": "x"}},
               defaults=dict(config.BUILTIN_DEFAULTS),
               scheduled_for=datetime.now(timezone.utc),
               on_finish=lambda *a, **k: None)
r.auth_expired = True
r.transient_api_error = True
assert r._status(1) == "auth_expired", r._status(1)
r.rate_limited = True
assert r._status(1) == "rate_limited", r._status(1)   # usage limit still wins
r.kill_reason = "stall"
assert r._status(1) == "stalled", r._status(1)        # a kill still wins over all
'

check "an ADOPTED run scores auth_expired in the same order" '
from scheduler import config, runner
config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
(config.LOGS_DIR / "adopted-auth.out").write_text(
    "Failed to authenticate: OAuth session expired and could not be refreshed\n")
a = runner.AdoptedRun("j", {"run_id": "adopted-auth"}, {"id": "j"},
                      dict(config.BUILTIN_DEFAULTS), lambda *a, **k: None)
# The auth line is non-empty stdout, which the evidence arm below reads as a
# COMPLETED run — without its own arm an adopted auth failure scores ok.
assert a._exited_status(None) == "auth_expired", a._exited_status(None)
'

check "an auth expiry defers a retry instead of hard-failing" '
from datetime import datetime, timezone
from scheduler import engine

NOW = datetime(2026, 9, 8, 6, 30, 5, tzinfo=timezone.utc)
JOB = {"id": "auth-defer", "agent_id": "plain", "enabled": True}


class FakeRun:
    run_id, session_id, model, retry_of = "r1", "s1", "opus", None
    job, job_id = JOB, JOB["id"]
    scheduled_for_ms = spawned_at_ms = int(NOW.timestamp() * 1000)


e = engine.Engine(now_fn=lambda: NOW)
e._on_run_finish(FakeRun(), status="auth_expired", exit_code=1,
                 kill_reason=None, summary="")
st = e.state["auth-defer"]
assert st["last_status"] == "auth_expired", st
# pinned a few minutes out, NOT fired again in the same breath
assert st["next_run_at_ms"] == int(NOW.timestamp() * 1000) + engine._AUTH_RETRY_SECONDS * 1000, st
assert st["auth_retries"] == 1, st
# a retry is coming, so the streak that drives alerting must not move
assert int(st.get("consecutive_errors") or 0) == 0, st
'

check "the auth retry cap parks the job as a hard failure" '
from datetime import datetime, timezone
from scheduler import engine

NOW = datetime(2026, 9, 8, 6, 30, 5, tzinfo=timezone.utc)
JOB = {"id": "auth-cap", "agent_id": "plain", "enabled": True}


class FakeRun:
    run_id, session_id, model, retry_of = "r1", "s1", "opus", None
    job, job_id = JOB, JOB["id"]
    scheduled_for_ms = spawned_at_ms = int(NOW.timestamp() * 1000)


e = engine.Engine(now_fn=lambda: NOW)
for _ in range(engine._MAX_AUTH_RETRIES):
    e._on_run_finish(FakeRun(), status="auth_expired", exit_code=1,
                     kill_reason=None, summary="")
st = e.state["auth-cap"]
assert st["auth_retries"] == engine._MAX_AUTH_RETRIES, st
pinned = st["next_run_at_ms"]
e._on_run_finish(FakeRun(), status="auth_expired", exit_code=1,
                 kill_reason=None, summary="")
assert st["next_run_at_ms"] == pinned, st          # no further defer booked
assert st["auth_retries"] == engine._MAX_AUTH_RETRIES, st
# a login that never comes back has to become visible
assert st["consecutive_errors"] == 1, st
assert "re-login" in st["last_error"], st
'

check "a later auth expiry starts a fresh retry budget" '
from datetime import datetime, timedelta, timezone
from scheduler import engine

NOW = datetime(2026, 9, 8, 6, 30, 5, tzinfo=timezone.utc)
JOB = {"id": "auth-episode", "agent_id": "plain", "enabled": True}


class FakeRun:
    run_id, session_id, model, retry_of = "r1", "s1", "opus", None
    job, job_id = JOB, JOB["id"]
    scheduled_for_ms = spawned_at_ms = int(NOW.timestamp() * 1000)


now = NOW
e = engine.Engine(now_fn=lambda: now)
for _ in range(engine._MAX_AUTH_RETRIES + 1):
    e._on_run_finish(FakeRun(), status="auth_expired", exit_code=1,
                     kill_reason=None, summary="")
st = e.state["auth-episode"]
assert st["consecutive_errors"] == 1, st            # cap spent today
now = NOW + timedelta(days=1)                       # tomorrow: a NEW fault
e._on_run_finish(FakeRun(), status="auth_expired", exit_code=1,
                 kill_reason=None, summary="")
assert st["auth_retries"] == 1, st
assert st["next_run_at_ms"] == int(now.timestamp() * 1000) + engine._AUTH_RETRY_SECONDS * 1000, st
assert st["consecutive_errors"] == 1, st            # still 1 — the retry is live again
'

check "a successful run clears the auth retry budget" '
from datetime import datetime, timezone
from scheduler import engine

NOW = datetime(2026, 9, 8, 6, 30, 5, tzinfo=timezone.utc)
JOB = {"id": "auth-clear", "agent_id": "plain", "enabled": True}


class FakeRun:
    run_id, session_id, model, retry_of = "r1", "s1", "opus", None
    job, job_id = JOB, JOB["id"]
    scheduled_for_ms = spawned_at_ms = int(NOW.timestamp() * 1000)


e = engine.Engine(now_fn=lambda: NOW)
e._on_run_finish(FakeRun(), status="auth_expired", exit_code=1,
                 kill_reason=None, summary="")
e._on_run_finish(FakeRun(), status="ok", exit_code=0, kill_reason=None, summary="done")
st = e.state["auth-clear"]
assert st["auth_retries"] == 0, st
assert "auth_last_fail_ms" not in st, st
'

# ── a retry knows it is a retry, and is withheld from a run that acted (#52) ──
#
# The failure these cover: `api_error` fires whenever the connection drops,
# including on the LAST turn of a run that had already done everything it was
# told to do. The retry arm re-sent the identical payload to a session with no
# way to know a sibling had run, and it repeated every action. Observed
# 2026-09-11: run cecd0cb9 booked a wake, died 100s later, and its retry booked
# a second competing one. The same path re-runs a push, a publish, or a send.

check "a transcript of pure reads names no action" '
import json
from pathlib import Path
from scheduler import config, runner
p = Path(config.STATE_DIR) / "t-reads.jsonl"
p.parent.mkdir(parents=True, exist_ok=True)
def tool(name, **inp):
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": name, "input": inp}]}})
p.write_text("\n".join([
    json.dumps({"type": "user", "message": {"content": "go"}}),
    tool("Read", file_path="/x"),
    tool("Grep", pattern="y"),
    tool("TodoWrite", todos=[]),
]))
assert runner.first_action(p) is None, runner.first_action(p)
'

check "a transcript naming a publish, a push, or a subagent names the first one" '
import json
from pathlib import Path
from scheduler import config, runner
d = Path(config.STATE_DIR); d.mkdir(parents=True, exist_ok=True)
def one(name, **inp):
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": name, "input": inp}]}})
def check_first(tag, line, want):
    p = d / f"t-{tag}.jsonl"
    p.write_text(one("Read", file_path="/x") + "\n" + line)
    got = runner.first_action(p)
    assert got == want, (tag, got)
# the MCP publish/send surface an install adds is unknown to this file, and is
# exactly why the tool test is an allowlist rather than a denylist
check_first("pub", one("mcp__meta-api__threads_publish", text="hi"),
            "mcp__meta-api__threads_publish")
check_first("task", one("Task", prompt="do it"), "Task")
check_first("write", one("Write", file_path="/x"), "Write")
check_first("fetch", one("WebFetch", url="https://x"), "WebFetch")
check_first("push", one("Bash", command="git push origin main"),
            "Bash: git push origin main")
'

check "shell calls never earn automatic replay from a command-name allowlist" '
from scheduler.runner import is_read_only_bash as ro
for c in ["git status", "git log --oneline -5", "git -C /r diff HEAD",
          "git --no-pager show HEAD", "ls -la ~/x", "cat a | grep foo",
          "rg -n pat . && wc -l a", "find . -name \"*.py\"", "sed -n 1,5p f",
          "jq .a f.json", "ps aux | grep claude", "date; whoami", ""]:
    assert not ro(c), c
for c in ["git symbolic-ref HEAD refs/heads/other", "sort -o output input",
          "sed \"w output\" input", "awk \"BEGIN {system(1)}\"",
          "rg --pre helper pattern file", "git diff --output=out"]:
    assert not ro(c), c
for c in ["git push origin main", "git merge --no-ff x", "git commit -m x",
          "git config user.name bob", "git stash", "git branch -d old",
          "git tag v1", "git remote add o u", "echo hi > f", "cat a > b",
          "curl -X POST https://x", "rm -rf /tmp/x", "sed -i \"\" s/a/b/ f",
          "find . -name x -delete", "find . -exec rm {} ;", "ls $(rm -rf /)",
          "ls `whoami`", "python3 s.py", "sleep 5 &", "ls \"unbalanced",
          "tee out.txt", "env FOO=1 rm x", "xargs rm", "gh pr create",
          "/bin/rm x", "./deploy.sh"]:
    assert not ro(c), c
'

check "missing unreadable and malformed transcripts block replay" '
from pathlib import Path
from scheduler import config, runner
assert runner.first_action(Path(config.STATE_DIR) / "never-written.jsonl") == "missing transcript"
d = Path(config.STATE_DIR) / "t-dir.jsonl"     # a path that exists and will not read
d.mkdir(parents=True, exist_ok=True)
assert runner.first_action(d) == "unreadable transcript", runner.first_action(d)
p = Path(config.STATE_DIR) / "malformed.jsonl"
p.write_text("{partial")
assert runner.first_action(p) == "malformed transcript"
'

check "native Codex tool calls block replay just like Claude actions" '
import json
from pathlib import Path
from scheduler import config, runner
p = Path(config.STATE_DIR) / "native.jsonl"
for kind in ["function_call", "custom_tool_call", "local_shell_call", "unknown_call"]:
    p.write_text(json.dumps({"type": "response_item", "payload": {"type": kind, "name": "exec_command"}}))
    assert runner.first_action(p) == "exec_command", kind
p.write_text(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": []}}))
assert runner.first_action(p) is None
'

# The engine checks below pin session_jsonl_path at a hermetic file: the real
# derivation puts it under ~/.claude/projects, and a test has no business
# writing there. The derivation itself is not under test here — what is, is that
# the gate reads the dead run and acts on the answer.

check "an api_error retry fires when the dead run only read, and carries the marker" '
import json
from datetime import datetime, timezone
from pathlib import Path
from scheduler import config, engine, runner
NOW = datetime(2026, 9, 11, 23, 2, 51, tzinfo=timezone.utc)
JOB = {"id": "retry-clean", "agent_id": "plain", "enabled": True,
       "schedule": {"kind": "once"}}
class FakeRun:
    run_id, session_id, model, retry_of = "cecd0cb9", "s1", "opus", None
    job, job_id = JOB, JOB["id"]
    workspace = "/tmp/ws"
    scheduled_for = NOW
    scheduled_for_ms = spawned_at_ms = int(NOW.timestamp() * 1000)
p = Path(config.STATE_DIR) / "clean.jsonl"
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}}]}}))
runner.session_jsonl_path = lambda ws, sid: p
fired = []
e = engine.Engine(now_fn=lambda: NOW)
e._try_fire = lambda *a, **k: fired.append(k)
e._on_run_finish(FakeRun(), status="api_error", exit_code=1, kill_reason=None, summary="")
assert len(fired) == 1, fired
assert fired[0]["retry_of"] == "cecd0cb9", fired[0]
assert fired[0]["retry_reason"] == "api_error", fired[0]
'

check "an api_error retry is withheld when the dead run already acted" '
import json
from datetime import datetime, timezone
from pathlib import Path
from scheduler import config, engine, journal, runner
NOW = datetime(2026, 9, 11, 23, 2, 51, tzinfo=timezone.utc)
JOB = {"id": "retry-dirty", "agent_id": "plain", "enabled": True,
       "schedule": {"kind": "once"}}
class FakeRun:
    run_id, session_id, model, retry_of = "cecd0cb9", "s1", "opus", None
    job, job_id = JOB, JOB["id"]
    workspace = "/tmp/ws"
    scheduled_for = NOW
    scheduled_for_ms = spawned_at_ms = int(NOW.timestamp() * 1000)
p = Path(config.STATE_DIR) / "dirty.jsonl"
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "Bash",
     "input": {"command": "git merge --no-ff issue-52 && git push"}}]}}))
runner.session_jsonl_path = lambda ws, sid: p
fired = []
e = engine.Engine(now_fn=lambda: NOW)
e._try_fire = lambda *a, **k: fired.append(k)
e._on_run_finish(FakeRun(), status="api_error", exit_code=1, kill_reason=None, summary="")
assert fired == [], fired                       # the merge is not re-run
st = e.state["retry-dirty"]
assert "not retried" in st["last_error"], st    # and the state says why
assert "git merge" in st["last_error"], st
rec = journal.read_history(job_id="retry-dirty")[0]
assert rec["status"] == "api_error", rec
assert rec["retryOf"] is None, rec
'

check "a withheld retry delivers a terminal failure instead of going silent" '
import json
from datetime import datetime, timezone
from pathlib import Path
from scheduler import config, engine, journal, runner, spawn
NOW = datetime(2026, 9, 11, 23, 2, 51, tzinfo=timezone.utc)
JOB = {"id": "retry-notify", "agent_id": "plain", "enabled": True,
       "notify_on_failure": True, "schedule": {"kind": "recurring"}}
class FakeRun:
    run_id, session_id, model, retry_of = "cecd0cb9", "s1", "opus", None
    job, job_id = JOB, JOB["id"]
    workspace = "/tmp/ws"
    scheduled_for = NOW
    scheduled_for_ms = spawned_at_ms = int(NOW.timestamp() * 1000)
p = Path(config.STATE_DIR) / "notify.jsonl"
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "mcp__meta-api__threads_publish",
     "input": {"text": "hi"}}]}}))
runner.session_jsonl_path = lambda ws, sid: p
config._install = dict(config.install()); config._install["failure_notifier"] = "x:y"
seen = []
spawn._load_hook = lambda spec: (lambda payload: seen.append(payload))
e = engine.Engine(now_fn=lambda: NOW)
e._try_fire = lambda *a, **k: seen.append(("FIRED", k))
e._on_run_finish(FakeRun(), status="api_error", exit_code=1, kill_reason=None, summary="")
# the decision passes to a human: delivered once, and NOT as a spawn
assert len(seen) == 1 and seen[0].get("job_id") == "retry-notify", seen
rec = journal.read_history(job_id="retry-notify")[0]
assert rec["deliveryStatus"] == "delivered", rec
'

check "a run that never got a session retries; one whose transcript cannot be located does not" '
from datetime import datetime, timezone
from scheduler import engine
NOW = datetime(2026, 9, 11, 23, 2, 51, tzinfo=timezone.utc)
JOB = {"id": "retry-nosession", "agent_id": "plain", "enabled": True}
class NeverSpawned:            # died before Run.spawn set anything — the ttft case
    run_id, session_id, workspace, model, retry_of = "r1", None, None, "opus", None
    job, job_id = JOB, JOB["id"]
    scheduled_for = NOW
    scheduled_for_ms = spawned_at_ms = int(NOW.timestamp() * 1000)
class Adopted(NeverSpawned):   # a session ran; a pre-`workspace` state entry lost where
    session_id, workspace = "s1", None
e = engine.Engine(now_fn=lambda: NOW)
assert e._retry_blocker(NeverSpawned()) is None
assert e._retry_blocker(Adopted()) == "transcript location unknown"
'

# ── and the marker actually arrives, through a real spawn ──
#
# Criterion: watch a retry carry it, not read the diff. This spawns the real
# Run against a stub claude that dumps the argv it was invoked with, so what is
# asserted is the message the session is handed.

mkdir -p "$TMP/tools"
cat > "$TMP/tools/claude" <<STUB
#!/bin/sh
printf '%s\n' "\$@" > "$TMP/claude-argv.txt"
exit 0
STUB
chmod +x "$TMP/tools/claude"

check "a real retry spawn hands the session the marker, ahead of the payload" '
import os
from datetime import datetime, timezone
from pathlib import Path
from scheduler import config, runner
HOME = Path(os.environ["SCHEDULER_HOME"])
JOB = {"id": "retry-spawn", "agent_id": "plain", "name": "Nightly publish",
       "payload": {"message": "publish the queued post"},
       "claude_bin": str(HOME / "tools" / "claude")}
def spawn_message(**kw):
    r = runner.Run(job=JOB, defaults=dict(config.BUILTIN_DEFAULTS),
                   scheduled_for=datetime.now(timezone.utc),
                   on_finish=lambda *a, **k: None, **kw).spawn()
    r.proc.wait()
    dump = (HOME / "claude-argv.txt").read_text()
    return dump.split("\n-p\n", 1)[1]          # -p is last; the rest is the message

first = spawn_message()
assert first.startswith("[cron:retry-spawn Nightly publish] "), first
assert "RETRY" not in first, first             # a first attempt is told nothing

msg = spawn_message(retry_of="cecd0cb9", retry_reason="api_error")
# the routing prefix still leads — thread classification matches on it
assert msg.startswith("[cron:retry-spawn Nightly publish] "), msg
assert "[RETRY of run cecd0cb9" in msg, msg
assert "api_error" in msg, msg
# and it lands BEFORE the instruction it qualifies, not after
assert msg.index("[RETRY") < msg.index("publish the queued post"), msg
assert msg.rstrip().endswith("publish the queued post"), msg
'

# ── failure delivery: a terminal non-ok finish reaches a human (#17) ──
#
# The outage that filed it: a daily job died two mornings running, every record
# stamped deliveryStatus not-requested, and the only reason anyone noticed was a
# human asking. The seam is an install-owned failure_notifier; the opt-in is the
# per-job notify_on_failure. These pin who is called, when, and what the record
# then says — the last being the field that used to be a lie on every row.

check "a terminal failure on an opted-in job is delivered and stamped on the record" '
from datetime import datetime, timezone
from scheduler import engine, config, spawn, journal
NOW = datetime(2026, 9, 8, 6, 30, 5, tzinfo=timezone.utc)
JOB = {"id": "notify-hit", "agent_id": "plain", "enabled": True,
       "notify_on_failure": True, "schedule": {"kind": "recurring"}}
class FakeRun:
    run_id, session_id, model, retry_of = "r1", "s1", "opus", None
    job, job_id = JOB, JOB["id"]
    scheduled_for_ms = spawned_at_ms = int(NOW.timestamp() * 1000)
config._install = dict(config.install()); config._install["failure_notifier"] = "x:y"
seen = []
spawn._load_hook = lambda spec: (lambda payload: seen.append((spec, payload)))
e = engine.Engine(now_fn=lambda: NOW)
e._on_run_finish(FakeRun(), status="error", exit_code=1, kill_reason=None, summary="boom")
assert len(seen) == 1, seen
spec, payload = seen[0]
assert spec == "x:y", spec
assert payload["job_id"] == "notify-hit" and payload["status"] == "error", payload
assert payload["consecutive_errors"] == 1, payload
rec = journal.read_history(job_id="notify-hit")[0]
assert rec["deliveryStatus"] == "delivered" and rec["delivered"] is True, rec
'

check "a failure on a job that did not opt in stays silent and not-requested" '
from datetime import datetime, timezone
from scheduler import engine, config, spawn, journal
NOW = datetime(2026, 9, 8, 6, 30, 5, tzinfo=timezone.utc)
JOB = {"id": "notify-miss", "agent_id": "plain", "enabled": True,
       "schedule": {"kind": "recurring"}}
class FakeRun:
    run_id, session_id, model, retry_of = "r1", "s1", "opus", None
    job, job_id = JOB, JOB["id"]
    scheduled_for_ms = spawned_at_ms = int(NOW.timestamp() * 1000)
config._install = dict(config.install()); config._install["failure_notifier"] = "x:y"
seen = []
spawn._load_hook = lambda spec: (lambda payload: seen.append(payload))
e = engine.Engine(now_fn=lambda: NOW)
e._on_run_finish(FakeRun(), status="error", exit_code=1, kill_reason=None, summary="")
assert seen == [], seen
rec = journal.read_history(job_id="notify-miss")[0]
assert rec["deliveryStatus"] == "not-requested" and rec["delivered"] is False, rec
'

check "opted in with no failure_notifier installed records no-notifier and does not raise" '
from datetime import datetime, timezone
from scheduler import engine, config, journal
NOW = datetime(2026, 9, 8, 6, 30, 5, tzinfo=timezone.utc)
JOB = {"id": "notify-none", "agent_id": "plain", "enabled": True,
       "notify_on_failure": True, "schedule": {"kind": "recurring"}}
class FakeRun:
    run_id, session_id, model, retry_of = "r1", "s1", "opus", None
    job, job_id = JOB, JOB["id"]
    scheduled_for_ms = spawned_at_ms = int(NOW.timestamp() * 1000)
config._install = dict(config.install())  # failure_notifier stays None
e = engine.Engine(now_fn=lambda: NOW)
e._on_run_finish(FakeRun(), status="error", exit_code=1, kill_reason=None, summary="")
rec = journal.read_history(job_id="notify-none")[0]
assert rec["deliveryStatus"] == "no-notifier", rec
'

check "a failure_notifier that raises is swallowed and recorded failed" '
from datetime import datetime, timezone
from scheduler import engine, config, spawn, journal
NOW = datetime(2026, 9, 8, 6, 30, 5, tzinfo=timezone.utc)
JOB = {"id": "notify-boom", "agent_id": "plain", "enabled": True,
       "notify_on_failure": True, "schedule": {"kind": "recurring"}}
class FakeRun:
    run_id, session_id, model, retry_of = "r1", "s1", "opus", None
    job, job_id = JOB, JOB["id"]
    scheduled_for_ms = spawned_at_ms = int(NOW.timestamp() * 1000)
config._install = dict(config.install()); config._install["failure_notifier"] = "x:y"
def boom(spec):
    def _raise(payload):
        raise RuntimeError("delivery down")
    return _raise
spawn._load_hook = boom
e = engine.Engine(now_fn=lambda: NOW)
e._on_run_finish(FakeRun(), status="error", exit_code=1, kill_reason=None, summary="")
rec = journal.read_history(job_id="notify-boom")[0]
assert rec["deliveryStatus"] == "failed", rec
'

check "a transient stall that will be retried does not deliver" '
from datetime import datetime, timezone
from pathlib import Path
from scheduler import engine, config, spawn, journal
NOW = datetime(2026, 9, 8, 6, 30, 5, tzinfo=timezone.utc)
JOB = {"id": "notify-stall", "agent_id": "plain", "enabled": True,
       "notify_on_failure": True, "schedule": {"kind": "recurring"}}
class FakeRun:
    run_id, session_id, model, retry_of = "r1", "s1", "opus", None
    job, job_id = JOB, JOB["id"]
    scheduled_for = NOW
    # A spawned run always has one (Run.spawn sets it first); without it the
    # retry gate cannot locate the transcript and withholds the retry, which
    # would make this a test of the wrong arm.
    workspace = "/tmp/ws"
    _jsonl = Path(config.STATE_DIR) / "no-actions.jsonl"
    _jsonl.write_text("{\"type\":\"user\"}\n")
    scheduled_for_ms = spawned_at_ms = int(NOW.timestamp() * 1000)
config._install = dict(config.install()); config._install["failure_notifier"] = "x:y"
seen = []
spawn._load_hook = lambda spec: (lambda payload: seen.append(payload))
e = engine.Engine(now_fn=lambda: NOW)
e._try_fire = lambda *a, **k: None   # a retry is attempted; do not spawn a real one
e._on_run_finish(FakeRun(), status="error", exit_code=1, kill_reason="stall", summary="")
assert seen == [], seen
rec = journal.read_history(job_id="notify-stall")[0]
assert rec["deliveryStatus"] == "not-requested", rec
'

check "a terminal spawn failure on an opted-in job delivers too" '
from datetime import datetime, timezone
from scheduler import engine, config, spawn, registry, journal
NOW = datetime(2026, 9, 8, 6, 30, 5, tzinfo=timezone.utc)
JOB = {"id": "spawn-notify", "agent_id": "plain", "enabled": True,
       "notify_on_failure": True, "schedule": {"kind": "recurring"}}
config._install = dict(config.install()); config._install["failure_notifier"] = "x:y"
seen = []
spawn._load_hook = lambda spec: (lambda payload: seen.append(payload))
e = engine.Engine(now_fn=lambda: NOW)
eff = registry.effective(JOB, dict(config.BUILTIN_DEFAULTS), {})
# retry_of set: the spawn retry already failed, so this one is terminal.
e._spawn_failure(JOB, eff, NOW, "no binary", "prev", dict(config.BUILTIN_DEFAULTS))
assert len(seen) == 1, seen
assert seen[0]["status"] == "error", seen[0]
rec = journal.read_history(job_id="spawn-notify")[0]
assert rec["deliveryStatus"] == "delivered", rec
'

check "notify_on_failure opts in at the job or category layer, off by default" '
from scheduler import resolve, registry, config
assert "notify_on_failure" in resolve.INHERITED_KEYS
D = dict(config.BUILTIN_DEFAULTS)
# a job opts itself in
assert registry.effective({"notify_on_failure": True}, D).get("notify_on_failure") is True
# a category opts a whole class in
cats = {"loud": {"notify_on_failure": True}}
assert registry.effective({"category": "loud"}, D, cats).get("notify_on_failure") is True
# nobody set it → off (BUILTIN_DEFAULTS ships it False). Opt-in only: like every
# value this resolver layers, a setting "provides" only when truthy, so absence
# reads as off and there is no accidental on.
assert not registry.effective({"agent_id": "x"}, D).get("notify_on_failure")
'

# ── the zone label names the instant, not now ──

check "_tz_label names the instant it labels, across the DST boundary" '
from datetime import datetime
from scheduler.cli import _tz_label
# Both sides asserted in the same run under the config zone: whatever today
# is, one of these is out of season, so an implementation reading `now`
# cannot get both right.
assert _tz_label(datetime(2026, 1, 15, 9, 0)) == "PST", _tz_label(datetime(2026, 1, 15, 9, 0))
assert _tz_label(datetime(2026, 7, 15, 9, 0)) == "PDT", _tz_label(datetime(2026, 7, 15, 9, 0))
# and to the minute at the transition itself — 2026-11-01 02:00 local is when
# the clocks go back.
assert _tz_label(datetime(2026, 11, 1, 1, 59)) == "PDT", _tz_label(datetime(2026, 11, 1, 1, 59))
assert _tz_label(datetime(2026, 11, 1, 3, 0)) == "PST", _tz_label(datetime(2026, 11, 1, 3, 0))
# no argument is still the honest answer for now, not a crash
assert _tz_label() in ("PST", "PDT"), _tz_label()
'

check "add-once names the fires-at time in the fires-at zone" '
import argparse, io, contextlib
from scheduler import cli, registry
args = argparse.Namespace(
    agent="demo", at="2026-01-15 09:00", message="m", name=None, workspace=None,
    timeout_seconds=60, category=None, resume_session=None, delete_after_run=True,
    locked=False, json=False)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    cli.cmd_add_once(args)
out = buf.getvalue()
assert "fires 2026-01-15 09:00 PST" in out, out
job = [j for j in registry.load_registry()["jobs"] if j["name"].startswith("demo wake")][-1]
assert job["name"] == "demo wake 2026-01-15 09:00 PST", job["name"]
'

# ── ics VTIMEZONE derivation ──

check "VTIMEZONE derives a US DST rule" '
from scheduler import ics
lines = ics.vtimezone("America/Los_Angeles")
assert "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU" in lines, lines
assert "DTSTART:19700308T020000" in lines, lines
assert "TZNAME:PDT" in lines and "TZNAME:PST" in lines
'

check "VTIMEZONE derives a last-Sunday rule" '
from scheduler import ics
lines = ics.vtimezone("Europe/Berlin")
assert "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU" in lines, lines
'

check "VTIMEZONE for a zone without DST has no recurrence" '
from scheduler import ics
lines = ics.vtimezone("Asia/Tokyo")
assert not [l for l in lines if l.startswith("RRULE")], lines
assert "BEGIN:DAYLIGHT" not in lines
'

# ── registry round-trip through the real CLI ──

if "$PY" -m scheduler.cli add-once --agent plain --at "2030-01-01 09:00" \
        --message "test wake" --name "jstack selftest" >"$TMP/add.out" 2>&1; then
    JOB_ID=$(sed -n 's/.*added once job \([0-9a-f-]*\).*/\1/p' "$TMP/add.out")
    if [ -n "$JOB_ID" ] && "$PY" -m scheduler.cli list --all 2>/dev/null | grep -q "jstack selftest"; then
        pass "registry round-trip: job added and listed"
        if "$PY" -m scheduler.cli rm "$JOB_ID" --force >/dev/null 2>&1 \
           && ! "$PY" -m scheduler.cli list --all 2>/dev/null | grep -q "jstack selftest"; then
            pass "registry round-trip: job removed"
        else
            fail "registry round-trip: job not removed"
        fi
    else
        fail "registry round-trip: job not listed after add ($(cat "$TMP/add.out"))"
    fi
else
    fail "registry round-trip: add-once failed ($(cat "$TMP/add.out"))"
fi

# ── the daemon boots and serves /health ──

PORT=$(( 19000 + RANDOM % 2000 ))
SCHEDULER_API_PORT="$PORT" SCHEDULER_TICK_SECONDS=0.5 \
    "$PY" -m scheduler >"$TMP/daemon.out" 2>&1 &
DAEMON_PID=$!
health=""
for _ in $(seq 1 40); do
    health=$(curl -s --max-time 1 "http://127.0.0.1:$PORT/health" 2>/dev/null) && [ -n "$health" ] && break
    health=""
done
if echo "$health" | grep -q '"ok": *true'; then
    pass "daemon boots and serves /health"
else
    fail "daemon never became healthy ($(tail -5 "$TMP/daemon.out" 2>/dev/null))"
fi
kill "$DAEMON_PID" 2>/dev/null
wait "$DAEMON_PID" 2>/dev/null

echo
if [ "$fails" -eq 0 ]; then
    echo "PASS — scheduler package + install seam verified"
    exit 0
fi
echo "$fails check(s) failed"
exit 1
