"""Translate jRemote's jStack command vocabulary before Codex's slash parser."""
import re
import subprocess
from pathlib import Path

ZERO_TURN = {name: f"JSTACK_{name.upper()}_CMD"
             for name in ("splitoff", "takeover", "print", "tag", "pict")}


def translate(text: str, cwd: str = "") -> str:
    match = re.fullmatch(r"\s*(?:/|\$(?=jstack:))(?:(jstack):)?([a-zA-Z0-9_-]+)([^\S\n].*)?", text, re.S)
    if not match:
        return text
    namespace, name, rest = match.groups()
    if name.lower() in ZERO_TURN:
        return ZERO_TURN[name.lower()] + (rest or "")
    if namespace:
        return "$jstack:" + name + (rest or "")
    if name == "elevator":
        return "$jstack:elevator" + (rest or "")
    # Shared Alpine skills and migrated workspace commands are native skills.
    # Leave Codex's own slash vocabulary intact when names collide.
    native = {"help", "model", "compact", "permissions", "status", "exit", "quit", "clear",
              "resume", "fork", "new", "hooks", "skills", "mcp", "plan", "review", "fast",
              "settings", "theme", "copy", "undo", "diff", "mention", "feedback", "agents", "goal"}
    if name not in native:
        roots = [Path.home() / ".agents/skills"]
        if cwd:
            directory = Path(cwd)
            roots += [p / ".agents/skills" for p in [directory, *directory.parents]]
        if any((root / name / "SKILL.md").is_file() for root in roots):
            return "$" + name + (rest or "")
    return text


def translate_paste(data: bytes, cwd: str = "") -> bytes:
    start, end = b"\x1b[200~", b"\x1b[201~"
    if data.startswith(start) and data.endswith(end):
        try:
            text = data[len(start):-len(end)].decode()
        except UnicodeDecodeError:
            return data
        return start + translate(text, cwd).encode() + end
    return data


def translate_composer(screen: str, cwd: str = "") -> bytes | None:
    """Single-line typed commands, read immediately before their Enter."""
    lines = screen.splitlines()
    prompts = [i for i, line in enumerate(lines) if line.lstrip().startswith("›")]
    if not prompts:
        return None
    index = prompts[-1]
    if index + 1 < len(lines) and lines[index + 1].strip():
        return None  # multiline draft: never guess which text would be erased
    original = lines[index].lstrip()[1:].strip()
    translated = translate(original, cwd)
    if translated == original:
        return None
    return b"\x01\x0b\x1b[200~" + translated.encode() + b"\x1b[201~"


def pending_command(sid: str) -> bytes | None:
    from . import managed
    try:
        screen = subprocess.run(managed._t("capture-pane", "-p", "-J", "-t", managed._name(sid)),
                                capture_output=True, text=True, timeout=2)
        return translate_composer(screen.stdout, workspace(sid)) if screen.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def workspace(sid: str) -> str:
    from . import open_path
    try:
        return open_path.pane_cwd(sid)
    except (OSError, KeyError, RuntimeError):
        return ""
