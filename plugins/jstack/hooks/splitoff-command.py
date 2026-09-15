#!/usr/bin/env python3
"""UserPromptSubmit — `/splitoff` forks the session without spending a turn.

A verbatim fork is two fixed calls — dub the transcript, open a window on the
copy — and neither is a judgement. Worse than merely costing a turn, doing it
through the model made the model part of what gets copied: the turn that runs
the fork lands in the source transcript, so the branch you asked for carries a
paragraph about being branched. Blocking the prompt cuts at the last completed
turn instead, which is the cut anyone means by "fork from here".

Grammar:

    /splitoff                the copy is titled "<original> - copy"
    /splitoff <words...>     ...titled "<original> - <words>" instead

There is still no focus argument — splitoff never narrows, it dubs the whole
transcript. Words are read as the copy's NAME, which is the one thing a fork
genuinely needs once you have made two of them and the resume picker shows both.
`/jstack:handoff <focus>` remains the scoped restart.

The source session is untouched and keeps running. The copy gets its own file
and its own id: resuming the same id in two windows would interleave appends
into one transcript and corrupt it, which is the whole reason the dub copies
before it opens anything.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from session_runtime import engine, metadata, CodexRPC
from _answer import block  # noqa: E402 — sibling module, path set above

#: Overridable so a test can stand fakes in their place — the alternative,
#: swapping the real adapters aside, hands every other session on this shared
#: machine the fake for as long as the test runs.
DUB = Path(os.environ.get("JSTACK_DUB_BIN")
           or Path(__file__).resolve().parent.parent / "bin" / "dub-session")
TERMINAL = (os.environ.get("JSTACK_TERMINAL_BIN") or shutil.which("open-terminal-here")
            or str(Path(__file__).resolve().parent.parent / "bin/open-terminal-here"))

#: The dub is a line-by-line rewrite of one jsonl and the open is an osascript
#: round-trip. Both are seconds; the ceiling is here so a hang reads as a hang.
TIMEOUT = 25

# `/splitoff`, `/jstack:splitoff`, or the stub's sentinel — then the rest.
TRIGGER = re.compile(r"^\s*(?:/(?:jstack:)?splitoff|JSTACK_SPLITOFF_CMD)\b[ \t]*(.*)$",
                     re.IGNORECASE | re.DOTALL)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    match = TRIGGER.match(payload.get("prompt") or "")
    if not match:
        sys.exit(0)          # not ours — every other prompt passes untouched

    if not os.access(DUB, os.X_OK):
        block("/splitoff: no dubber on this machine (bin/dub-session missing)")

    # The transcript path from the payload is the file the harness is writing
    # to. Reconstructing it from the id and the cwd is a guess about path
    # encoding; a fork built on a guess forks the wrong conversation.
    transcript = (payload.get("transcript_path") or "").strip()
    if not transcript:
        block("/splitoff: no transcript path in the hook payload — nothing to fork")
    if not Path(transcript).is_file():
        block(f"/splitoff: no transcript on disk yet — {transcript}")

    words = " ".join((match.group(1) or "").split())
    suffix = f" - {words}" if words else " - copy"

    provider = engine(payload)
    if provider == "codex":
        try:
            with CodexRPC() as rpc:
                fork = rpc.call("thread/fork", {"threadId": metadata(transcript)["id"],
                                               "excludeTurns": True, "deferGoalContinuation": True})
            new_id = fork["thread"]["id"]
        except (OSError, RuntimeError, TimeoutError, KeyError) as exc:
            block(f"/splitoff: native Codex fork failed: {exc}")
        cmd = [TERMINAL, str(payload.get("cwd") or Path.cwd()), "--engine", "codex",
               "--name", words or "copy", "--resume", new_id]
        if payload.get("model"):
            cmd += ["--model", payload["model"]]
        try:
            opened = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
            if opened.returncode:
                block(f"/splitoff: fork {new_id} created; window failed: {opened.stderr.strip()}\n"
                      f"Resume with: codex resume {new_id}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            block(f"/splitoff: fork {new_id} created; window failed: {exc}\n"
                  f"Resume with: codex resume {new_id}")
        block(f"split → {words or 'copy'} · {new_id}\n  native Codex fork; source session unchanged")

    try:
        dub = subprocess.run([str(DUB), transcript, "", suffix],
                             capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        block(f"/splitoff: the dub did not finish in {TIMEOUT}s")
    if dub.returncode != 0:
        block(f"/splitoff: {(dub.stderr or '').strip() or 'dub-session failed'}")

    new_id = (dub.stdout or "").strip().splitlines()[-1] if dub.stdout.strip() else ""
    if not new_id:
        block("/splitoff: the dubber printed no id — nothing to resume")

    cwd = str(payload.get("cwd") or Path.cwd())
    title = f"copy · {words}" if words else "copy"

    # `--resume` loads the copied history. Never `--session-id`: it forces a
    # fresh empty session and errors outright when the file already exists.
    opened = False
    if shutil.which(TERMINAL):
        try:
            opened = subprocess.run([TERMINAL, cwd, "--resume", new_id],
                                    capture_output=True, text=True,
                                    timeout=TIMEOUT).returncode == 0
        except subprocess.TimeoutExpired:
            opened = False

    # The dub already happened either way — a window that would not open is a
    # missing convenience, not a lost fork, so the id goes out regardless.
    if opened:
        block(f"split → {title} · new window on {cwd}\n"
              f"  {new_id} — verbatim to this point; this session is unchanged")
    block(f"split → {title} · {new_id}\n"
          f"  no terminal opened — resume it yourself: cd {cwd} && claude --resume {new_id}")


if __name__ == "__main__":
    main()
