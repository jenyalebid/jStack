#!/usr/bin/env python3
"""
jStack: inject-path-rules
=========================

PreToolUse hook for Claude Code. When the model is about to mutate a file
(Edit, Write, MultiEdit, NotebookEdit), this hook scans ~/.claude/rules/*.md
for any rule whose `paths:` frontmatter glob matches the file's absolute
path, and injects matched rule bodies as `additionalContext` — so cross-tree
work picks up the right conventions regardless of launch CWD.

Per-session dedup: each rule's body is shown again only after the transcript
has grown by at least JSTACK_RULE_REINJECT_BYTES bytes (default 400_000 ≈
100K tokens at ~4 bytes/token). Markers live in /tmp/jstack-rule-cache/<sid>/.

Failure mode: any exception causes a silent exit 0 with no output. The hook
NEVER blocks the tool — its only job is to enrich context.

Disable per-session: set env var JSTACK_PATH_RULES_DISABLED=1.

Test overrides (do not use in production):
  JSTACK_RULES_DIR   — override the rules source directory.
  JSTACK_CACHE_ROOT  — override the per-session marker root.
These let `tests/path-rule-injection.sh` run hermetically against a temp
fixture without touching real markers or shipped rules.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from session_runtime import engine, patch_paths

TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"}
DEFAULT_RULES_DIR = Path.home() / ".claude" / "rules"
DEFAULT_CACHE_ROOT = Path("/tmp/jstack-rule-cache")
DEFAULT_REINJECT_BYTES = 400_000


def _rules_dir() -> Path:
    override = os.environ.get("JSTACK_RULES_DIR")
    return Path(override).expanduser() if override else DEFAULT_RULES_DIR


def _cache_root() -> Path:
    override = os.environ.get("JSTACK_CACHE_ROOT")
    return Path(override).expanduser() if override else DEFAULT_CACHE_ROOT


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    if os.environ.get("JSTACK_PATH_RULES_DISABLED"):
        sys.exit(0)

    tool_name = payload.get("tool_name")
    if tool_name not in TOOLS:
        sys.exit(0)

    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
    file_paths = (patch_paths(tool_input.get("command", ""), payload.get("cwd") or os.getcwd())
                  if tool_name == "apply_patch" else [file_path] if isinstance(file_path, str) else [])
    if not file_paths:
        sys.exit(0)

    session_id = payload.get("session_id") or "_unknown"
    transcript_path = payload.get("transcript_path") or ""

    try:
        reinject_bytes = int(os.environ.get("JSTACK_RULE_REINJECT_BYTES", DEFAULT_REINJECT_BYTES))
    except ValueError:
        reinject_bytes = DEFAULT_REINJECT_BYTES

    rules_dir = _rules_dir()
    if not rules_dir.is_dir():
        sys.exit(0)

    matched: list[Path] = []
    for rule_path in sorted(rules_dir.glob("**/*.md")):
        try:
            paths = _parse_paths_frontmatter(rule_path)
            if not paths:
                continue
            if any(_glob_match(g, candidate) for g in paths for candidate in file_paths):
                matched.append(rule_path)
        except Exception:
            continue

    if not matched:
        sys.exit(0)

    current_size = _file_size(transcript_path)
    session_cache = _cache_root() / _safe_dir_name(session_id)
    try:
        session_cache.mkdir(parents=True, exist_ok=True)
    except Exception:
        sys.exit(0)

    to_inject: list[Path] = []
    for rule_path in matched:
        key = rule_path.stem
        if rule_path.parent != rules_dir:
            key += "-" + hashlib.sha256(str(rule_path.relative_to(rules_dir)).encode()).hexdigest()[:16]
        marker = session_cache / f"{key}.marker"
        last = _read_marker(marker)
        if last is None:
            to_inject.append(rule_path)
            _write_marker(marker, current_size)
            continue
        if current_size - last >= reinject_bytes:
            to_inject.append(rule_path)
            _write_marker(marker, current_size)

    if not to_inject:
        sys.exit(0)

    chunks: list[str] = []
    for rule_path in to_inject:
        try:
            body = _strip_frontmatter(rule_path.read_text())
            if not body.strip():
                continue
            chunks.append(f"## Rule: {rule_path.stem}\n\n{body.strip()}")
        except Exception:
            continue

    if not chunks:
        sys.exit(0)

    additional = (
        f"Path-matched rules for {', '.join(file_paths)}. Apply these conventions to "
        "the edit you are about to make:\n\n" + "\n\n---\n\n".join(chunks)
    )

    try:
        output = {"hookEventName": "PreToolUse", "additionalContext": additional}
        # Native Codex's explicit allow path drops additionalContext in code
        # mode. Context-only output reaches the model without changing tool
        # permissions; retain the existing Claude output contract.
        if engine(payload) != "codex":
            output["permissionDecision"] = "allow"
        sys.stdout.write(json.dumps({"hookSpecificOutput": output}))
        sys.stdout.flush()
    except Exception:
        pass
    sys.exit(0)


def _parse_paths_frontmatter(rule_path: Path) -> list[str]:
    text = rule_path.read_text()
    if not text.startswith("---"):
        return []
    end = text.find("\n---", 3)
    if end == -1:
        return []
    block = text[3:end].lstrip("\n")
    paths: list[str] = []
    in_paths = False
    for raw in block.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not raw.startswith(" ") and not raw.startswith("\t"):
            if stripped.startswith("paths:"):
                rest = stripped[len("paths:"):].strip()
                if rest.startswith("[") and rest.endswith("]"):
                    paths.extend(_parse_inline_list(rest))
                    in_paths = False
                else:
                    in_paths = True
                continue
            in_paths = False
            continue
        if in_paths and stripped.startswith("- "):
            val = stripped[2:].strip().strip('"').strip("'")
            if val:
                paths.append(val)
    return paths


def _parse_inline_list(s: str) -> list[str]:
    inner = s[1:-1]
    out: list[str] = []
    for part in inner.split(","):
        v = part.strip().strip('"').strip("'")
        if v:
            out.append(v)
    return out


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    rest = text[end + 4:]
    return rest.lstrip("\n")


def _glob_to_regex(glob: str) -> str:
    out = ["^"]
    i = 0
    n = len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            if i + 1 < n and glob[i + 1] == "*":
                if i + 2 < n and glob[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        if c == "?":
            out.append("[^/]")
            i += 1
            continue
        if c in r".+()[]{}|\^$":
            out.append("\\" + c)
            i += 1
            continue
        out.append(c)
        i += 1
    out.append("$")
    return "".join(out)


def _glob_match(glob: str, path: str) -> bool:
    pattern = _glob_to_regex(glob)
    try:
        rx = re.compile(pattern)
    except re.error:
        return False
    if rx.match(path):
        return True
    parts = path.split("/")
    for i in range(1, len(parts)):
        tail = "/".join(parts[i:])
        if rx.match(tail):
            return True
    return False


def _file_size(path: str) -> int:
    if not path:
        return 0
    try:
        return os.path.getsize(path)
    except Exception:
        return 0


def _read_marker(marker: Path) -> int | None:
    try:
        return int(marker.read_text().strip())
    except Exception:
        return None


def _write_marker(marker: Path, offset: int) -> None:
    try:
        marker.write_text(str(offset))
    except Exception:
        pass


def _safe_dir_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:128] or "_"


if __name__ == "__main__":
    main()
