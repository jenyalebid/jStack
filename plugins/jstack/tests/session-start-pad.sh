#!/usr/bin/env bash
# jStack live test — hooks/session-start-pad.py (the seat's one shared folder).
#
# The harness hands each session a private directory in the system temp dir and
# hardcodes that path into its own prompt. A session told to put its output there
# has put it where the user cannot reach it, and no instruction in a doc outranks
# a path in a system prompt. So the hook replaces the directory with a symlink to
# the seat's pad before anything writes to it.
#
# Runs the real hook with real SessionStart JSON and reads the filesystem back.
# What it pins:
#   - the harness path becomes a symlink to <seat>/pad, and the pad is created.
#   - THE DEFAULT PATH IS DERIVED THE WAY THE HARNESS DERIVES IT. A wrong slug
#     links a directory no session will ever be told about, and nothing reports it.
#   - a session that already wrote into the private directory has its output MOVED
#     into the pad — the race with the first write loses nothing.
#   - which seat a directory belongs to is the tree's answer, not this hook's:
#     deepest seat wins, and a checkout parked in a pad belongs to the seat above.
#   - anything not a seat, and anything already pointed somewhere, is left alone.
#
# Exit 0 = all pass, exit 1 = any fail.

set -u

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$PLUGIN_ROOT/hooks/session-start-pad.py"

[[ -x "$HOOK" ]] || { echo "FAIL: $HOOK not executable" >&2; exit 1; }

