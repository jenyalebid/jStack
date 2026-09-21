#!/usr/bin/env bash
# jStack live test — bin/issue-worktree, the two-sessions-one-worktree interlock.
#
# Driven against REAL processes and REAL git worktrees, not stubs. The whole
# value of this binary is what it sees on a live machine, and a stubbed `ps`
# would only prove that a fixture parses. Fake sessions are made with
# `exec -a claude`, which gives a genuine process whose argv[0] is `claude` —
# the same thing the scan identifies a session by — and whose cwd is a real
# directory lsof can be asked about.
#
#   (a) a fresh issue creates the worktree and says so
#   (b) a second dispatch with a live session holding the issue REFUSES,
#       names its pid and start time, and creates nothing        <- the issue
#   (c) it refuses on a session whose argv has NO issue ref but whose cwd is
#       the worktree — the `--resume` shape an argv-only
#       scan waves straight through, because the number never reaches argv
#   (d) it does NOT refuse itself. The scanning session's own argv carries the
#       issue ref; unguarded, every dispatch refuses itself and the skill can
#       never run at all
#   (e) `#420` does not match `#42` — a digit boundary, not a substring
#   (f) a process carrying the ref that is not an agent CLI is not an owner
#       (`JStackRuntime local claude-max-api` contains "claude")
#   (g) a DEAD owner is a resume, and the receipt says what was checked, so
#       "resumed" is never silent
#   (h) an unreadable process table refuses rather than reporting a clear
#       machine — a look that failed must not answer as a look that found
#       nothing
#
# Exit 0 = all pass, 1 = any fail. Hermetic: its own git repo under $TMPDIR,
# its own worktrees, and every process it starts is killed on the way out.

set -uo pipefail

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$PLUGIN_ROOT/bin/issue-worktree"

[[ -x "$BIN" ]] || { echo "FAIL: issue-worktree not executable at $BIN" >&2; exit 1; }
command -v git  >/dev/null 2>&1 || { echo "FAIL: git not on PATH"  >&2; exit 1; }
command -v lsof >/dev/null 2>&1 || { echo "FAIL: lsof not on PATH" >&2; exit 1; }

TMP="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/jstack-issuewt.XXXXXX")" && pwd -P)"
KIDS=()
cleanup() {
  for pid in "${KIDS[@]:-}"; do [[ -n "${pid:-}" ]] && kill "$pid" 2>/dev/null; done
  # Worktrees first: removing the repo out from under a registered worktree
  # leaves entries pointing at nothing in this test's own scratch area.
  git -C "$TMP/repo" worktree prune 2>/dev/null
  rm -rf "$TMP"
}
trap cleanup EXIT

fails=0
ok()  { echo "  ok   $1"; }
bad() { echo "  FAIL $1 — $2"; fails=$((fails + 1)); }

# ── a repo to work in ───────────────────────────────────────────────────────
REPO="$TMP/repo"
mkdir -p "$REPO"
git -C "$REPO" init -q
git -C "$REPO" config user.email t@example.com
git -C "$REPO" config user.name  Test
echo seed > "$REPO/README.md"
git -C "$REPO" add README.md
git -C "$REPO" -c commit.gpgsign=false commit -qm seed
git -C "$REPO" symbolic-ref HEAD refs/heads/main 2>/dev/null
git -C "$REPO" branch -M main 2>/dev/null

SLUG="acme/widget"

# Start a process that looks exactly like an agent session to the scan:
# argv[0] is `claude`, argv carries $2, and it stands in $1.
#   spawn_session <cwd> <argv-marker> [argv0]
spawn_session() {
  local cwd="$1" marker="$2" argv0="${3:-claude}" pid
  # Two commands, not one. `bash -c "sleep 120 # marker"` is a single simple
  # command, so bash exec-optimizes itself away and the process becomes a bare
  # `sleep 120` — losing both the spoofed argv[0] and the marker, which makes
  # the fixture silently stop resembling a session at all.
  #
  # stdout and stderr go to /dev/null, not to the caller's pipe. A background
  # child that inherits the command substitution's stdout keeps it open, and
  # `$(spawn_session ...)` then blocks for the child's full lifetime.
  ( cd "$cwd" && exec -a "$argv0" /bin/bash -c "sleep 120; true # $marker" ) >/dev/null 2>&1 &
  pid=$!
  KIDS+=("$pid")
  # The subshell exec's in place, so $! is the final process. Wait for the
  # argv rewrite to be visible before anything scans for it.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    ps -p "$pid" -o command= 2>/dev/null | grep -qF -- "$marker" && break
    sleep 0.2
  done
  echo "$pid"
}

# Run the binary as the CHILD of a fake session that carries $1 in its argv —
# which is what a real dispatch looks like from the inside. Assertion (d)
# turns on the tool excluding that ancestor rather than refusing to it.
run_under_session() {
  local marker="$1"; shift
  # Same exec-optimization trap as spawn_session, and here it would fake a
  # PASS: with a single command bash replaces itself with the binary, so the
  # ancestor carrying the ref never exists and (d) proves nothing. The extra
  # statements keep the fake session alive as a real parent, and carry the
  # child's exit code back out rather than reporting `true`'s.
  ( exec -a claude /bin/bash -c '"$@"; __rc=$?; exit $__rc # '"$marker" _ "$@" )
}

# ── (a) a fresh issue creates the worktree ──────────────────────────────────
WT42="$TMP/pad/issue-42"
out="$("$BIN" --repo "$SLUG" --issue 42 --path "$WT42" --repo-root "$REPO" 2>&1)"; rc=$?
if [[ $rc -eq 0 && "$out" == *"state=created"* && -d "$WT42" ]]; then
  ok "fresh issue creates the worktree"
