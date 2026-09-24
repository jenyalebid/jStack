"""SessionStart(source=compact): hand a Codex session back the work it just lost.

Codex compacts SERVER-SIDE. The `compacted` record carries `message: ''` and an opaque
`encrypted_content`, and the window it replaces is rebuilt from `replacement_history` --
the developer prompts, plus the user's own messages verbatim, and nothing else. Every
assistant message and every tool result is dropped. Claude's answer, steering the
summarizer from PreCompact, has nothing to steer here and no wire to steer it through:
PreCompact and PostCompact fire on Codex 0.156 but neither carries `additionalContext`.

The rollout is the way back in. Compaction rebuilds what is SENT to the model; it does not
touch the file, which still holds every turn it dropped. SessionStart re-fires after each
compaction tagged `source: "compact"`, it does carry `additionalContext`, and what it
injects lands as a `developer` message in the REBUILT window. That message does not
outlive the next boundary -- measured on the 2026-09-22 17:58 rollout, 14 developer
messages stood before the first compaction and `replacement_history` kept the three
built-in ones and none injected by a hook -- which is why this runs at EVERY compaction
rather than once. So the residue is read off disk and put back, each time.

Only what replacement_history drops, and only what a session cannot reconstruct: the
commands it ran and its own last words. The user's ask survives on its own and is not
repeated.
"""
import json
import os
import re
import sys

from . import context_ceiling

#: Enough tail to cover the window a compaction just dropped, doubled once when the file's
#: own tool results are large enough to crowd the turns out of it.
TAIL_BYTES = 2 * 1024 * 1024
MAX_SCAN = 8 * 1024 * 1024

#: What fits without crowding out the thing it is trying to protect. This injection is paid
#: on EVERY compaction of a heavy session, alongside the timeline's own, so the budget is a
#: real cost and not a formality.
MAX_COMMANDS = 40
COMMAND_CHARS = 180
MAX_MESSAGES = 3
MESSAGE_CHARS = 700

#: Code-mode writes tool calls as JavaScript, so the command is inside a string literal
#: rather than a JSON field: `text(await tools.exec_command({cmd:"git status"}));`
CODE_MODE_CMD = re.compile(r'\bcmd\s*:\s*("(?:[^"\\]|\\.)*")')

#: Commands worth carrying are the ones that CHANGED something or that cost real time to
#: learn. A session can re-run `ls`; it cannot un-push, and it cannot cheaply rediscover
#: that it already booked a wake. Everything else is noise at 40 lines of budget.
KEEPERS = re.compile(
    r"\b(git|gh|apply_patch|schedule_self|schedule_recurring|msg|ping_boss|pytest|"
    r"launchctl|log_event|jstack|mv|cp|rm|chmod|ln|tee|python3?|pip|npm|cargo|make)\b"
)

#: The tail reader and the dialect sniff are the meter's, deliberately: two copies of
#: "which client wrote this file" is how one of them ends up answering for a shape the
#: other learned about.
tail = context_ceiling.tail


def _unescape(literal):
    """A JS/JSON double-quoted literal as its text, or '' if it will not parse."""
    try:
        return json.loads(literal)
    except Exception:
        return ""


def command_of(payload):
    """The shell command a tool call ran, in either of the two shapes Codex writes.

    Code mode (`custom_tool_call`) carries JavaScript; the classic function-call tool
    carries JSON arguments whose `command` is an argv list whose last element is the script.
    """
    if payload.get("type") == "custom_tool_call":
        hit = CODE_MODE_CMD.search(payload.get("input") or "")
        return _unescape(hit.group(1)) if hit else ""
    if payload.get("type") == "function_call":
        try:
            args = json.loads(payload.get("arguments") or "{}")
        except Exception:
            return ""
        cmd = args.get("command")
        if isinstance(cmd, list) and cmd:
            return str(cmd[-1])
        return str(cmd or "")
    return ""


def assistant_text(payload):
    """An assistant message's text, or '' when the item is not one."""
    if payload.get("type") != "message" or payload.get("role") != "assistant":
        return ""
    out = []
    for part in payload.get("content") or []:
        if isinstance(part, dict) and part.get("text"):
            out.append(part["text"])
    return "".join(out).strip()


def harvest(blob):
    """(commands, messages) from a rollout tail, oldest first, already bounded."""
    commands, messages = [], []
    for line in blob.splitlines():
        if '"response_item"' not in line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if entry.get("type") != "response_item":
            continue
        payload = entry.get("payload") or {}
        cmd = " ".join((command_of(payload) or "").split())
        if cmd:
            # A retry or a poll writes the same line repeatedly; the budget is 40 lines and
            # forty copies of one `git status` carries nothing the first copy did not.
            if not commands or commands[-1] != cmd:
                commands.append(cmd[:COMMAND_CHARS])
            continue
        text = assistant_text(payload)
        if text:
            messages.append(text)
    keepers = [c for c in commands if KEEPERS.search(c)] or commands
    return keepers[-MAX_COMMANDS:], messages[-MAX_MESSAGES:]


HEADER = (
    "CARRIED ACROSS THE COMPACTION. The window you were working in was just rebuilt from "
    "the user's own messages and these instructions -- every assistant message and every "
    "tool result in it was dropped. Below is what was read back off the rollout, because "
    "you cannot reconstruct it: it is not a summary and nothing here is new work.\n\n"
    "Read it as the state you are resuming from. If it shows a file written and no commit "
    "after it, that file is uncommitted in a tree other sessions share. If it shows a wake "
    "booked or a message sent, it is already booked or sent -- do not repeat it."
)


def render(commands, messages):
    """The injection, or '' when the tail held nothing worth handing back."""
    if not commands and not messages:
        return ""
    out = [HEADER]
    if commands:
        out.append("\nCOMMANDS YOU RAN (oldest first, noise dropped):\n" +
                   "\n".join("  " + c for c in commands))
    if messages:
        out.append("\nYOUR LAST WORDS BEFORE THE BOUNDARY:\n" +
                   "\n\n".join("  " + m[:MESSAGE_CHARS] for m in messages))
    return "\n".join(out)


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    # Startup has nothing to carry: nothing has been dropped yet, and injecting a session's
    # own empty history at the top of it would cost the budget and teach nothing.
    if payload.get("source") != "compact":
        return 0

    path = payload.get("transcript_path")
    if not path:
        return 0

    # One manifest serves both clients, so this fires on a Claude compaction too. Claude
    # keeps its own window and re-reads its transcript; there is nothing here to give it,
    # and the doubling scan below would read up to 8MB to find that out. Ask the dialect
    # once instead, off the file.
    if context_ceiling.engine_of(path) != context_ceiling.CODEX:
        return 0

    span = TAIL_BYTES
    while True:
        blob = tail(path, span)
        if blob is None:
            return 0
        commands, messages = harvest(blob)
        if commands or messages or span >= MAX_SCAN:
            break
        if span >= os.path.getsize(path):
            break  # the whole file has been read; doubling reads it again
        span = min(span * 2, MAX_SCAN)

    note = render(commands, messages)
    if not note:
        return 0
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": note,
        }
    }))
    return 0