TMP=$(mktemp -d /tmp/jstack-pad-test.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

python3 - "$HOOK" "$TMP" <<'PY'
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HOOK, TMP = sys.argv[1], Path(sys.argv[2])

fails = []
def check(name, cond):
    print(("ok" if cond else "FAIL") + f": {name}")
    if not cond:
        fails.append(name)

agents = TMP / "Agents"
for rel in ("Alpha", "Alpha/social", "Alpha/social/threads", "Gamma"):
    (agents / rel).mkdir(parents=True, exist_ok=True)
    (agents / rel / "CLAUDE.md").write_text(f"# {rel}\n")

BASE = os.environ.copy()
BASE.pop("JSTACK_SCRATCHPAD", None)
# The hook falls back to the env var when the payload carries no id, so the
# session running this test would otherwise lend it one.
BASE.pop("CLAUDE_CODE_SESSION_ID", None)
BASE["JSTACK_AGENTS_DIR"] = str(agents)
BASE["JSTACK_PAD_LOG"] = str(TMP / "pad-link.log")

def run(cwd, sid="sid-abcdef1234", scratchpad=None, payload=None):
    env = BASE.copy()
    if scratchpad is not None:
        env["JSTACK_SCRATCHPAD"] = str(scratchpad)
    body = payload if payload is not None else json.dumps(
        {"session_id": sid, "cwd": str(cwd), "hook_event_name": "SessionStart",
         "source": "startup"})
    r = subprocess.run([HOOK], input=body, env=env, cwd=str(cwd),
                       capture_output=True, text=True, timeout=25)
    return r.returncode, r.stdout, r.stderr

# --- the plain case ---------------------------------------------------------
seat = agents / "Alpha/social"
sp = TMP / "harness/one/scratchpad"
code, out, err = run(seat, scratchpad=sp)
check("exits 0", code == 0)
check("the harness path is now a symlink", sp.is_symlink())
check("it points at the seat's pad", sp.resolve() == (seat / "pad").resolve())
check("the pad was created", (seat / "pad").is_dir())
log = (TMP / "pad-link.log").read_text() if (TMP / "pad-link.log").exists() else ""
check("the run left its line in the pad log", "sid-abcdef1234" in log and "linked" in log)
check("the hook prints nothing on stdout", out == "")

# --- the default path is the harness's, derived the same way ---------------
# A slug that disagrees with the harness's links a directory no session is ever
# told about, so this is checked against the real location and then cleaned up.
seat2 = agents / "Gamma"
sid = "sid-default-99"
slug = re.sub(r"[/.]", "-", str(seat2.resolve()))
real = Path(f"/private/tmp/claude-{os.getuid()}") / slug / sid / "scratchpad"
try:
    code, _, _ = run(seat2, sid=sid)
    check("with no override it links the harness's own derived path",
          real.is_symlink() and real.resolve() == (seat2 / "pad").resolve())
finally:
    if real.is_symlink():
        real.unlink()
    shutil.rmtree(real.parent.parent, ignore_errors=True)

# --- output already written into the private directory is rescued ----------
seat3 = agents / "Alpha"
sp = TMP / "harness/two/scratchpad"
sp.mkdir(parents=True)
(sp / "notes.md").write_text("half a report")
(sp / "sub").mkdir()
(sp / "sub/deep.txt").write_text("deeper")
code, _, _ = run(seat3, scratchpad=sp)
pad = seat3 / "pad"
check("a directory the session already wrote to becomes a link", sp.is_symlink())
check("its file was moved into the pad",
      (pad / "notes.md").read_text() == "half a report")
check("a subdirectory was moved too", (pad / "sub/deep.txt").read_text() == "deeper")

# A name the pad already holds is kept, and the rescued copy is marked with the
# session that wrote it — a rescue that overwrote the user's file would be worse
# than the private directory it came from.
sp = TMP / "harness/three/scratchpad"
sp.mkdir(parents=True)
(sp / "notes.md").write_text("a second session's version")
code, _, _ = run(seat3, sid="sid-99887766aa", scratchpad=sp)
check("the pad's own copy is not overwritten",
      (pad / "notes.md").read_text() == "half a report")
check("the rescued copy is marked with the session that wrote it",
      (pad / "notes-sid-9988.md").read_text() == "a second session's version")

# --- idempotent, and it never steals a link that is somebody else's -------
sp = TMP / "harness/four/scratchpad"
code, _, _ = run(seat, scratchpad=sp)
before = os.readlink(sp)
code, _, _ = run(seat, scratchpad=sp)
check("running twice leaves the same link", os.readlink(sp) == before)

elsewhere = TMP / "elsewhere"
elsewhere.mkdir()
sp = TMP / "harness/five/scratchpad"
sp.parent.mkdir(parents=True)
sp.symlink_to(elsewhere)
code, _, _ = run(seat, scratchpad=sp)
check("a link pointing somewhere else is left alone", sp.resolve() == elsewhere.resolve())

sp = TMP / "harness/six/scratchpad"
sp.parent.mkdir(parents=True)
sp.write_text("not a directory")
code, _, _ = run(seat, scratchpad=sp)
check("a plain file in the way is left alone",
      sp.is_file() and sp.read_text() == "not a directory")

# --- which seat, answered by the tree ------------------------------------
sp = TMP / "harness/seven/scratchpad"
code, _, _ = run(agents / "Alpha/social/threads", scratchpad=sp)
check("the deepest seat wins",
      sp.resolve() == (agents / "Alpha/social/threads/pad").resolve())

# A throwaway checkout parked in a pad carries its own CLAUDE.md. The seat a
# session standing in one belongs to is the seat above the pad, not the checkout.
parked = seat / "pad/some-checkout"
parked.mkdir(parents=True, exist_ok=True)
(parked / "CLAUDE.md").write_text("# a checkout\n")
sp = TMP / "harness/eight/scratchpad"
code, _, _ = run(parked, scratchpad=sp)
check("a checkout parked in a pad belongs to the seat above it",
      sp.resolve() == (seat / "pad").resolve())

# --- a pad is a fixture of a place agents work from ----------------------
outside = TMP / "somewhere-else"
outside.mkdir()
sp = TMP / "harness/nine/scratchpad"
code, _, _ = run(outside, scratchpad=sp)
check("outside the agents tree the harness default is left alone",
      code == 0 and not sp.exists())
check("no pad is invented outside the tree", not (outside / "pad").exists())

# --- a session must start whether or not its pad could be wired ---------
sp = TMP / "harness/ten/scratchpad"
code, _, _ = run(seat, payload=json.dumps({"cwd": str(seat)}), scratchpad=sp)
check("no session id is a no-op", code == 0 and not sp.exists())
code, out, _ = run(seat, payload="not json", scratchpad=TMP / "harness/eleven/scratchpad")
check("garbage stdin exits 0", code == 0)

# A session id can arrive in the environment instead of the payload, and a
# SessionStart that carried neither is the only case with nothing to key on.
sp = TMP / "harness/thirteen/scratchpad"
env_run = BASE.copy()
env_run["CLAUDE_CODE_SESSION_ID"] = "sid-from-the-env"
env_run["JSTACK_SCRATCHPAD"] = str(sp)
r = subprocess.run([HOOK], input=json.dumps({"cwd": str(seat)}), env=env_run,
                   cwd=str(seat), capture_output=True, text=True, timeout=25)
check("a session id in the environment is used",
      r.returncode == 0 and sp.is_symlink())

# An unwritable pad location cannot take the session down with it.
locked = agents / "Alpha/social/threads"
sp = TMP / "harness/twelve/scratchpad"
sp.parent.mkdir(parents=True, exist_ok=True)
os.chmod(sp.parent, 0o500)
try:
    code, _, _ = run(locked, scratchpad=sp)
    check("an unwritable target still exits 0", code == 0)
finally:
    os.chmod(sp.parent, 0o700)

print()
if fails:
    print(f"session-start-pad: {len(fails)} FAILED", file=sys.stderr)
    sys.exit(1)
print("session-start-pad: all pass")
PY