else
  bad "fresh issue" "rc=$rc out=$(echo "$out" | tr '\n' ' ')"
fi

# ── (b) a live owner is refused, by name ────────────────────────────────────
owner_pid="$(spawn_session "$TMP" "-p /jstack:issue $SLUG#42")"
out="$("$BIN" --repo "$SLUG" --issue 42 --path "$WT42" --repo-root "$REPO" 2>&1)"; rc=$?
if [[ $rc -ne 3 ]]; then
  bad "live owner refused" "expected exit 3, got $rc — a second session would have proceeded"
elif [[ "$out" != *"$owner_pid"* ]]; then
  bad "refusal names the pid" "pid $owner_pid absent from the refusal"
elif ! echo "$out" | grep -qE 'started [A-Z][a-z]{2} [A-Z][a-z]{2}'; then
  bad "refusal names the start time" "no start time in: $(echo "$out" | tr '\n' ' ')"
elif [[ "$out" != *"state=refused"* ]]; then
  bad "refusal receipt" "no machine-readable refused line"
else
  ok "a live owner is refused, with pid and start time"
fi
kill "$owner_pid" 2>/dev/null; wait "$owner_pid" 2>/dev/null

# ── (c) the adopted-issue shape: no ref in argv, standing in the worktree ──
# A session that adopted its issue from a comment on a different one is started
# `--resume <sid>` and has no issue reference anywhere in its argv. argv alone
# cannot see it.
cwd_pid="$(spawn_session "$WT42" "--resume a1b2c3d4 --model opus")"
out="$("$BIN" --repo "$SLUG" --issue 42 --path "$WT42" --repo-root "$REPO" 2>&1)"; rc=$?
if [[ $rc -eq 3 && "$out" == *"$cwd_pid"* ]]; then
  ok "a session standing in the worktree is an owner, with no ref in its argv"
else
  bad "cwd-based ownership" "rc=$rc (expected 3) — the --resume shape was missed"
fi
kill "$cwd_pid" 2>/dev/null; wait "$cwd_pid" 2>/dev/null

# ── (d) it does not refuse itself ───────────────────────────────────────────
# The caller's own argv carries the ref, exactly as a real dispatch's does.
out="$(run_under_session "-p /jstack:issue $SLUG#42" \
        "$BIN" --repo "$SLUG" --issue 42 --path "$WT42" --repo-root "$REPO" 2>&1)"; rc=$?
if [[ $rc -eq 0 ]]; then
  ok "a session does not refuse itself"
else
  bad "self-exclusion" "rc=$rc — the dispatch refused its own ancestor, so the skill can never run"
fi

# ── (e) a digit boundary, not a substring ───────────────────────────────────
near_pid="$(spawn_session "$TMP" "-p /jstack:issue $SLUG#420")"
out="$("$BIN" --repo "$SLUG" --issue 42 --path "$WT42" --repo-root "$REPO" 2>&1)"; rc=$?
if [[ $rc -eq 0 ]]; then
  ok "#420 is not #42"
else
  bad "digit boundary" "rc=$rc — a session on #420 blocked #42"
fi
kill "$near_pid" 2>/dev/null; wait "$near_pid" 2>/dev/null

# ── (f) identity is argv[0], not a substring of the line ────────────────────
runtime_pid="$(spawn_session "$TMP" "local claude-max-api $SLUG#42" "JStackRuntime")"
out="$("$BIN" --repo "$SLUG" --issue 42 --path "$WT42" --repo-root "$REPO" 2>&1)"; rc=$?
if [[ $rc -eq 0 ]]; then
  ok "a non-session carrying the ref is not an owner"
else
  bad "argv0 identity" "rc=$rc — matched a process that is not an agent CLI"
fi
kill "$runtime_pid" 2>/dev/null; wait "$runtime_pid" 2>/dev/null

# ── (g) a dead owner is a resume, and it is not silent ──────────────────────
out="$("$BIN" --repo "$SLUG" --issue 42 --path "$WT42" --repo-root "$REPO" 2>&1)"; rc=$?
if [[ $rc -ne 0 || "$out" != *"state=resumed"* ]]; then
  bad "dead owner resumes" "rc=$rc out=$(echo "$out" | tr '\n' ' ')"
elif [[ "$out" != *"no live session holds it"* ]]; then
  bad "resume is not silent" "the receipt does not say what was checked: $(echo "$out" | tr '\n' ' ')"
else
  ok "a dead owner's worktree resumes, and the receipt says what was checked"
fi

# ── (h) a scan that cannot look refuses ─────────────────────────────────────
STUB="$TMP/stub"; mkdir -p "$STUB"
printf '#!/bin/sh\nexit 1\n' > "$STUB/ps"
chmod +x "$STUB/ps"
out="$(PATH="$STUB:$PATH" "$BIN" --repo "$SLUG" --issue 42 --path "$WT42" --repo-root "$REPO" 2>&1)"; rc=$?
if [[ $rc -eq 3 && "$out" == *"state=refused"* ]]; then
  ok "an unreadable process table refuses rather than reporting a clear machine"
else
  bad "fail-closed scan" "rc=$rc — a blind scan read as 'nobody is working this'"
fi

echo
if [[ $fails -eq 0 ]]; then
  echo "PASS — the interlock holds"
  exit 0
fi
echo "FAIL — $fails assertion(s)"
exit 1
