"""review_config — the one place review.json's location is decided.

Thirteen readers each carried the same literal by coincidence, not by
construction: `root.py` reads no config file by contract, so nothing owned
this. Move the file, or add a second default, and whichever reader is not
updated reads a different config from the rest.

Stdlib only, imports nothing from the package — loaded both as a sibling and
by standalone bin/ scripts that insert the plugin dir themselves, so an
import from the tree would close a cycle on the first module reading it back.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def path() -> Path:
    """Where review.json lives: $JSTACK_REVIEW_CONFIG, else ~/.claude/jstack/review.json."""
    return Path(os.environ.get(
        "JSTACK_REVIEW_CONFIG",
        str(Path.home() / ".claude" / "jstack" / "review.json"),
    )).expanduser()


def load() -> dict:
    """The parsed config, or {} on anything short of a well-formed dict.

    A missing or malformed file is a normal state (a fresh install, a typo
    mid-edit) — every caller here already tolerated it, so the shared reader
    must too rather than raising where thirteen call sites used to swallow.
    """
    try:
        data = json.loads(path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
