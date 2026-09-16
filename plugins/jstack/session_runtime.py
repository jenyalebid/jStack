"""Provider-neutral session facts; native Codex operations never rewrite rollouts."""
from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import time
from pathlib import Path


def session_id() -> str:
    return os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")


def codex_root() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def metadata(path) -> dict:
    try:
        with Path(path).open() as stream:
            row = json.loads(stream.readline())
        if row.get("type") == "session_meta":
            return row.get("payload") or {}
    except (OSError, ValueError, TypeError):
        pass
    return {}


def session_title(path, sid: str = "") -> str:
    """The provider's current title for ``path``, or ``""`` when unnamed.

    Claude writes generated and user-renamed titles into the transcript. A
    custom title is the current one whenever present, matching its own session
    picker. Codex keeps titles in ``session_index.jsonl`` instead of the
    rollout, so its native thread id joins the two files. Read the whole index:
    renames append another row and the last matching row wins.

    This deliberately does not derive a title from a prompt. Callers may use a
    prompt as scope, but scope is not the session's name.
    """
    transcript = Path(path)
    meta = metadata(transcript)
    if meta:
        native_sid = str(meta.get("id") or meta.get("session_id") or sid or "")
        title = ""
        try:
            with (codex_root() / "session_index.jsonl").open() as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if row.get("id") != native_sid:
                        continue
                    candidate = row.get("thread_name")
                    if isinstance(candidate, str):
                        title = " ".join(candidate.split())
        except OSError:
            pass
        return title

    generated = ""
    custom = ""
    try:
        with transcript.open() as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if row.get("type") == "ai-title":
                    candidate = row.get("aiTitle")
                    if isinstance(candidate, str):
                        generated = " ".join(candidate.split())
                elif row.get("type") == "custom-title":
                    candidate = row.get("customTitle")
                    if isinstance(candidate, str):
                        custom = " ".join(candidate.split())
    except OSError:
        pass
    return custom or generated


def engine(payload=None) -> str:
    payload = payload or {}
    path = payload.get("transcript_path")
    if path:
        if Path(path).is_file():
            return "codex" if metadata(path) else "claude"
        if Path(path).name.startswith("rollout-"):
            return "codex"
    return "codex" if os.environ.get("CODEX_THREAD_ID") else "claude"


def transcripts(sid: str) -> list[Path]:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", sid):
        return []
    found = list((Path.home() / ".claude/projects").glob(f"*/{sid}.jsonl"))
    for folder in ("sessions", "archived_sessions"):
        found.extend((codex_root() / folder).glob(f"**/*-{sid}.jsonl"))
    return sorted(set(found))


def patch_paths(patch: str, cwd: str) -> list[str]:
    if not isinstance(patch, str):
        return []
    paths = []
    for line in patch.splitlines():
        match = re.match(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", line)
        if match:
            path = Path(match[1]).expanduser()
            paths.append(str((Path(cwd) / path).resolve()))
    return list(dict.fromkeys(paths))


def user_text(row: dict) -> str:
    if row.get("type") == "response_item":
        message = row.get("payload") or {}
        if message.get("type") != "message" or message.get("role") != "user":
            return ""
    elif row.get("type") == "user" and not row.get("isMeta"):
        message = row.get("message") or {}
    else:
        return ""
    content = message.get("content") or []
    text = content if isinstance(content, str) else "\n".join(
        b.get("text", "") for b in content if isinstance(b, dict)
        and b.get("type") in ("text", "input_text"))
    if row.get("type") == "response_item" and text.lstrip().startswith((
            "<environment_context>", "# AGENTS.md instructions for ", "<permissions instructions>")):
        return ""
    return text


def engagement_marker(sid: str, state_dir=None) -> Path:
    root = state_dir or os.environ.get("JSTACK_REVIEW_STATE") or Path.home() / ".claude/jstack/review-state"
    return Path(root).expanduser() / "user-engaged" / re.sub(r"[^A-Za-z0-9_-]", "_", sid)


def codex_user_engaged(path, state_dir=None) -> bool:
    meta = metadata(path)
    return bool(meta and (meta.get("source") == "cli"
                          or engagement_marker(meta.get("id", ""), state_dir).is_file()))


class CodexRPC:
    """Short-lived native app server, for offline fork/read operations only.

    Never resume or compact a thread another CLI owns: it would create two
    writers. Close the child even when a request fails or times out.
    """
    def __init__(self, binary="codex", env=None):
        self.binary = binary
        self.env = env

    def __enter__(self):
        env = dict(self.env if self.env is not None else os.environ, SKIP_SESSION_HOOK="1")
        env.pop("CODEX_THREAD_ID", None)
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        self.proc = subprocess.Popen([self.binary, "app-server", "--stdio"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, env=env)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.proc.stdout, selectors.EVENT_READ)
        self.buffer = b""
        self.serial = 0
        try:
            self.call("initialize", {"clientInfo": {"name": "jstack", "version": "1"},
                                      "capabilities": {"experimentalApi": True}})
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def call(self, method, params, timeout=20):
        self.serial += 1
        request = {"id": self.serial, "method": method, "params": params}
        self.proc.stdin.write((json.dumps(request) + "\n").encode())
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                row = json.loads(line)
                if row.get("id") != self.serial:
                    continue
                if "error" in row:
                    raise RuntimeError(row["error"].get("message", str(row["error"])))
                return row["result"]
            if self.selector.select(max(0, deadline - time.monotonic())):
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("Codex app server exited")
                self.buffer += chunk
        raise TimeoutError(f"Codex {method} timed out")

    def __exit__(self, *_):
        self.selector.close()
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.proc.stdin.close()
        self.proc.stdout.close()


def command_main():
    """Skill fallback: invoke the same deterministic hook with native facts."""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("splitoff", "takeover", "print", "tag", "pict"))
    parser.add_argument("--session", default=session_id())
    parser.add_argument("words", nargs="*")
    args = parser.parse_args()
    paths = transcripts(args.session)
    if len(paths) != 1:
        parser.error(f"expected one transcript for {args.session!r}; found {len(paths)}")
    payload = {"session_id": args.session, "transcript_path": str(paths[0]),
               "cwd": os.getcwd(), "prompt": "/" + args.command + " " + " ".join(args.words)}
    result = subprocess.run([str(Path(__file__).parent / "hooks" / (args.command + "-command.py"))],
                            input=json.dumps(payload), capture_output=True, text=True)
    try:
        answer = json.loads(result.stdout)
        print(answer.get("reason") or answer.get("stopReason") or answer.get("systemMessage") or result.stdout)
    except (ValueError, KeyError):
        print(result.stderr or result.stdout)
    return 0 if result.returncode in (0, 2) else result.returncode


if __name__ == "__main__":
    raise SystemExit(command_main())
