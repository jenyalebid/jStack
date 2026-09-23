#!/usr/bin/env python3
"""Wire this machine's Claude Code into jStack — `codex_setup.py`'s counterpart.

Thin CLI over `jstack_host.claude_settings`, which holds the rules and is what
`jstack-doctor` reads. Run by `install.sh`; safe to re-run, and a no-op when
the settings are already right.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jstack_host import claude_settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", default=None,
                        help="the jStack checkout the settings should point at")
    parser.add_argument("--settings", default=None)
    parser.add_argument("--state-dir", default=None,
                        help="carried into the command when it is not the default")
    parser.add_argument("--check", action="store_true",
                        help="report what would change and write nothing")
    args = parser.parse_args()

    print(claude_settings.install(
        path=Path(args.settings) if args.settings else None,
        checkout=Path(args.checkout) if args.checkout else None,
        state_dir=args.state_dir,
        dry_run=args.check))


if __name__ == "__main__":
    main()
