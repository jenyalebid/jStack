"""Model-bound text longer than a sentence lives in a prompt file, never in code.

The rule (plugins/jstack/rules-stage/prompt-sourcing.md) stood for weeks while the
context-ceiling notices, the compact-on-delivery nudges, the Codex carry header, the
plan-gate refusals, the turn budget and the memory ceiling all held their prose inline —
and one of those inline paragraphs went on telling sessions "a finished delivery is left
alone" after a switch made that false. A rule nothing measures is a suggestion, so this
reads every hook and every host module and fails on a multi-sentence literal.

Operator-facing modules — CLI output, install errors, SQL — are not model-bound and are
named below with the reason. A new module is enforced by default.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOST = REPO / "host" / "jstack_host"
HOOKS = REPO / "plugins" / "jstack" / "hooks"

#: Text read by a person at a terminal or written to a database, never by a model.
OPERATOR_FACING = {
    HOST / "cli.py": "CLI output",
    HOST / "adopt_offline.py": "install/adopt CLI output",
    HOST / "build_source.py": "release build errors",
    HOST / "build_hub.py": "release build errors",
    HOST / "publish_release.py": "release CLI output",
    HOST / "attach_parent.py": "attach CLI errors",
    HOST / "enrolment.py": "pairing errors returned to the app",
    HOST / "grants.py": "mesh grant errors returned to the app",
    HOST / "hostenv.py": "state-dir misconfiguration errors",
    HOST / "server.py": "startup errors",
    HOST / "codex_hooks.py": "a comment header written into a TOML file",
    HOST / "feed.py": "SQL schema",
    HOST / "store.py": "SQL schema",
    HOOKS / "tag-command.py": "/tag replies are shown to the user, not the model",
}

#: A sentence ending followed by the next one starting.
_BOUNDARY = re.compile(r"[a-z0-9)\]`'\"][.!?]\s+[A-Z(`\"*]")
_MIN_LEN = 80


def _literals(tree):
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):
            yield node.lineno, "".join(
                v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)


def offenders(source: str, where: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return [f"{where}:{line}: {text[:70]!r}" for line, text in _literals(tree)
            if len(text) > _MIN_LEN and _BOUNDARY.search(text)]


def _shell_python(text: str) -> list[str]:
    """The `python3 -c '…'` bodies inside a shell hook — code a scan of .py files misses."""
    return re.findall(r"python3 -c '\n(.*?)\n'", text, re.DOTALL)


def _heredoc_prose(text: str) -> list[str]:
    return [body for body in re.findall(r"<<-?'?(\w+)'?\n(.*?)\n\1\n", text, re.DOTALL)
            for body in [body[1]] if _BOUNDARY.search(body)]


def scan() -> list[str]:
    found: list[str] = []
    for path in sorted([*HOST.glob("*.py"), *HOOKS.glob("*.py")]):
        if path not in OPERATOR_FACING:
            found += offenders(path.read_text(), str(path.relative_to(REPO)))
    for path in sorted(HOOKS.glob("*.sh")):
        text = path.read_text()
        for body in _shell_python(text):
            found += offenders(body, str(path.relative_to(REPO)))
        found += [f"{path.relative_to(REPO)}: heredoc {b[:60]!r}" for b in _heredoc_prose(text)]
    return found


def test_no_hook_or_host_module_holds_multi_sentence_prose_inline():
    assert scan() == [], (
        "model-bound text longer than a sentence belongs in a prompts/*.md file "
        "(plugins/jstack/prompts/ for hooks, host/jstack_host/prompts/ for the host):\n"
        + "\n".join(scan()))


def test_the_scan_catches_what_it_is_for():
    inline = 'NOTE = ("First sentence of a notice that is long enough to count. "\n' \
             '        "Second sentence that makes it a paragraph, which belongs in a file.")\n'
    assert offenders(inline, "x.py")
    assert _heredoc_prose("cat <<'X'\nOne sentence here. Another one there.\nX\n")


def test_every_operator_facing_exemption_still_exists():
    assert [p for p in OPERATOR_FACING if not p.exists()] == []


def test_every_host_prompt_file_ships():
    from jstack_host import prompt_files
    assert sorted(p.name for p in prompt_files.PROMPTS_DIR.glob("*.md"))
