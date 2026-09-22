#!/usr/bin/env bash
# The same release workflow can be invoked from either product repository.
set -euo pipefail
RELEASE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASE_PYTHON="${JSTACK_RELEASE_PYTHON:-$RELEASE_ROOT/host/.venv/bin/python3}"

# The Hub bundle carries its own runtime, copied out of the interpreter that
# builds it, and `build_hub._build` accepts exactly one: the audited CPython
# 3.12 framework. The host's own venv tracks whatever the host runs on — it
# moved to 3.14 — so taking it by default cost a full release attempt that died
# nine frames deep in "this runtime build requires the audited CPython 3.12
# framework", naming the requirement and never the interpreter that broke it.
# Refuse here instead, where the remedy is in reach.
if [ -z "${JSTACK_RELEASE_PYTHON:-}" ]; then
    RUNTIME="$("$RELEASE_PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
    if [ "$RUNTIME" != "3.12" ]; then
        AUDITED=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3
        if [ -x "$AUDITED" ]; then
            RELEASE_PYTHON="$AUDITED"
        else
            echo "release: $RELEASE_PYTHON is Python ${RUNTIME:-unknown}, and the Hub runtime" >&2
            echo "  must be built by the audited CPython 3.12 framework, which is not installed" >&2
            echo "  at $AUDITED. Install it, or set JSTACK_RELEASE_PYTHON to one." >&2
            exit 1
        fi
    fi
fi

exec env PYTHONPATH="$RELEASE_ROOT/host${PYTHONPATH:+:$PYTHONPATH}" \
    "$RELEASE_PYTHON" -m jstack_host.publish_release "$@"
