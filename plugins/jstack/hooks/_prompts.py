"""Loader for the plugin's injected-text templates.

The law (rules-stage/prompt-sourcing.md): model-bound text longer than a
sentence lives in `prompts/*.md`, never inline in hook code. A hook loads a
whole file, or one `## section` of one, and `.format()`s its runtime values
in. Plurals and conditionals are computed by the caller and passed as values
— the files hold prose, never expressions.
"""
from __future__ import annotations

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

_SECTION = re.compile(r"(?m)^## (\S+)\n(.*?)(?=^## \S+$|\Z)", re.DOTALL)


def load(name: str, section: str | None = None) -> str:
    """The template `prompts/<name>`, whole or one `## section` of it.

    Sectioned files hold several fragments for one hook; section bodies
    therefore must not open lines with `## `. Whole-file templates (no
    section asked for) carry whatever markdown they like.
    """
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
