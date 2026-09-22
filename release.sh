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
#
# The remedy is a venv *on* the framework, not the framework itself. A venv
# keeps `sys.base_prefix` pointed at the framework, which is what build_hub
# copies the runtime out of, and it is the only one of the two that can hold
# the release tool's own dependencies. Handing over the bare framework python
# — which is what this guard used to do — trades a build failure nine frames
# deep for an import failure on line one, and cost a second release attempt.
RELEASE_VENV="$RELEASE_ROOT/host/.venv312"
if [ -z "${JSTACK_RELEASE_PYTHON:-}" ]; then
    RUNTIME="$("$RELEASE_PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
    if [ "$RUNTIME" != "3.12" ]; then
        AUDITED=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3
        if [ -x "$RELEASE_VENV/bin/python3" ]; then
            RELEASE_PYTHON="$RELEASE_VENV/bin/python3"
        elif [ -x "$AUDITED" ]; then
            echo "release: $RELEASE_PYTHON is Python ${RUNTIME:-unknown}, and the Hub runtime" >&2
            echo "  must be built by the audited CPython 3.12 framework at $AUDITED." >&2
            echo "  That interpreter carries no dependencies, so build it a venv once:" >&2
            echo "    $AUDITED -m venv $RELEASE_VENV" >&2
            echo "    $RELEASE_VENV/bin/python3 -m pip install -e $RELEASE_ROOT/host" >&2
            exit 1
        else
            echo "release: $RELEASE_PYTHON is Python ${RUNTIME:-unknown}, and the Hub runtime" >&2
            echo "  must be built by the audited CPython 3.12 framework, which is not installed" >&2
            echo "  at $AUDITED. Install it, or set JSTACK_RELEASE_PYTHON to one." >&2
            exit 1
        fi
    fi
fi

# A 3.12 interpreter that cannot import the release tool fails identically to
# no interpreter at all, and says so far less clearly. Check here.
if ! "$RELEASE_PYTHON" -c 'import httpx, cryptography' >/dev/null 2>&1; then
    echo "release: $RELEASE_PYTHON is Python 3.12 but is missing the release tool's" >&2
    echo "  dependencies. Install them into it:" >&2
    echo "    $RELEASE_PYTHON -m pip install -e $RELEASE_ROOT/host" >&2
    exit 1
fi

exec env PYTHONPATH="$RELEASE_ROOT/host${PYTHONPATH:+:$PYTHONPATH}" \
    "$RELEASE_PYTHON" -m jstack_host.publish_release "$@"
