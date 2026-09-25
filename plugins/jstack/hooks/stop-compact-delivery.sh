#!/bin/sh
# The installed host owns opt-in preferences and the managed terminal.

# WHERE THE HOST IS, NOT WHAT IT IS CALLED.
#
# `command -v jstack-host` used to be this hook's whole address for the engine, and
# PATH is a nickname the install hands out, not the thing itself: the name resolves
# through one symlink in ~/.local/bin that `install.sh --uninstall` removes and
# nothing else re-creates. On 2026-09-24 that symlink went away while the host stayed
# installed, running and perfectly able to compact. Every Stop after it exited 0 here,
# no session on the machine was compacted for hours, and because the exit is the same
# one a machine without a host gives, not a single line anywhere said so — the fault
# was found by hand, watching a session blow past seam after seam.
#
# So PATH is asked first, because an explicit install outranks a guess, and the places
# install.sh actually writes are asked after it.
host=$(command -v jstack-host 2>/dev/null)
if [ -z "$host" ]; then
    for cand in "$HOME/.local/bin/jstack-host" \
                "${JSTACK_CHECKOUT:-$HOME/jStack}/host/.venv/bin/jstack-host"; do
        if [ -x "$cand" ]; then host=$cand; break; fi
    done
fi

# `--which` is how a health probe asks whether this hook can still reach an engine
# without delivering anything. It answers ahead of every other branch — including the
# skip switch — because a suppressed delivery still has to be able to report where the
# engine is, and it lives here rather than in the probe so the search has exactly one
# definition to keep true.
if [ "${1:-}" = "--which" ]; then
    if [ -n "$host" ]; then echo "$host"; exit 0; fi
    echo "no jstack-host: not on PATH and not at any install location" >&2
    exit 1
fi

[ "${SKIP_SESSION_HOOK:-}" = 1 ] && exit 0

# No host anywhere on this machine: delivery is opt-in, most machines never install
# one, and its absence is not a fault. This is the ONLY silent exit left.
[ -n "$host" ] || exit 0

# A shell opened through the Hub's tmux can carry the app interpreter's
# PYTHONPATH; the host CLI locates its own packages, so it never gets it.
exec env -u PYTHONPATH -u PYTHONDONTWRITEBYTECODE "$host" compact-delivery
