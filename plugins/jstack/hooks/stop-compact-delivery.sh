#!/bin/sh
# The installed host owns opt-in preferences and the managed terminal.
[ "${SKIP_SESSION_HOOK:-}" = 1 ] && exit 0
command -v jstack-host >/dev/null 2>&1 || exit 0
# A shell opened through the Hub's tmux can carry the app interpreter's
# PYTHONPATH; the host CLI locates its own packages, so it never gets it.
exec env -u PYTHONPATH -u PYTHONDONTWRITEBYTECODE jstack-host compact-delivery
