#!/usr/bin/env bash
# The same release workflow can be invoked from either product repository.
set -euo pipefail
RELEASE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASE_PYTHON="${JSTACK_RELEASE_PYTHON:-$RELEASE_ROOT/host/.venv/bin/python3}"
exec env PYTHONPATH="$RELEASE_ROOT/host${PYTHONPATH:+:$PYTHONPATH}" \
    "$RELEASE_PYTHON" -m jstack_host.publish_release "$@"
