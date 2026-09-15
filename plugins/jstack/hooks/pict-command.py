#!/usr/bin/env python3
"""UserPromptSubmit — `/pict` renders and opens without spending a turn.

Rendering a directory's injected context is three fixed steps — run the tool,
put the file where a viewer can read it, open it — and not one of them is a
judgement. Routing it through the model bought a turn spent narrating what the
document on screen already says, and worse: a session asked to describe its own
context will describe what it believes is there, which is the guess the tool
exists to replace. So this hook does the work in-process and BLOCKS the prompt,
the way `/tag` does.

Grammar:

    /pict                    the directory this session is running in
    /pict <dir>              that directory instead
    /pict --full             the annotated view — weights, mechanism, on-demand
    /pict <dir> <flags...>   anything else goes to `pict` untouched

Bare is the default because this is the READING copy: the injection itself, in
wire order, with no weight table and no on-demand layer. Rules that fire when a
session touches a file are a different question from what a session opens with,
and a document that mixes them answers neither. `--full` is this hook's flag,
not the renderer's — it drops `--bare` rather than adding anything.

Where the file lands, in order: a `pad/` beside the session, when the workspace
has one — a host that fences what its viewer may read draws that fence around
the workspace, so a render outside it opens onto a refusal — otherwise a
jStack-owned directory under the Claude config. The name is stable per rendered
directory, so asking twice refreshes the document already on screen instead of
stacking a second beside it.

Opening is delegated, never reimplemented: `show-doc` when the host has one
(it knows which screen is driving this session — a phone, a tablet, the desk),
`open-artifact` otherwise. Neither present, or both refusing, still leaves the
document written and its path in the answer: the render is the product, the
window is the convenience.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _answer import block, configure  # noqa: E402 — sibling module, path set above
from session_runtime import engine

#: The renderer, a sibling of this hook. Overridable so a test can stand a fake
#: in its place — the alternative, swapping the real binary aside, is a window
#: in which every other session on the machine gets the fake.
PICT = Path(os.environ.get("JSTACK_PICT_BIN")
            or Path(__file__).resolve().parent.parent / "bin" / "pict")

#: Falls here when the session's workspace has no pad. Under the Claude config
#: rather than a temp dir, because a document a viewer may be asked to fetch
#: has to still exist when it is asked for.
FALLBACK = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) \
    / "jstack" / "pict"

#: It walks a whole walk-up chain and every hook that claims to inject. The
#: harness kills the hook at its own timeout anyway; this one exists so the
#: failure reads as "the render hung" rather than as a silent pass-through.
TIMEOUT = 110

# `/pict`, `/jstack:pict`, or the stub's sentinel — then the rest of the line.
TRIGGER = re.compile(r"^\s*(?:/(?:jstack:)?pict|\$jstack:pict|JSTACK_PICT_CMD)(?=\s|$)[ \t]*(.*)$",
                     re.IGNORECASE | re.DOTALL)


def target(words: list[str], cwd: Path) -> tuple[Path, list[str]]:
    """Split the argument line into the directory to render and pict's flags.

    A leading word that names a directory is the target; everything else is the
    renderer's business. A leading word that was MEANT as a directory and does
    not exist reaches `pict` as a flag and is refused there, by name — better
    than silently rendering the cwd and handing back a document about the
    wrong place.
    """
    if words and not words[0].startswith("-"):
        first = Path(words[0]).expanduser()
        first = first if first.is_absolute() else cwd / first
        if first.is_dir():
            return first.resolve(), words[1:]
    return cwd, words


def out_dir(cwd: Path) -> Path:
    """Where the render goes — the session's pad, or jStack's own directory."""
    pad = cwd / "pad"
    return pad if pad.is_dir() else FALLBACK


def open_it(path: Path, title: str) -> str:
    """Put the document on screen. Returns a phrase for the answer, or ''.

    A host's own document router wins: it routes to the screen driving this
    session, which the OS's default-open cannot know. Neither adapter's failure
    is fatal — the caller reports the path either way.
    """
    if shutil.which("show-doc"):
        r = subprocess.run(["show-doc", str(path), "--title", title],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return (r.stdout or "").strip().split("—")[0].strip() or "opened"
    if shutil.which("open-artifact"):
        if subprocess.run(["open-artifact", str(path)],
                          capture_output=True).returncode == 0:
            return "opened"
    return ""


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    configure(payload)
    match = TRIGGER.match(payload.get("prompt") or "")
    if not match:
        sys.exit(0)          # not ours — every other prompt passes untouched

    if not os.access(PICT, os.X_OK):
        block("/pict: no renderer on this machine (bin/pict missing)")

    cwd = Path(payload.get("cwd") or Path.cwd())
    words = (match.group(1) or "").split()
    directory, rest = target(words, cwd)

    # `--full` is ours: it drops the bare default rather than adding a flag.
    view = [] if "--full" in rest else ["--bare"]
    if engine(payload) == "codex":
        view = ["--engine", "codex", *view]
    flags = [w for w in rest if w not in ("--full", "--bare")]

    where = out_dir(cwd)
    try:
        where.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        block(f"/pict: nowhere to write the render — {e}")

    name = directory.name or "root"
    out = where / f"pict-{name}.md"

    # Rendered to a temp first: a failed render must not leave half a document
    # behind for the viewer to open as though it were the answer, and must not
    # replace a good one already on screen.
    fd, tmp = tempfile.mkstemp(dir=str(where), prefix=".pict-", suffix=".md")
    try:
        with os.fdopen(fd, "w") as sink:
            r = subprocess.run([str(PICT), str(directory), *view, *flags],
                               stdout=sink, stderr=subprocess.PIPE,
                               text=True, timeout=TIMEOUT)
        if r.returncode != 0:
            block(f"/pict: {(r.stderr or '').strip() or f'failed on {directory}'}")
        os.replace(tmp, out)
    except subprocess.TimeoutExpired:
        block(f"/pict: the render did not finish in {TIMEOUT}s — {directory}")
    finally:
        Path(tmp).unlink(missing_ok=True)

    title = f"{name} · pict"
    opened = open_it(out, title)
    block(f"{title} — {opened + ' · ' if opened else ''}{out}")


if __name__ == "__main__":
    main()
