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


#: The one prefix every machine-sent user turn opens with (rules-stage/
#: prompt-sourcing.md, "A machine never speaks as the user"). A session reads
#: an untagged user turn as its person; anything a hook, spawn, wake or nudge
#: puts in that seat says so first. The plugin and the host each carry a copy
#: because the host ships without the plugin tree — a test pins them equal.
INJECTED_TAG = "[system prompt]"


def tagged(text: str) -> str:
    """`text` as a machine-sent user turn: `INJECTED_TAG` first, exactly once.

    Idempotent, so every layer a send passes through can enforce it without
    stacking. A slash command is left bare — the CLI only runs one that opens
    the line, and a command is an action, not speech anyone could mistake.
    """
    if not text or not text.strip():
        return text
    body = text.lstrip()
    if body.startswith(INJECTED_TAG) or body.startswith("/"):
        return text
    return f"{INJECTED_TAG} {body}"
