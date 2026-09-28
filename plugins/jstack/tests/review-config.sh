#!/bin/bash
# review.json's location, resolved once — jenyalebid/jStack#190.
#
# Before this fix, thirteen readers (5 hooks, 8 bins) each hard-coded the
# $JSTACK_REVIEW_CONFIG / ~/.claude/jstack/review.json fallback independently.
# They agreed by coincidence, not construction: a reader missed in a future
# edit would silently resolve a different config from the rest. This asserts
# the single owner both halves of that promise: the module answers the
# override and the default correctly, and nothing else in the tree still
# carries the literal it replaced.

set -u
PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${JSTACK_PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || { echo "FAIL: no python3 on PATH (set JSTACK_PYTHON)"; exit 1; }

. "$PLUGIN_ROOT/tests/lib/pin-plugin-root.sh"

TMP=$(mktemp -d /tmp/jstack-review-config.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

fails=0
fail() { echo "FAIL: $1" >&2; fails=$((fails+1)); }
pass() { echo "ok: $1"; }

# --- default: $HOME/.claude/jstack/review.json, override unset --------------

out=$(HOME="$TMP/fakehome" "$PY" - <<'EOF' 2>&1
import os, review_config
from pathlib import Path
want = Path(os.environ["HOME"]) / ".claude" / "jstack" / "review.json"
assert review_config.path() == want, review_config.path()
assert review_config.load() == {}
print("OK")
EOF
)
[ "$out" = "OK" ] && pass "no override: defaults to ~/.claude/jstack/review.json, missing file loads {}" \
                   || fail "default path/empty load: $out"

# --- override wins, and a well-formed dict loads back ------------------------

mkdir -p "$TMP/cfg"
echo '{"agent_root": "/tmp/agents", "mail": {"poll": 5}}' > "$TMP/cfg/review.json"
out=$(JSTACK_REVIEW_CONFIG="$TMP/cfg/review.json" "$PY" - <<'EOF' 2>&1
import os, review_config
from pathlib import Path
assert review_config.path() == Path(os.environ["JSTACK_REVIEW_CONFIG"])
cfg = review_config.load()
assert cfg == {"agent_root": "/tmp/agents", "mail": {"poll": 5}}, cfg
print("OK")
EOF
)
[ "$out" = "OK" ] && pass "JSTACK_REVIEW_CONFIG overrides the default and loads its contents" \
                   || fail "override path/load: $out"

# --- malformed / non-dict content degrades to {}, never raises --------------

echo 'not json' > "$TMP/cfg/bad.json"
echo '[1, 2]' > "$TMP/cfg/list.json"
for name in bad list; do
    out=$(JSTACK_REVIEW_CONFIG="$TMP/cfg/$name.json" "$PY" -c \
        'import review_config; print(review_config.load())' 2>&1)
    [ "$out" = "{}" ] && pass "malformed $name.json loads as {} rather than raising" \
                       || fail "$name.json: expected {}, got: $out"
done

# --- no reader still carries the literal this module now owns ---------------
#
# The regression this guards: a fourteenth call site added later (or one of
# the thirteen edited back) that spells out the default path itself instead
# of asking review_config — the exact drift #190 was filed over.

hits=$(grep -rl '\.claude["'"'"'"]* */ *["'"'"'"]jstack["'"'"'"]* */ *["'"'"'"]review\.json' \
    "$PLUGIN_ROOT/hooks" "$PLUGIN_ROOT/bin" "$PLUGIN_ROOT/skills" 2>/dev/null \
    | grep -v '/review_config\.py$')
if [ -z "$hits" ]; then
    pass "no hook/bin/skill outside review_config.py still constructs the review.json path itself"
else
    fail "still hard-coding the review.json path: $hits"
fi

echo
if [ "$fails" -eq 0 ]; then
    echo "review-config.sh: all checks passed"
    exit 0
else
    echo "review-config.sh: $fails check(s) failed"
    exit 1
fi
