#!/usr/bin/env bash
# jStack test — run shortcuts (host/jstack_host/run_shortcuts.py), via
# host/tests/test_run_shortcuts.py against a throwaway tmux socket.
# Exit 0 = all pass, exit 1 = any fail.

set -u

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="$(cd "$PLUGIN_ROOT/../../host" && pwd)"
PY="${JSTACK_TEST_PYTHON:-python3}"
for cand in "$PY" "$HOST/.venv/bin/python3" "${JSTACK_CHECKOUT:-$HOME/jStack}/host/.venv/bin/python3"; do
    if "$cand" -c "import pytest" >/dev/null 2>&1; then PY=$cand; break; fi
done
cd "$HOST" && PYTHONPATH="$HOST" "$PY" -m pytest -q -p no:cacheprovider tests/test_run_shortcuts.py || exit 1
