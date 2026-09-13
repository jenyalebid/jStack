#!/bin/bash
# The tree derivation this package stands on: one root, everything by structure.
#
# Without this file, root.py's precedence order is held by nothing but the
# docstring that states it — and precedence is exactly what a live install
# hangs from. A running daemon points individual dirs at its own tree while
# JSTACK_ROOT stays free for the derivation; the moment a derived path
# outranks an explicit one, that daemon's state moves out from under it
# mid-flight and every job it owns re-fires or vanishes. The other promise
# held here is portability: agent answers must come from the declared root,
# not from whatever private ~/Agents exists on the machine that wrote the
# code — a resolver that silently finds the home tree passes everywhere it
# was developed and nowhere else. And the guard: one precedence bug that
# roots a data dir inside this public checkout puts a live token one
# `git add -A` from being published.

set -u
PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${JSTACK_PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || { echo "FAIL: no python3 on PATH (set JSTACK_PYTHON)"; exit 1; }

TMP=$(mktemp -d /tmp/jstack-root.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

# Every `import root` below must be THIS tree's root.py. PYTHONPATH does not
# settle that on its own — the pre-push gate's interpreter carries a .pth that
# fronts the main checkout, which is how the shipping-tree guard below came to
# compare two different trees and fail. See tests/lib/pin-plugin-root.sh.
. "$PLUGIN_ROOT/tests/lib/pin-plugin-root.sh"
# Hermetic baseline: the ambient machine may declare any of these, and every
# check below states its own environment outright.
unset JSTACK_ROOT JSTACK_AGENTS_DIR JSTACK_SYSTEMS_DIR JSTACK_CONFIG_DIR \
      JSTACK_STATE_DIR JSTACK_LOGS_DIR JSTACK_CREDENTIALS_DIR

fails=0
fail() { echo "FAIL: $1" >&2; fails=$((fails+1)); }
pass() { echo "ok: $1"; }

ROOT_A="$TMP/root-a"; ROOT_B="$TMP/root-b"; FAKEHOME="$TMP/fakehome"
mkdir -p "$ROOT_A" "$ROOT_B" "$FAKEHOME"

# --- the root declaration and the six-dir derivation -------------------------

out=$(HOME="$FAKEHOME" JSTACK_ROOT="$ROOT_A" "$PY" - <<'EOF' 2>&1
import os, root
from pathlib import Path
r = Path(os.environ["JSTACK_ROOT"])
assert root.root() == r, root.root()
for fn, leaf in [("agents_dir", "Agents"), ("systems_dir", "Systems"),
                 ("config_dir", "Config"), ("state_dir", "State"),
                 ("logs_dir", "Logs"), ("credentials_dir", "Credentials")]:
    got = getattr(root, fn)()
    assert got == r / leaf, (fn, str(got))
print("OK")
EOF
)
if [ "$out" = "OK" ]; then
    pass "JSTACK_ROOT wins over HOME and all six dirs derive from it"
else
    fail "root derivation: $out"
fi

out=$(JSTACK_ROOT="$ROOT_A" JSTACK_LOGS_DIR="$ROOT_B/logs-here" "$PY" - <<'EOF' 2>&1
import os, root
from pathlib import Path
assert root.logs_dir() == Path(os.environ["JSTACK_LOGS_DIR"]), root.logs_dir()
assert root.state_dir() == Path(os.environ["JSTACK_ROOT"]) / "State", root.state_dir()
print("OK")
EOF
)
if [ "$out" = "OK" ]; then
    pass "a dir's own env override beats the derivation; its siblings still derive"
else
    fail "env override precedence: $out"
fi

out=$(JSTACK_ROOT="$ROOT_A" CFG_STATE="$ROOT_B/state-cfg" ENV_STATE="$ROOT_B/state-env" \
      "$PY" - <<'EOF' 2>&1
import os, root
from pathlib import Path
cfg = {"state_dir": os.environ["CFG_STATE"]}
assert root.state_dir(cfg) == Path(os.environ["CFG_STATE"]), root.state_dir(cfg)
# Same process, environment changed between calls: the env override must take
# effect immediately. A value cached at import passes every one-shot check and
# still answers a long-lived daemon with the environment of its process start.
os.environ["JSTACK_STATE_DIR"] = os.environ["ENV_STATE"]
assert root.state_dir(cfg) == Path(os.environ["ENV_STATE"]), root.state_dir(cfg)
print("OK")
EOF
)
if [ "$out" = "OK" ]; then
    pass "a cfg key beats the derivation, loses to its env override — resolved per call"
else
    fail "cfg precedence: $out"
fi

out=$(JSTACK_ROOT="$ROOT_A" AG="$ROOT_B/MyAgents" "$PY" - <<'EOF' 2>&1
import os, root
from pathlib import Path
cfg = {"agent_root": os.environ["AG"]}
assert root.agents_dir(cfg) == Path(os.environ["AG"]), root.agents_dir(cfg)
print("OK")
EOF
)
if [ "$out" = "OK" ]; then
    pass "agents_dir reads the legacy cfg key 'agent_root' installs already set"
else
    fail "agent_root legacy key: $out"
fi

# The precedence a live install depends on: an install that declares its dirs
# explicitly keeps them, whatever JSTACK_ROOT says. Break this and a running
# daemon's state moves mid-flight.
out=$(JSTACK_ROOT="$ROOT_A" JSTACK_STATE_DIR="$ROOT_B/live-state" "$PY" - <<'EOF' 2>&1
import os, root
from pathlib import Path
assert root.state_dir() == Path(os.environ["JSTACK_STATE_DIR"]), root.state_dir()
print("OK")
EOF
)
if [ "$out" = "OK" ]; then
    pass "JSTACK_STATE_DIR outranks the JSTACK_ROOT derivation — the live daemon's ground"
else
    fail "live-install precedence: $out"
fi

# --- the shipping-tree guard -------------------------------------------------

if out=$(JSTACK_STATE_DIR="$PLUGIN_ROOT/state" "$PY" -c "import root; root.state_dir()" 2>&1); then
    fail "a state dir inside the shipping checkout was accepted: $out"
elif printf '%s' "$out" | grep -q "checkout that ships"; then
    pass "a data dir inside the shipping checkout raises, naming the tree"
else
    fail "guard raised without naming the checkout: $(printf '%s' "$out" | tail -2)"
fi

if out=$(JSTACK_AGENTS_DIR="$PLUGIN_ROOT/tests" "$PY" -c "import root; print(root.agents_dir())" 2>&1); then
    pass "agents_dir inside a repo is allowed — only the data dirs are guarded"
else
    fail "agents_dir wrongly guarded: $out"
fi

# --- what an agent is --------------------------------------------------------

AGROOT="$TMP/agentroot"
mkdir -p "$AGROOT/Agents/alpha" \
         "$AGROOT/Agents/bravo/chat" "$AGROOT/Agents/bravo/pad" \
         "$AGROOT/Agents/bravo/.claude" \
         "$AGROOT/Agents/notes" \
         "$AGROOT/Agents/.hidden" \
         "$AGROOT/Agents/Work-Ops"
touch "$AGROOT/Agents/alpha/CLAUDE.md" \
      "$AGROOT/Agents/bravo/chat/CLAUDE.md" \
      "$AGROOT/Agents/.hidden/CLAUDE.md" \
      "$AGROOT/Agents/Work-Ops/CLAUDE.md" \
      "$AGROOT/Agents/notes/readme.txt"

out=$(HOME="$FAKEHOME" JSTACK_ROOT="$AGROOT" "$PY" - <<'EOF' 2>&1
import os, root
from pathlib import Path
base = Path(os.environ["JSTACK_ROOT"]) / "Agents"

got = root.agents()
assert got == ["Work-Ops", "alpha", "bravo"], got  # notes/ and .hidden/ are not agents

assert root.resolve_agent("alpha") == base / "alpha"          # exact
assert root.resolve_agent("ALPHA") == base / "alpha"          # case fold
assert root.resolve_agent("Work-Ops") == base / "Work-Ops"    # exact
assert root.resolve_agent("work-ops") == base / "Work-Ops"    # case fold
assert root.resolve_agent("work_ops") == base / "Work-Ops"    # separator fold
assert root.resolve_agent("workops") == base / "Work-Ops"     # separator fold
assert root.resolve_agent("ghost") is None                    # genuine miss
assert root.resolve_agent("notes") is None                    # a folder, not an agent
assert root.resolve_agent(".hidden") is None                  # dotdirs never resolve
ws = root.resolve_agent("bravo")
assert ws is not None and ws.is_dir(), ws                     # never a nonexistent path

assert root.seats("bravo") == ["chat"], root.seats("bravo")   # pad/ has no CLAUDE.md
assert root.seats("alpha") == [], root.seats("alpha")
assert root.seats("ghost") == [], root.seats("ghost")
print("OK")
EOF
)
if [ "$out" = "OK" ]; then
    pass "agents/resolve_agent/seats agree on the one definition of an agent"
else
    fail "agent definition: $out"
fi

# --- the address grammar -----------------------------------------------------
#
# One spelling of a seat, shared by mail, the scheduler and every command that
# takes an @-token. The hyphen is structure here, never a spelling variant:
# resolve_agent's fold reads `work-ops` and `workops` as one agent, and reached
# for by an addressing caller it answers None for every hyphenated seat id.

ADDR="$TMP/addrroot"
mkdir -p "$ADDR/Agents/alice/chat" "$ADDR/Agents/alice/social/chat" \
         "$ADDR/Agents/alice/social/threads" "$ADDR/Agents/alice/service-call" \
         "$ADDR/Agents/alice/social/threads/worktree" \
         "$ADDR/Agents/alice/pad/checkout" \
         "$ADDR/Agents/work-ops/chat" \
         "$ADDR/Agents/bare"
touch "$ADDR/Agents/alice/CLAUDE.md" "$ADDR/Agents/alice/chat/CLAUDE.md" \
      "$ADDR/Agents/alice/social/CLAUDE.md" \
      "$ADDR/Agents/alice/social/chat/CLAUDE.md" \
      "$ADDR/Agents/alice/social/threads/CLAUDE.md" \
      "$ADDR/Agents/alice/service-call/CLAUDE.md" \
      "$ADDR/Agents/alice/pad/checkout/CLAUDE.md" \
      "$ADDR/Agents/work-ops/chat/CLAUDE.md" \
      "$ADDR/Agents/bare/CLAUDE.md"

out=$(HOME="$FAKEHOME" JSTACK_ROOT="$ADDR" "$PY" - <<'EOF' 2>&1
import os, root
from pathlib import Path
A = Path(os.environ["JSTACK_ROOT"]) / "Agents"
R = root.resolve_seat

# an agent alone is its cockpit; a leading @ is optional; case does not matter
assert R("alice").path == A / "alice/chat", R("alice")
assert R("@alice").path == A / "alice/chat"
assert R("ALICE").path == A / "alice/chat"
# a bare agent — CLAUDE.md on top, no chat/ — keeps its cockpit at the root
assert R("bare").path == A / "bare", R("bare")
assert R("bare").id == "bare", R("bare").id   # no seat dir to name

# hyphens walk down, and a seat holding its own chat/ means that operator seat
assert R("alice-social").path == A / "alice/social/chat", R("alice-social")
assert R("alice-social-threads").path == A / "alice/social/threads"
# a hyphen inside a real directory name beats reading it as a nested pair
assert R("alice-service-call").path == A / "alice/service-call"
# an agent whose own name holds a hyphen wins over agent+seat on the same prefix
assert R("work-ops-chat").path == A / "work-ops/chat", R("work-ops-chat")
assert R("work-ops").path == A / "work-ops/chat"

# a pad is never a seat and is never walked through to find one — what lands
# in one is checkouts, and a checkout carries a CLAUDE.md of its own
for miss in ("alice-pad", "alice-pad-checkout"):
    try:
        R(miss); raise SystemExit(f"{miss} resolved; a pad is not a seat")
    except root.AddressError:
        pass

# a miss names what does exist and never joins a path blind
for miss in ("ghost", "alice-nope", ""):
    try:
        R(miss); raise SystemExit(f"{miss!r} resolved to something")
    except root.AddressError as exc:
        # the message has to be usable by whoever typed it
        assert "alice" in str(exc) or "agent" in str(exc), exc

# every spelling comes from one resolution, and the id round-trips
s = R("alice-social-threads")
assert (s.agent, s.submode) == ("alice", "social/threads"), s
assert s.id == "alice-social-threads" and s.timeline == "alice/social/threads"
assert R(s.id).path == s.path

# the reverse direction: a cwd names the seat it is standing in...
assert root.seat_at(A / "alice/social/threads").id == "alice-social-threads"
# ...and the nearest enclosing seat is what a session below one belongs to
E = root.enclosing_seat
assert E(A / "alice/social/threads/worktree").id == "alice-social-threads"
assert E(A / "alice/pad/checkout").id == "alice", E(A / "alice/pad/checkout")
assert E(A / "alice/chat").id == "alice-chat"
assert E(Path("/tmp")) is None
# seat_of stays the two-value shim its existing callers unpack
assert root.seat_of(A / "alice/social/threads") == ("alice", "social/threads")
print("OK")
EOF
)
if [ "$out" = "OK" ]; then
    pass "one address grammar: hyphens walk seats, cockpits descend, pads never resolve"
else
    fail "address grammar: $out"
fi

EMPTY="$TMP/empty-root"; mkdir -p "$EMPTY"
out=$(HOME="$FAKEHOME" JSTACK_ROOT="$EMPTY" "$PY" - <<'EOF' 2>&1
import root
assert root.agents() == [], root.agents()
assert root.resolve_agent("anyone") is None
assert root.seats("anyone") == []
print("OK")
EOF
)
if [ "$out" = "OK" ]; then
    pass "a root with no Agents/ answers empty, never raises — mid-install is normal"
else
    fail "missing agents_dir tolerance: $out"
fi

# --- bare-root proof ---------------------------------------------------------
# A root containing ONLY Agents/alice/CLAUDE.md, and HOME pointed at an empty
# dir so a fallback to the real home tree finds nothing to hide behind. Every
# answer must come from inside the declared tmpdir — a run that passes because
# it silently found the machine's own ~/Agents proves nothing.

BARE="$TMP/bare"; BAREHOME="$TMP/bare-home"
mkdir -p "$BARE/Agents/alice" "$BAREHOME"
touch "$BARE/Agents/alice/CLAUDE.md"

out=$(HOME="$BAREHOME" JSTACK_ROOT="$BARE" "$PY" - <<'EOF' 2>&1
import os, root
bare = os.environ["JSTACK_ROOT"]
for a in (root.root(), root.agents_dir(), root.systems_dir(), root.config_dir(),
          root.state_dir(), root.logs_dir(), root.credentials_dir()):
    assert str(a).startswith(bare), a
assert root.agents() == ["alice"], root.agents()
ws = root.resolve_agent("alice")
assert ws is not None and str(ws).startswith(bare) and ws.is_dir(), ws
assert root.resolve_agent("ALICE") == ws
assert root.seats("alice") == []
print("OK")
EOF
)
if [ "$out" = "OK" ]; then
    pass "bare root: every answer comes from the declared tmpdir, no private tree involved"
else
    fail "bare-root proof: $out"
fi

# The gate three callers used to each carry a copy of: on a machine laid out
# as Agents/<id>/<seat>/CLAUDE.md with nothing at the agent's top, every one of
# them refused to name the seat the session was in — msg could not say who was
# sending. seat_of is now the single answer.
BR="$TMP/seatroot"
mkdir -p "$BR/Agents/seatonly/chat" "$BR/Agents/topped/social/chat" "$BR/Agents/notanagent/sub"
touch "$BR/Agents/seatonly/chat/CLAUDE.md" "$BR/Agents/topped/CLAUDE.md"
if out=$(env JSTACK_ROOT="$BR" HOME="$TMP/emptyhome" "$PY" -c '
import root
assert root.seat_of("'"$BR"'/Agents/seatonly/chat") == ("seatonly", "chat"), root.seat_of("'"$BR"'/Agents/seatonly/chat")
assert root.seat_of("'"$BR"'/Agents/topped") == ("topped", "chat")
assert root.seat_of("'"$BR"'/Agents/topped/social/chat") == ("topped", "social/chat")
assert root.seat_of("'"$BR"'/Agents/notanagent/sub") == (None, None)
assert root.seat_of("/tmp") == (None, None)
assert root.seat_of(None) == (None, None)
' 2>&1); then
    pass "seat_of names a seat-only agent, per-dir submodes, and refuses a non-agent"
else
    fail "seat_of — $out"
fi

if out=$(env JSTACK_ROOT="$BR" HOME="$TMP/emptyhome" "$PY" -c '
import root
from pathlib import Path
b = Path("'"$BR"'/Agents")
assert root.is_agent(b / "topped") is True          # CLAUDE.md at the top
assert root.is_agent(b / "seatonly") is True        # only a seat carries one
assert root.is_agent(b / "notanagent") is False     # neither
assert root.is_agent(b / "nope") is False           # not there at all
' 2>&1); then
    pass "is_agent is public, and answers about a directory named by the caller"
else
    fail "is_agent — $out"
fi

# --- the timeline is ONE db, so it gets ONE answer ---------------------------
# log_event writes it, msg files exchanges into it, and the session-end engine
# exports the path to every spawn and then reads the row count back to prove a
# write happened. Three literals agreeing is not one location: move any one and
# all three still succeed, against different files.

if out=$(env JSTACK_ROOT="$ROOT_A" HOME="$FAKEHOME" "$PY" -c '
import os, root
from pathlib import Path
r = Path(os.environ["JSTACK_ROOT"])
assert root.timeline_dir() == r / "Logs" / "Timeline", root.timeline_dir()
' 2>&1); then
    pass "timeline_dir derives to {root}/Logs/Timeline"
else
    fail "timeline_dir derivation — $out"
fi

if out=$(env HOME="$FAKEHOME" "$PY" -c '
import root
from pathlib import Path
# No root declared: the derived answer must be the literal these three tools
# each shipped, or adopting the derivation silently moves every install.
assert root.timeline_dir() == Path("'"$FAKEHOME"'/Logs/Timeline"), root.timeline_dir()
' 2>&1); then
    pass "with no JSTACK_ROOT the derived answer IS the pre-root literal ~/Logs/Timeline"
else
    fail "timeline_dir unconfigured — $out"
fi

if out=$(env JSTACK_ROOT="$ROOT_A" JSTACK_LOGS_DIR="$ROOT_B/logs-here" HOME="$FAKEHOME" "$PY" -c '
import root
from pathlib import Path
# Nested under logs, not re-derived from the root: a host that moves its logs
# must not leave the timeline behind in a Logs/ nobody writes to.
assert root.timeline_dir() == Path("'"$ROOT_B"'/logs-here/Timeline"), root.timeline_dir()
' 2>&1); then
    pass "timeline_dir follows JSTACK_LOGS_DIR rather than re-deriving from the root"
else
    fail "timeline_dir under moved logs — $out"
fi

if out=$(env JSTACK_ROOT="$ROOT_A" JSTACK_TIMELINE_DIR="$ROOT_B/tl" HOME="$FAKEHOME" "$PY" -c '
import root
from pathlib import Path
assert root.timeline_dir() == Path("'"$ROOT_B"'/tl"), root.timeline_dir()
# cfg is consulted only when the env is silent — an explicit export outranks a
# config file, the same order every other dir here uses.
assert root.timeline_dir({"timeline_dir": "/nope"}) == Path("'"$ROOT_B"'/tl")
' 2>&1); then
    pass "JSTACK_TIMELINE_DIR outranks the derivation and the cfg key"
else
    fail "timeline_dir env precedence — $out"
fi

if out=$(env JSTACK_ROOT="$ROOT_A" HOME="$FAKEHOME" "$PY" -c '
import root
from pathlib import Path
assert root.timeline_dir({"timeline_dir": "'"$ROOT_B"'/from-cfg"}) == Path("'"$ROOT_B"'/from-cfg")
' 2>&1); then
    pass "a cfg timeline_dir still wins over the derivation (review.json keeps its say)"
else
    fail "timeline_dir cfg precedence — $out"
fi

if out=$(env JSTACK_TIMELINE_DIR="$PLUGIN_ROOT/Timeline" HOME="$FAKEHOME" "$PY" -c '
import root
try:
    root.timeline_dir()
except RuntimeError as e:
    assert "public git tree" in str(e), e
else:
    raise AssertionError("timeline_dir accepted a path inside the shipping checkout")
' 2>&1); then
    pass "timeline_dir refuses to put the db inside this public checkout"
else
    fail "timeline_dir guard — $out"
fi

# A relative root is refused, not repaired.
#
# It was accepted, and it reached launchd: WorkingDirectory and
# StandardErrorPath must be absolute, so the daemon exited 78 before running a
# line and the install ended on a FAIL naming the scheduler rather than the
# answer that broke it. Anchoring it to $HOME instead would make one string
# mean two directories, so the rule is refusal — and the message has to carry
# the absolute path to type, or the reader is left where the FAIL left them.
if out=$(env JSTACK_ROOT="dfredsf" HOME="$FAKEHOME" "$PY" -c '
import root
try:
    root.root()
except ValueError as e:
    assert "absolute" in str(e), e
    assert "dfredsf" in str(e), e
else:
    raise AssertionError("root() accepted a relative JSTACK_ROOT")
' 2>&1); then
    pass "a relative JSTACK_ROOT is refused with the absolute path to type"
else
    fail "relative root guard — $out"
fi

# Same rule on the per-dir overrides, which reach the same plist fields.
if out=$(env JSTACK_ROOT="$ROOT_A" JSTACK_STATE_DIR="State" HOME="$FAKEHOME" "$PY" -c '
import root
try:
    root.state_dir()
except ValueError as e:
    assert "JSTACK_STATE_DIR" in str(e), e
else:
    raise AssertionError("state_dir accepted a relative override")
' 2>&1); then
    pass "a relative dir override is refused and names itself, not the root"
else
    fail "relative override guard — $out"
fi

echo
if [ "$fails" -eq 0 ]; then
    echo "PASS — one declaration, the whole tree derives; precedence holds the live install"
    exit 0
fi
echo "$fails check(s) failed"
exit 1
