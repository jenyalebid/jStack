#!/bin/sh
# The environment's dispatcher: one hooks.json entry per event, every trigger behind it.
# Adding a trigger is a registry change (`jstack-host trigger list`), never an edit here
# or in hooks.json — Codex keys hook trust by an entry's position, so a new entry there
# disarms Codex until someone trusts it again.
event=${1:-}
[ -n "$event" ] || exit 0
[ "${SKIP_SESSION_HOOK:-}" = 1 ] && exit 0

# Nothing listens to this event: skip the host entirely. `.armed` is rewritten by every
# dispatch and by `jstack-host trigger arm`; a missing file means ask the host.
armed="${JSTACK_TRIGGERS_DIR:-$HOME/.config/jstack/triggers}/.armed"
if [ -f "$armed" ] && ! grep -qx "$event" "$armed"; then
    cat >/dev/null
    exit 0
fi

# Where the host is, not what it is called — the same search stop-compact-delivery.sh
# learned the hard way (a removed PATH symlink silently ended every delivery).
host=$(command -v jstack-host 2>/dev/null)
if [ -z "$host" ]; then
    for cand in "$HOME/.local/bin/jstack-host" \
                "${JSTACK_CHECKOUT:-$HOME/jStack}/host/.venv/bin/jstack-host"; do
        if [ -x "$cand" ]; then host=$cand; break; fi
    done
fi
[ -n "$host" ] || { cat >/dev/null; exit 0; }

exec env -u PYTHONPATH -u PYTHONDONTWRITEBYTECODE "$host" trigger dispatch "$event"
