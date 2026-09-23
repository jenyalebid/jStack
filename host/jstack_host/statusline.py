#!/usr/bin/env python3
"""Claude Code status-line command — the Claude allowance sampler.

**This is the writer for `allowance.py`'s Claude bars.** Without it a host has
only the CLI's own cache, and that cache is written by the `/usage` screen and
nothing else (jStack #133): a machine where nobody opens `/usage` shows no
Usage bars at all, and one where somebody did shows a reading that goes stale
fifteen minutes later and stays that way. A headroom meter that can only be
refreshed by hand is not a meter.

Claude Code hands this command the session's state as JSON on stdin at every
status-line render, and that payload carries `rate_limits.five_hour` and
`rate_limits.seven_day` — each `{used_percentage, resets_at}`, taken from API
response headers. It is the freshest cap reading available anywhere on the
machine and it costs nothing to read. Every live session is therefore a
sampler, so the bars are seconds old whenever anyone is working.

**It prints nothing, on purpose.** The status line is the only place Claude
Code hands out those percentages, so this is a data tap wearing a display
hook's clothes: it reads the payload, records it, and returns empty, which
Claude Code takes as "no custom line" and renders its own. Installing it
therefore does not change anyone's terminal — the one property that let it be
wired by default instead of offered as a choice.

Absolute rule: **this must never fail loudly.** A status-line command that
errors or hangs degrades every session on the machine, so every path is
guarded and a sample is dropped rather than retried.

Writes are throttled (`MIN_WRITE_INTERVAL`) against a stamp file shared by
every session on the host: renders fire far faster than the underlying
percentages move, and each write takes the allowance lock that concurrent
sessions contend for. Shared rather than per-process on purpose — a Mac with
twelve live panes would otherwise write twelve identical samples per interval.

Wired into `~/.claude/settings.json` by `host/tools/claude_setup.py`, which
`install.sh` runs; managed updates keep the path current through
`update_plugins.replace_references`, which already rewrites that file.
"""

import json
import os
import sys
import time
from pathlib import Path

#: Renders are far more frequent than the numbers move, and each write takes a
#: cross-process lock. One sample per host per 30s keeps the meter fresh.
MIN_WRITE_INTERVAL = 30

#: Per-host, not per-process. `TMPDIR` is per-user on macOS, so this is shared
#: by every session the user runs and survives a session ending.
STAMP = Path(os.environ.get("JSTACK_STATUSLINE_STAMP")
             or Path(os.environ.get("TMPDIR", "/tmp")) / "jstack-statusline.stamp")

LABELS = {"five_hour": "Session (5h)", "seven_day": "Week"}


def windows(rate_limits: dict) -> list:
    """The payload's windows, normalised for `allowance.record`.

    Order is fixed rather than dict order so the readout does not reshuffle
    between renders. A window with no percentage is skipped, never zeroed —
    "not measured" and "nothing used" are different answers and the bar draws
    them differently.
    """
    out = []
    for wid in ("five_hour", "seven_day"):
        w = rate_limits.get(wid)
        if not isinstance(w, dict):
            continue
        pct = w.get("used_percentage")
        if pct is None:
            continue
        out.append({"id": wid, "label": LABELS.get(wid, wid),
                    "pct": pct, "resets_at": w.get("resets_at")})
    return out


def should_write() -> bool:
    """Throttle on the stamp's mtime, and claim the interval by touching it
    before the write rather than after — two sessions rendering at once must
    not both decide it is their turn."""
    try:
        if time.time() - STAMP.stat().st_mtime < MIN_WRITE_INTERVAL:
            return False
    except OSError:
        pass
    try:
        STAMP.parent.mkdir(parents=True, exist_ok=True)
        STAMP.touch()
    except OSError:
        return False
    return True


def record(sample: list) -> None:
    """Hand the sample to the store.

    The package is imported here and not at module scope: Claude Code runs
    this file as a script on every render, and the throttle above returns
    without it on all but one render in thirty seconds.
    """
    if not sample:
        return
    if not should_write():
        return
    # The one place `__file__` is the only anchor available: Claude Code runs
    # this file as a script, so there is no package yet to ask `hostenv` where
    # anything is — locating it IS this line. `attention_hook.py` bootstraps
    # the same way and for the same reason.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from jstack_host import allowance
    allowance.record("claude", sample, source="statusline")


def main() -> None:
    """Read, record, print nothing — on every path, including the failures."""
    try:
        payload = json.load(sys.stdin)
        record(windows(payload.get("rate_limits") or {}))
    except Exception:  # noqa: BLE001 — a status line never explains itself
        return


if __name__ == "__main__":
    main()
