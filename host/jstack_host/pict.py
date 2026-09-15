"""Render what a session opens with, as a document the app can read back.

`pict` answers one question — everything a session spawned from a directory is
injected with, in the order the model receives it. On the desk that answer is a
command in the terminal the session is already in. From the phone there is no
terminal, so the thread's ⋯ menu asks the host for the same render and opens
the file it names.

Bare, because this is the reading copy: the injection itself, in wire order,
with no weight table, no mechanism notes and no on-demand layer. Rules that
fire when a session touches a file are a different question from what a session
opens with, and a document that mixes the two answers neither.

The render lands in the seat's pad and nowhere else. The pad is inside the
app's read fence (`~/Agents/**`), so a document the Mac just wrote is one the
phone can ask back through `/context/file`; a temp file would open a window
onto a refusal. The name is stable per directory, so asking twice refreshes
the document already on screen instead of stacking a second beside it.
"""

import os
import subprocess
import tempfile
from pathlib import Path

from . import plugin_paths

# The jStack renderer — the one implementation of "what is this directory's
# injection". Resolved at import like the dub adapter, so a test pointing HOME
# elsewhere still finds the binary while the render reads the test tree, and
# resolved by `jstack_bin` rather than spelled out, because the plugin lives
# under `plugins/cache/` on any machine that installs it from its marketplace.
PICT = plugin_paths.jstack_bin("pict")

# It walks a whole walk-up chain and every hook that claims to inject. Slow on
# a deep tree, but bounded — a render that hasn't finished by now is wedged,
# and the caller is a person holding a phone waiting for a window.
TIMEOUT = 120


def render(cwd: str, pad: Path, full: bool = False, engine: str = "claude") -> tuple[Path, str]:
    """Write the pict of `cwd` into `pad`. Returns (path, title).

    `full` swaps the reading copy for the annotated view — weights, mechanism
    per file, the on-demand pool.

    Raises FileNotFoundError when this host has no pict, and RuntimeError with
    the renderer's own complaint when the render fails. Both before the file is
    replaced: a failed render must not leave half a document behind for the
    viewer to open as though it were the answer.
    """
    if not os.access(PICT, os.X_OK):
        raise FileNotFoundError(str(PICT))

    name = Path(cwd).name or "root"
    out = pad / f"pict-{name}.md"
    pad.mkdir(parents=True, exist_ok=True)

    if engine not in ("claude", "codex"):
        raise ValueError(f"unknown engine: {engine}")
    cmd = [str(PICT), cwd] + (["--engine", "codex"] if engine == "codex" else [])
    cmd += [] if full else ["--bare"]
    fd, tmp = tempfile.mkstemp(dir=str(pad), prefix=".pict-", suffix=".md")
    try:
        with os.fdopen(fd, "w") as sink:
            r = subprocess.run(cmd, stdout=sink, stderr=subprocess.PIPE,
                               text=True, timeout=TIMEOUT)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or "").strip()
                               or f"pict failed on {cwd}")
        os.replace(tmp, out)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise

    return out, f"{name} · pict"
