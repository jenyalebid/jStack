#!/usr/bin/env bash
# jStack live test — repo seats.
#
# An IDE fixes a session's cwd to the checked-out repo, so the session lands
# outside {agent_root} and resolves to no seat: no timeline on entry, no entry
# on exit. The agent registry already records which repos each agent owns, so
# the session is resolved back to that agent's own cockpit seat. This exercises
# the real resolver (repo_seat.py / bin/repo-seat) and the real SessionStart
# hook against a hermetic fixture — a temp agents tree, a temp registry, real
# git checkouts, and a temp timeline seeded through the real bin/log_event.
#
#   (a) repo dir named in the registry     → the owning agent's workspace seat
#   (b) a subdirectory of that checkout    → same seat (resolves as the repo)
#   (c) registry spelling differs from the directory (Acme_iOS vs Acme-iOS)
#   (d) a worktree-style clone whose directory name is unknown → seat by origin
#   (e) `<Repo>-issue-42` with no origin   → seat by longest-prefix
#   (f) a repo no entry owns               → no seat, exit 3
#   (g) `"active": false` entries never claim a repo
#   (h) the seat follows the entry's `workspace`, not a fixed "chat"
#   (i) a real seat directory always wins over the registry
#   (j) end to end: SessionStart in an owned repo injects that agent's timeline
#   (k) no registry at all → silent, unchanged behavior
#   (l) the role files walk-up can't reach are injected for a repo session
#   (m) a cockpit session, which walk-up already covers, is not double-loaded
#   (n) inject_identity:false drops the role and keeps the timeline
#
# Exit 0 = all pass, 1 = any fail. Hermetic — never touches the real timeline.

set -u

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$PLUGIN_ROOT/hooks/session-start-inject.py"
SEAT="$PLUGIN_ROOT/bin/repo-seat"
LOG_EVENT="$PLUGIN_ROOT/bin/log_event"

[[ -x "$SEAT" ]] || { echo "FAIL: repo-seat not executable at $SEAT" >&2; exit 1; }
command -v git >/dev/null 2>&1 || { echo "FAIL: git not on PATH" >&2; exit 1; }

TMP="$(mktemp -d "${TMPDIR:-/tmp}/jstack-reposeat.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

ROOT="$TMP/Agents"
CODE="$TMP/code"
mkdir -p "$ROOT/Gamma/chat" "$ROOT/Delta/pm/ios" "$ROOT/Zombie/chat" "$CODE"
printf '# Gamma\nROLE-MARKER-GAMMA\n' > "$ROOT/Gamma/CLAUDE.md"
printf '# Shared protocol\nPROTOCOL-MARKER\n' > "$ROOT/CLAUDE.md"
printf '# Gamma chat seat\nSEAT-MARKER\n' > "$ROOT/Gamma/chat/CLAUDE.md"
printf '# Delta\n' > "$ROOT/Delta/CLAUDE.md"
printf '# Zombie\n' > "$ROOT/Zombie/CLAUDE.md"

mkrepo() {  # mkrepo <dir> [origin-url]
  mkdir -p "$CODE/$1" && git -C "$CODE/$1" init -q 2>/dev/null
  [[ $# -gt 1 ]] && git -C "$CODE/$1" remote add origin "$2"
  return 0
}
mkrepo Widget-iOS
mkdir -p "$CODE/Widget-iOS/Sources/Deep"
mkrepo Gadget_iOS
mkrepo some-checkout-name "git@github.com:org/Widget-iOS.git"
mkdir -p "$CODE/Widget-iOS-issue-42" # deliberately not a git repo: no origin
mkrepo Unowned-iOS
mkrepo Archived-iOS

REG="$TMP/agents.json"
cat > "$REG" <<JSON
{
  "_roles": {"app": "not an agent"},
  "gamma": {"workspace": "$ROOT/Gamma/chat", "repos": ["Widget_iOS"]},
  "delta": {"workspace": "$ROOT/Delta/pm/ios", "repos": ["Gadget-iOS"]},
  "zombie": {"active": false, "workspace": "$ROOT/Zombie/chat",
             "repos": ["Archived-iOS"]}
}
JSON

export JSTACK_TIMELINE_DIR="$TMP/Timeline"
CFG="$TMP/review.json"
cat > "$CFG" <<JSON
{"agent_root": "$ROOT", "agent_registry": "$REG",
 "timeline_inject": {"*/chat": {"n": 5}, "*/pm/ios": {"n": 5}}}
JSON
export JSTACK_REVIEW_CONFIG="$CFG"

PASS=0; FAIL=0
check() { # check <name> <expected> <actual>
  if [[ "$2" == "$3" ]]; then echo "ok: $1"; PASS=$((PASS+1))
  else echo "FAIL: $1"; echo "  expected: $2"; echo "  actual:   $3"; FAIL=$((FAIL+1)); fi
}
seat() { "$SEAT" "$1" 2>/dev/null || echo "EXIT$?"; }

check "(a) registry repo → owning agent's seat"      "gamma/chat" "$(seat "$CODE/Widget-iOS")"
check "(b) subdirectory resolves as the checkout"    "gamma/chat" "$(seat "$CODE/Widget-iOS/Sources/Deep")"
check "(c) _ and - are the same repo name"           "gamma/chat" "$(seat "$CODE/Widget-iOS")"
check "(d) unknown dir name, seat by git origin"     "gamma/chat" "$(seat "$CODE/some-checkout-name")"
check "(e) <Repo>-issue-42 by longest prefix"        "gamma/chat" "$(seat "$CODE/Widget-iOS-issue-42")"
check "(f) unowned repo has no seat"                 "EXIT3"      "$(seat "$CODE/Unowned-iOS")"
check "(g) inactive entry never claims"              "EXIT3"      "$(seat "$CODE/Archived-iOS")"
check "(h) seat follows workspace, not a fixed chat" "delta/pm/ios" "$(seat "$CODE/Gadget_iOS")"
# A seat directory that ALSO looks like a repo the registry hands to another
# agent: the seat it actually sits in has to win, or an agent could be
# renamed out of its own cockpit by a registry line.
git -C "$ROOT/Gamma/chat" init -q 2>/dev/null
git -C "$ROOT/Gamma/chat" remote add origin "git@github.com:org/Gadget-iOS.git"

"$LOG_EVENT" gamma/chat "widget sync fixed" --origin direct --detail "the repo session wrote this" >/dev/null 2>&1

inject() { echo "{\"cwd\":\"$1\"}" | JSTACK_ASSUME_INTERACTIVE=1 python3 "$HOOK" 2>/dev/null; }
case "$(inject "$ROOT/Gamma/chat")" in
  *"widget sync fixed"*) echo "ok: (i) a real seat dir wins over the registry"; PASS=$((PASS+1));;
  *) echo "FAIL: (i) a real seat dir wins over the registry"; FAIL=$((FAIL+1));;
