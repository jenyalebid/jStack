#!/usr/bin/env bash
# jStack live test — the scrub gate reads files that are not staged yet.
#
# A new file is untracked at exactly the moment its author runs the gate
# before `git add`. The gate once listed only `git ls-files`, reported PASS
# without ever opening that file, and the file reached public main carrying
# the banned persona in a docstring. This proves, on a throwaway clone of
# HEAD with the working copy's scrub.sh dropped in:
#   - the clean tree passes (the fixture itself is sound)
#   - an untracked file holding a banned term fails, named file:line
#   - an untracked file whose NAME holds a banned term fails
#   - a .gitignore'd file is not scanned — it cannot ship by accident
# Never touches this checkout's index or working tree.
#
# Exit 0 = all pass, exit 1 = any fail.

set -u

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO_ROOT="$(cd "$PLUGIN_ROOT/../.." && pwd)"

unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_OBJECT_DIRECTORY \
      GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_PREFIX GIT_CONFIG_PARAMETERS

TMP=$(mktemp -d /tmp/jstack-scrub-untracked.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

fails=0
fail() { echo "FAIL: $1" >&2; fails=$((fails+1)); }
pass() { echo "ok: $1"; }

git clone -q --shared "$REPO_ROOT" "$TMP/repo" \
    || { echo "FAIL: cannot clone $REPO_ROOT"; exit 1; }
FIX="$TMP/repo"
cp "$PLUGIN_ROOT/tests/scrub.sh" "$FIX/plugins/jstack/tests/scrub.sh"
SCRUB="$FIX/plugins/jstack/tests/scrub.sh"

# Assembled at runtime so this file is not itself a carrier of the term.
TERM_WORD="Bo""ss"
NAME_WORD="lyn""da"

out=$(bash "$SCRUB" 2>&1); rc=$?
if [ $rc -eq 0 ]; then
    pass "clean clone of HEAD passes"
else
    fail "clean clone of HEAD does not pass — fixture unsound ($out)"
fi

# ── untracked content ──
printf 'x = "%s"\n' "$TERM_WORD" > "$FIX/host/scrubprobe_tmp.py"
out=$(bash "$SCRUB" 2>&1); rc=$?
if [ $rc -ne 0 ] && echo "$out" | grep -q "host/scrubprobe_tmp.py:1: term '$TERM_WORD'"; then
    pass "untracked file with a banned term fails, named file:line"
else
    fail "untracked file with a banned term was not caught (rc=$rc: $out)"
fi
if echo "$out" | grep -q "1 untracked files"; then
    pass "count line reports the untracked file separately"
else
    fail "count line does not split tracked/untracked ($out)"
fi
rm -f "$FIX/host/scrubprobe_tmp.py"

# ── untracked name ──
echo "clean" > "$FIX/host/$NAME_WORD-probe.txt"
out=$(bash "$SCRUB" 2>&1); rc=$?
if [ $rc -ne 0 ] && echo "$out" | grep -q "host/$NAME_WORD-probe.txt: term '$NAME_WORD'"; then
    pass "untracked file whose name holds a banned term fails"
else
    fail "untracked file name was not checked (rc=$rc: $out)"
fi
rm -f "$FIX/host/$NAME_WORD-probe.txt"

# ── ignored files do not ship, so they are not scanned ──
mkdir -p "$FIX/host/__pycache__"
printf 'x = "%s"\n' "$TERM_WORD" > "$FIX/host/__pycache__/probe.py"
out=$(bash "$SCRUB" 2>&1); rc=$?
if [ $rc -eq 0 ]; then
    pass "a .gitignore'd file is not scanned"
else
    fail "a .gitignore'd file was scanned (rc=$rc: $out)"
fi

[ $fails -eq 0 ] || { echo "$fails check(s) failed"; exit 1; }
echo "all scrub-untracked checks passed"
