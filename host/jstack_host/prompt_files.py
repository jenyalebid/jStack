"""Loader for the host's injected-text templates.

The law (plugins/jstack/rules-stage/prompt-sourcing.md): model-bound text longer
than a sentence lives in a file, never inline in code. The plugin's hooks keep
theirs in `plugins/jstack/prompts/`; the host keeps its own in `prompts/` beside
this module, because the host ships as a package without the plugin tree and
the text has to travel with the code that sends it.

Same shape as the plugin's `hooks/_prompts.py`: a whole file, or one
`## section` of it, with runtime values `.format()`ed in by the caller.
"""
from __future__ import annotations

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

_SECTION = re.compile(r"(?m)^## (\S+)\n(.*?)(?=^## \S+$|\Z)", re.DOTALL)


def load(name: str, section: str | None = None) -> str:
    """The template `prompts/<name>`, whole or one `## section` of it."""
    text = (PROMPTS_DIR / name).read_text()
    if section is None:
        return text
    for match in _SECTION.finditer(text):
        if match.group(1) == section:
            return match.group(2).strip("\n")
    raise KeyError(f"{name} has no section {section!r}")
