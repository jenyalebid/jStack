"""Run a command in a guest's own Terminal window, visibly, and bring back its exit.

The command travels as its own file and the runner never interpolates it. The
runner is opened through LaunchServices (`open -a Terminal`), never an Apple
Event, closes its own window so pseudo-ttys are not spent, and a window that ran
nothing is repaired by reaping tty holders and opening again. The deadline is
wall clock. Consent rows let the guest's Terminal drive System Events, so a
payload's osascript runs without a click. System doc: ~/Systems/sandbox/SYSTEM.md.
"""
from __future__ import annotations

import subprocess
import tempfile
import time
import uuid

from . import client, settings

RUNNER = r"""#!/bin/bash
set -o pipefail
BASE="${BASH_SOURCE[0]%.command}"
{ bash "$BASE.cmd"; } 2>&1 | tee "$BASE.log"
printf '%s' "${PIPESTATUS[0]}" > "$BASE.exit"
echo
echo "── exit $(cat "$BASE.exit") — this window closes in 10s ──"
sleep 10
/usr/bin/osascript -e "tell application \"Terminal\" to close (every window whose name contains \"$(/usr/bin/basename "$BASE")\") saving no" >/dev/null 2>&1 &
"""

CONSENT = r"""set -u
DB="/Library/Application Support/com.apple.TCC/TCC.db"
UDB="$HOME/Library/Application Support/com.apple.TCC/TCC.db"
[ -f "$DB" ] || exit 0
for PAIR in "com.apple.Terminal|0" "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal|1"; do
  CLIENT="${PAIR%|*}"; TYPE="${PAIR#*|}"
  for SVC in kTCCServiceAccessibility kTCCServicePostEvent kTCCServiceSystemPolicyAllFiles; do
    sudo sqlite3 "$DB" "INSERT OR REPLACE INTO access (service,client,client_type,auth_value,auth_reason,auth_version,indirect_object_identifier_type,indirect_object_identifier,flags,last_modified) VALUES('$SVC','$CLIENT',$TYPE,2,4,1,0,'UNUSED',0,strftime('%s','now'));"
  done
  for TGT in com.apple.Terminal com.apple.systemevents com.apple.finder com.apple.systempreferences com.apple.dock; do
    for D in "$DB" "$UDB"; do
      [ -f "$D" ] || continue
      sudo sqlite3 "$D" "INSERT OR REPLACE INTO access (service,client,client_type,auth_value,auth_reason,auth_version,indirect_object_identifier_type,indirect_object_identifier,flags,last_modified) VALUES('kTCCServiceAppleEvents','$CLIENT',$TYPE,2,4,1,0,'$TGT',0,strftime('%s','now'));"
    done
  done
done
sudo killall tccd 2>/dev/null || true
"""

RESET_TERMINAL = ("defaults write com.apple.Terminal NSQuitAlwaysKeepsWindows -bool false; "
                  "killall -9 Terminal 2>/dev/null; sleep 1; "
                  "ps -ax -o pid,tty | awk '$2 ~ /^ttys/ {print $1}' | xargs kill -9 2>/dev/null; "
                  "sleep 2; rm -rf ~/Library/Saved\\ Application\\ State/com.apple.Terminal.savedState; true")


def _in(lease_id: str, script: str, data: str | None = None) -> tuple[int, str]:
    """Run a shell line in the lease; its output comes back as text."""
    entry = client.held(lease_id)
    with tempfile.TemporaryFile() as feed, tempfile.TemporaryFile() as out:
        if data is not None:
            feed.write(data.encode())
            feed.seek(0)
        code = client.run(client.target_of(entry), lease_id, ["bash", "-c", script],
                          interactive=data is not None, tenant=entry["tenant"],
                          stdin=feed if data is not None else subprocess.DEVNULL,
                          stdout=out)
        out.seek(0)
        return code, out.read().decode(errors="replace")


def consent(lease_id: str) -> None:
    _in(lease_id, "bash -s", CONSENT)


def term(lease_id: str, command: str, say=print) -> int:
    entry = client.held(lease_id)
    if entry["kind"] == "seat":
        raise client.SandboxError("a seat has no screen of its own; take a guest "
                                  "with `get --own` to run in its Terminal")
    conf = settings.load()
    base = f"$HOME/.sandbox-term/t-{uuid.uuid4().hex[:8]}"
    _in(lease_id, f'mkdir -p "$HOME/.sandbox-term" && cat > "{base}.cmd"', command + "\n")
    _in(lease_id, f'cat > "{base}.command" && chmod +x "{base}.command"', RUNNER)
    consent(lease_id)

    def launched(wait: float) -> bool:
        until = time.time() + wait
        while time.time() < until:
            if _in(lease_id, f'[ -f "{base}.log" ]')[0] == 0:
                return True
            time.sleep(3)
        return False

    say(f"running in {entry['guest']}'s Terminal, on {entry['host']}'s display")
    _in(lease_id, f'open -a Terminal "{base}.command"')
    if not launched(conf["term_launch_seconds"]):
        say("its Terminal ran nothing (out of pseudo-ttys); restarting Terminal")
        _in(lease_id, RESET_TERMINAL)
        for _ in range(5):
            if _in(lease_id, f'open -a Terminal "{base}.command"')[0] == 0:
                break
            time.sleep(3)
        if not launched(conf["term_launch_seconds"] + 30):
            raise client.SandboxError("the guest cannot open a working Terminal window; "
                                      "`sandbox reset` it")
    _in(lease_id, 'sleep 1; osascript -e "tell application \\"System Events\\" to '
                  'tell process \\"Terminal\\" to set frontmost to true"')
    deadline = time.time() + conf["term_seconds"]
    while time.time() < deadline:
        if _in(lease_id, f'[ -f "{base}.exit" ]')[0] == 0:
            break
        time.sleep(5)
    else:
        raise client.SandboxError(f"still running after {conf['term_seconds']}s; "
                                  "its window is still up")
    print(_in(lease_id, f'cat "{base}.log"')[1], end="")
    code = _in(lease_id, f'cat "{base}.exit"; rm -f "{base}".*')[1].strip()
    return int(code) if code.isdigit() else 1