esac

OUT="$(inject "$CODE/Widget-iOS")"
case "$OUT" in
  *"widget sync fixed"*) echo "ok: (j) SessionStart in an owned repo injects the seat"; PASS=$((PASS+1));;
  *) echo "FAIL: (j) SessionStart in an owned repo injects the seat"; echo "  actual: ${OUT:0:200}"; FAIL=$((FAIL+1));;
esac
case "$(inject "$CODE/Unowned-iOS")" in
  "") echo "ok: (j) unowned repo injects nothing"; PASS=$((PASS+1));;
  *) echo "FAIL: (j) unowned repo injects nothing"; FAIL=$((FAIL+1));;
esac

# The role file a repo session can't reach on its own: the workspace is a
# sibling of the checkout, so CLAUDE.md walk-up never climbs into it.
OUT="$(inject "$CODE/Widget-iOS")"
for marker in PROTOCOL-MARKER ROLE-MARKER-GAMMA SEAT-MARKER; do
  case "$OUT" in
    *"$marker"*) echo "ok: (l) repo session is handed $marker"; PASS=$((PASS+1));;
    *) echo "FAIL: (l) repo session is handed $marker"; FAIL=$((FAIL+1));;
  esac
done

# A cockpit session already loads all three by standing in the workspace —
# injecting them again would just pay for them twice.
case "$(inject "$ROOT/Gamma/chat")" in
  *ROLE-MARKER-GAMMA*) echo "FAIL: (m) cockpit session must not double-load its role"; FAIL=$((FAIL+1));;
  *) echo "ok: (m) cockpit session does not double-load its role"; PASS=$((PASS+1));;
esac

cat > "$CFG" <<JSON
{"agent_root": "$ROOT", "agent_registry": "$REG", "inject_identity": false,
 "timeline_inject": {"*/chat": {"n": 5}, "*/pm/ios": {"n": 5}}}
JSON
OUT="$(inject "$CODE/Widget-iOS")"
case "$OUT" in
  *ROLE-MARKER-GAMMA*) echo "FAIL: (n) inject_identity:false suppresses the role"; FAIL=$((FAIL+1));;
  *"widget sync fixed"*) echo "ok: (n) inject_identity:false keeps the timeline, drops the role"; PASS=$((PASS+1));;
  *) echo "FAIL: (n) inject_identity:false keeps the timeline, drops the role"; FAIL=$((FAIL+1));;
esac

cat > "$CFG" <<JSON
{"agent_root": "$ROOT", "timeline_inject": {"*/chat": {"n": 5}}}
JSON
case "$(inject "$CODE/Widget-iOS")" in
  "") echo "ok: (k) no registry → unchanged, silent"; PASS=$((PASS+1));;
  *) echo "FAIL: (k) no registry → unchanged, silent"; FAIL=$((FAIL+1));;
esac

echo
if [[ $FAIL -eq 0 ]]; then echo "repo-seat: all pass ($PASS)"; exit 0
else echo "repo-seat: $FAIL failed, $PASS passed"; exit 1; fi
