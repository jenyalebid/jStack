#!/usr/bin/env python3
"""PreToolUse — tell a session it has got heavy, while ending the turn is still cheap.

The load meter's bands, not the client's: `heavy` at 160k, `extreme` at 200k,
the same cuts the app and the client's chip draw. Auto-compact is a backstop
that should never be reached — by the time the client takes its own boundary
the session has been overpaying for tens of turns, and that boundary lands
between two tool calls, mid-task.

The rules are `jstack_host.context_ceiling`, which reads the transcript in both
dialects and fires once per band on the CROSSING. This file is the wiring: find
the package through `_host.py`, hand it stdin, stay silent about everything else.
A hook that dies is a hook switched off for the rest of the session, so nothing
here is allowed to raise.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    try:
        import _host  # the resolver: checkout, then the marketplace it came from
        context_ceiling = _host.load("context_ceiling")
    except Exception:
        return 0     # no host this plugin can reach: the meter is not this hook's
    try:
        return context_ceiling.main()
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
