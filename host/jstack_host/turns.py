"""Streaming turn runner — the one genuinely new primitive.

Sends a dictated/typed message to a Claude Code session and streams the reply
back token-by-token over SSE. Built on the proven `claude --print --resume`
pattern (assistant/claude_session.py), upgraded to `--output-format stream-json
--include-partial-messages` for realtime deltas.

Event schema (verified live):
  {"type":"stream_event","event":{"type":"content_block_delta",
     "delta":{"type":"text_delta","text":"..."}}}   → a reply chunk
  {"type":"result","subtype":"success","result":"<full text>",
     "usage":{"output_tokens":N,...},"is_error":false}  → final

Safety:
  - One turn per session (a second concurrent turn → 409 via TurnBusy).
  - Refuse a turn into a session that already has a live process — two
    processes appending the same JSONL would corrupt it.
"""

import asyncio
import json
import os
from pathlib import Path

from .transcripts import _find_session_cwd
from .board import _live_session_ids
from .messages import tool_summary
from .hostenv import default_model, spawn_path, workspace

# asyncio StreamReader default line limit is 64 KiB; SessionStart hook injection
# lines blow past that. Give the reader plenty of headroom.
_STREAM_LIMIT = 16 * 1024 * 1024
_TURN_TIMEOUT = 1800  # seconds — work turns run long; the ceiling is a hang guard, not a leash

_in_flight: set[str] = set()
_turn_procs: dict = {}   # sid -> asyncio subprocess, while a turn runs


def interrupt_turn(session_id: str) -> bool:
    """Kill the in-flight headless turn for this session, if any."""
    proc = _turn_procs.get(session_id)
    if proc is None or proc.returncode is not None:
        return False
    try:
        proc.kill()
        return True
    except ProcessLookupError:
        return False


