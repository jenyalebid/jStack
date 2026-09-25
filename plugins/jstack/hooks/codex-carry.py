#!/usr/bin/env python3
"""SessionStart(source=compact) — put back what a server-side compaction dropped.

Codex compacts on the server: the window is rebuilt from the user's own messages
and the developer prompts, and every assistant message and every tool result in
it is gone. No PreCompact note can steer that, because no Codex compaction hook
carries `additionalContext`. What survives is the rollout on disk, so the residue
is read back off the file and injected at each boundary.

The rules are `jstack_host.codex_carry`, which reads the dialect off the file and
stays silent on a Claude transcript. This file is the wiring: find the package
through `_host.py`, hand it stdin, stay silent about everything else. A hook that
dies is a hook switched off for the rest of the session, so nothing here raises.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    try:
        import _host  # the resolver: checkout, then the marketplace it came from
        codex_carry = _host.load("codex_carry")
    except Exception:
        return 0     # no host this plugin can reach: the carry is not this hook's
    try:
        return codex_carry.main()
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
