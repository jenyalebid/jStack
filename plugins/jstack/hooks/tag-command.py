#!/usr/bin/env python3
"""UserPromptSubmit — `/tag` runs and answers without spending a turn.

Tagging is bookkeeping, not reasoning: the vocabulary is a list, the session's
membership is a lookup, and every branch is decidable from the two. Routing
that through the model costs a full turn to reach a foregone conclusion — so
this hook does the work in-process and BLOCKS the prompt, which is what keeps
the turn from happening. Claude Code's own `/color` is the shape being matched:
you type it, it takes effect, nothing is asked of the model.

The skill (`skills/tag/SKILL.md`) stays for the case this cannot serve — the
model tagging a session it has been reasoning about, mid-turn, in its own words.

Grammar, one tag per invocation:

    /tag                     what this session is filed under, then the rest
    /tag <name>              carried already → remove it; known → attach it
    /tag <name> <words...>   unknown name → mint it with <words...>, attach it
    /tag -<name>             remove, said outright

Bare `/tag` answers one question — *what is this session filed under* — and
then names the other tags it could be. Nothing else: not the use counts, not
the descriptions. Both are real, and both belong to a different question (which
tag fits work you are describing), which is the model's `/jstack:tag` and
`log_event tag list`. Rendering them here made the answer a table to read
instead of a line.

Toggle is the whole set-vs-unset decision: the state is already on screen from
the last bare `/tag`, so a second verb would only let the two disagree.

Recognized in three forms, because command expansion may or may not have
happened by the time a hook sees the prompt: the raw `/tag`, the plugin's
`/jstack:tag`, and the `JSTACK_TAG_CMD` sentinel that the installed stub
command expands to. Matching all three means this works whichever way the
harness routes it, and the stub degrades to a readable instruction if some
future version stops firing hooks for commands at all.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _answer import block, configure  # noqa: E402 — sibling module, path set above

LOG_EVENT = Path(__file__).resolve().parent.parent / "bin" / "log_event"

# `/tag`, `/jstack:tag`, or the stub's sentinel — then the rest of the line.
TRIGGER = re.compile(r"^\s*(?:/(?:jstack:)?tag|\$jstack:tag|JSTACK_TAG_CMD)(?=\s|$)[ \t]*(.*)$",
                     re.IGNORECASE | re.DOTALL)


def log_event(*args: str) -> tuple[int, str]:
    proc = subprocess.run([str(LOG_EVENT), *args], capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def render(tags: list[dict]) -> str:
    """Two lines: what this session carries, then what else it could.

    The marker column is the only structure — `●` is filed, the indent under
    it is the rest of the vocabulary in the order the CLI hands it over
    (busiest first, so the likely tag is the first word). Names only; see the
    module docstring for why the counts and descriptions are not here.
    """
    if not tags:
        return "no tags yet — /tag <name> <what work belongs under it> mints the first one"
    carried = [t["name"] for t in tags if t["carried"]]
    rest = [t["name"] for t in tags if not t["carried"]]
    head = "● " + "  ".join(carried) if carried else "○ untagged"
    return head + ("\n  " + "  ".join(rest) if rest else "")


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    configure(payload)
    match = TRIGGER.match(payload.get("prompt") or "")
    if not match:
        sys.exit(0)          # not ours — every other prompt passes untouched

    if not os.access(LOG_EVENT, os.X_OK):
        block("/tag: no timeline on this machine (bin/log_event missing)")

    session = (payload.get("session_id") or "").strip()
    if not session:
        block("/tag: no session id in the hook payload — nothing to tag")

    argument = " ".join((match.group(1) or "").split())

    code, out = log_event("tag", "list", "--json", "--session", session)
    if code != 0:
        block(f"/tag: {out}")
    try:
        tags = json.loads(out or "[]")
    except json.JSONDecodeError:
        block(f"/tag: unreadable tag list — {out[:200]}")

    if not argument:
        block(render(tags))

    explicit_unset = argument.startswith("-")
    name, _, description = argument.lstrip("-").partition(" ")
    name = name.strip().lower().lstrip("#")
    description = description.strip()
    known = {t["name"]: t for t in tags}

    if explicit_unset or (name in known and known[name]["carried"]):
        code, out = log_event("tag", "unset", name, "--session", session)
        block(out if code == 0 else f"/tag: {out}")

    if name not in known:
        if not description:
            # The description is the gate that keeps the vocabulary from
            # splitting into synonyms — it is what the next session matches
            # against. Refusing here costs one retype and no turn.
            near = [t["name"] for t in tags if name[:3] and name[:3] in t["name"]][:3]
            hint = f"  close: {', '.join(near)}" if near else ""
            block(f"/tag: no tag '{name}'. To mint it, say what belongs under "
                  f"it:\n  /tag {name} <one line — the subject, not the name>{hint}")
        code, out = log_event("tag", "new", name, "--description", description)
        if code != 0:
            block(f"/tag: {out}")

    code, out = log_event("tag", "set", name, "--session", session)
    block(out if code == 0 else f"/tag: {out}")


if __name__ == "__main__":
    main()
