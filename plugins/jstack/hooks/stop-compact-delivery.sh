#!/bin/sh
# The installed host owns opt-in preferences and the managed terminal.
[ "${SKIP_SESSION_HOOK:-}" = 1 ] && exit 0
command -v jstack-host >/dev/null 2>&1 || exit 0
exec jstack-host compact-delivery
