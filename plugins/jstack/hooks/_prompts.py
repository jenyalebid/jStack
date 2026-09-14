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