class TurnError(Exception):
    """Turn cannot start. `status` is the HTTP code to surface."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _build_cmd(session_id: str, text: str, resume: bool) -> list[str]:
    cmd = [
        "claude", "--print",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--permission-mode", "bypassPermissions",
    ]
    # Resume inherits the session's stored model; fresh sessions pin the auto-run
    # model rather than inherit the cwd's saved /model default (the Fable leak).
    if resume:
        cmd += ["--resume", session_id]
    else:
        cmd += ["--model", default_model(), "--session-id", session_id]
    cmd += ["-p", text]
    return cmd


def _env() -> dict:
    env = os.environ.copy()
    env.pop("CODEX_THREAD_ID", None)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env["PATH"] = spawn_path(inherit=env.get("PATH", ""))
    # Don't spawn a post-session review for each jRemote turn.
    env["SKIP_SESSION_HOOK"] = "1"
    # Timeline injection needs no opt-in here — the jStack SessionStart hook
    # injects it regardless of SKIP_SESSION_HOOK (it fires on resume too).
    return env


def _native_resume(session_id: str, text: str) -> tuple[str, list[str]] | None:
    from . import codex_transcript, managed
    from .messages import _find_session_file
    path = _find_session_file(session_id)
    meta = codex_transcript.metadata(path) if path else {}
    native_id = meta.get("id") or meta.get("session_id")
    if not native_id:
        return None
    if native_id in _live_session_ids() or any(
        row.get("transcript") == str(path) and managed.is_open(sid)
        for sid, row in managed.open_registry().items()
    ):
        raise TurnError("session is busy (running elsewhere)", 409)
    cmd = ["codex", "exec", "resume", "--json",
           "--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"]
    model = codex_transcript.summary(path).get("model")
    if model:
        cmd += ["--model", model]
    return native_id, cmd + [native_id, text]


async def stream_turn(session_id: str, text: str, *, resume: bool = True,
                      agent_id: str | None = None):
    """Async generator yielding (event_name, payload_dict) SSE events.

    resume=True  → continue an existing session (cwd resolved from its JSONL).
    resume=False → start a fresh session for `agent_id` in its workspace.

    Raises TurnError before any event is yielded if the turn can't start.
    """
    text = (text or "").strip()
    if not text:
        raise TurnError("empty message", 400)

    native = _native_resume(session_id, text) if resume else None
    lock_id = native[0] if native else session_id

    if resume:
        cwd = _find_session_cwd(session_id)
        if not cwd:
            raise TurnError("session not found", 404)
    else:
        try:
            cwd = str(workspace(agent_id))
        except (KeyError, TypeError):
            raise TurnError(f"unknown agent {agent_id!r}", 404)
        if not Path(cwd).exists():
            raise TurnError(f"agent workspace missing for {agent_id!r}", 404)

    if session_id in _live_session_ids():
        raise TurnError("session is busy (running elsewhere)", 409)
    if lock_id in _in_flight:
        raise TurnError("still working on the previous message — watch the reply or ESC it first", 409)

    _in_flight.add(lock_id)
    # Tell the phone the session id up front (esp. for a brand-new Direct
    # session) so it can persist openOnPhone immediately and re-attach after a
    # quit mid-turn — not only when the `done` event finally arrives.
    yield ("session", {"session_id": session_id})
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *(native[1] if native else _build_cmd(session_id, text, resume)),
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_env(),
            limit=_STREAM_LIMIT,
        )
        _turn_procs[session_id] = proc

        loop = asyncio.get_event_loop()
        deadline = loop.time() + _TURN_TIMEOUT
        saw_result = False
        native_text = []
        tool_blocks: dict = {}  # index → {name, input(json str)} for in-flight tool calls

        while True:
            if loop.time() > deadline:
                proc.kill()
                yield ("error", {"message": "turn timed out"})
                return
            try:
                # Short read window: a silent tool run (build, long think)
                # streams nothing for minutes. Emit a keepalive every 15s so
                # bytes keep flowing and the phone's idle timer never fires —
                # the overall deadline is the only real ceiling.
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=15)
            except asyncio.TimeoutError:
                yield ("ping", {})
                continue
            if not line:
                break

            try:
                o = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            t = o.get("type")
            if native:
                if t == "item.completed":
                    item = o.get("item") or {}
                    if item.get("type") == "agent_message":
                        chunk = item.get("text", "")
                        native_text.append(chunk)
                        yield ("delta", {"text": chunk})
                    elif item.get("type") in ("command_execution", "mcp_tool_call", "file_change"):
                        yield ("tool", {"name": item["type"],
                                        "summary": item.get("command") or item.get("tool") or "file changes"})
                elif t == "turn.completed":
                    saw_result = True
                    usage = o.get("usage") or {}
                    yield ("done", {"session_id": session_id, "result": "\n".join(native_text),
                                    "tokens": usage.get("output_tokens", 0),
                                    "input_tokens": usage.get("input_tokens", 0), "is_error": False})
                elif t in ("error", "turn.failed"):
                    saw_result = True
                    error = o.get("error") or {}
                    yield ("error", {"message": error.get("message") or o.get("message") or "Codex turn failed"})
                continue
            if t == "stream_event":
                ev = o.get("event", {})
                et = ev.get("type")
                if et == "content_block_start":
                    cb = ev.get("content_block", {})
                    if cb.get("type") in ("tool_use", "server_tool_use"):
                        tool_blocks[ev.get("index")] = {"name": cb.get("name", "?"), "input": ""}
                elif et == "content_block_delta":
                    d = ev.get("delta", {})
                    dt = d.get("type")
                    if dt == "text_delta":
                        chunk = d.get("text", "")
                        if chunk:
                            yield ("delta", {"text": chunk})
                    elif dt == "input_json_delta":
                        blk = tool_blocks.get(ev.get("index"))
                        if blk is not None:
                            blk["input"] += d.get("partial_json", "")
                elif et == "content_block_stop":
                    blk = tool_blocks.pop(ev.get("index"), None)
                    if blk is not None:
                        try:
                            inp = json.loads(blk["input"]) if blk["input"].strip() else {}
                        except (json.JSONDecodeError, ValueError):
                            inp = {}
                        yield ("tool", {"name": blk["name"],
                                        "summary": tool_summary(blk["name"], inp)})
            elif t == "result":
                saw_result = True
                usage = o.get("usage", {}) or {}
                yield ("done", {
                    "session_id": session_id,
                    "result": o.get("result", ""),
                    "tokens": usage.get("output_tokens", 0),
                    "input_tokens": usage.get("input_tokens", 0),
                    "is_error": bool(o.get("is_error")),
                })

        await proc.wait()
        if not saw_result:
            stderr = (await proc.stderr.read()).decode(errors="replace")[:500]
            yield ("error", {"message": f"turn ended without result (exit={proc.returncode})",
                             "detail": stderr})
    finally:
        _turn_procs.pop(session_id, None)
        if proc and proc.returncode is None:
            # The stream died (app quit, connection drop) but the turn is
            # alive. The work MUST survive — it writes to the transcript and
            # the phone re-attaches via live-tail on relaunch. Hold the turn
            # lock until the process actually finishes, then release it.
            async def _reap(p=proc, sid=lock_id):
                try:
                    await p.wait()
                finally:
                    _in_flight.discard(sid)
            asyncio.get_event_loop().create_task(_reap())
        else:
            _in_flight.discard(lock_id)
