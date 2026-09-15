"""Opt-in delivery compaction, after the owning CLI finishes its turn.

Never resume a live rollout in a second process. Ask the owning terminal to
compact only while idle with an empty composer, and verify its boundary.
"""
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import agent_prefs, codex_transcript, hostenv, managed, transcripts


def facts(path: Path, engine: str) -> dict:
    if engine == "codex":
        result = codex_transcript.summary(path)
    else:
        result = {"context": transcripts.get_session_summary(path).get("last_context", 0)}
    result.update(turn_id="", boundary=0, open_tasks=False, aborted=False)
    with path.open() as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            payload = row.get("payload") or {}
            if row.get("type") == "event_msg":
                kind = payload.get("type")
                if kind == "task_started":
                    result.update(turn_id=payload.get("turn_id", ""), aborted=False)
                elif kind == "turn_aborted":
                    result["aborted"] = True
            if row.get("type") == "compacted" or row.get("subtype") == "compact_boundary":
                result["boundary"] += 1
            # The native plan is another unfinished-work signal. Only parse
            # structured calls; shell/code-mode writes aren't a task API.
            if row.get("type") == "response_item" and payload.get("name", "").split(".")[-1] == "update_plan":
                try:
                    plan = json.loads(payload.get("arguments", "{}"))["plan"]
                    result["open_tasks"] = any(p.get("status") != "completed" for p in plan)
                except (ValueError, KeyError, TypeError):
                    result["open_tasks"] = True
    return result


def empty_composer(screen: str, engine: str) -> bool:
    prefix = "›" if engine == "codex" else "❯"
    lines = screen.splitlines()
    indices = [i for i, line in enumerate(lines) if line.lstrip().startswith(prefix)]
    if not indices:
        return False
    index = indices[-1]
    value = lines[index].strip()[1:].strip()
    allowed = ("", "Ask Codex to do anything") if engine == "codex" else ("",)
    # A multiline draft has a continuation immediately below the prompt.
    return value in allowed and (index + 1 == len(lines) or not lines[index + 1].strip())


def pending_tasks(native_id: str) -> bool:
    for path in (Path.home() / ".claude/tasks" / native_id).glob("*.json"):
        try:
            if json.loads(path.read_text()).get("status") in ("pending", "in_progress"):
                return True
        except (OSError, ValueError):
            return True
    return False


def log(sid, outcome):
    folder = hostenv.state_dir()
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "compact-delivery.jsonl").open("a") as stream:
        stream.write(json.dumps({"time": time.time(), "sid": sid, "outcome": outcome}) + "\n")


def run(payload: dict):
    path = Path(payload.get("transcript_path") or "")
    native_id = payload.get("session_id") or ""
    if not native_id or not path.is_file():
        return
    registry = managed.open_registry()
    match = next(((sid, row) for sid, row in registry.items()
                  if row.get("transcript") == str(path) or sid == native_id), None)
    if not match:
        return
    sid, row = match
    if not agent_prefs.is_on("compact_when_done", row.get("agent", "")):
        return
    engine = row.get("engine", "claude")
    initial = facts(path, engine)
    threshold = int(os.environ.get("JSTACK_DELIVERY_COMPACT_TOKENS", "100000"))
    if initial["context"] < threshold or initial["open_tasks"] or pending_tasks(native_id):
        return
    marker = hostenv.state_dir() / ("compact-delivery-" + sid)
    marker.parent.mkdir(parents=True, exist_ok=True)
    with marker.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        # A Stop callback runs before task_complete is flushed. Wait for that
        # exact turn, then require a quiet empty composer twice.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            time.sleep(1)
            current = facts(path, engine)
            if current["boundary"] != initial["boundary"] or current["aborted"]:
                return
            if current["turn_id"] != initial["turn_id"] or current["open_tasks"] or pending_tasks(native_id):
                return
            if engine == "codex" and current.get("turn") != "idle":
                continue
            screen = subprocess.run(managed._t("capture-pane", "-p", "-t", managed._name(sid)),
                                    capture_output=True, text=True, timeout=3)
            if screen.returncode or not empty_composer(screen.stdout, engine):
                log(sid, "skipped: composer is occupied or unavailable")
                return
            before = path.stat().st_size
            time.sleep(1)
            screen2 = subprocess.run(managed._t("capture-pane", "-p", "-t", managed._name(sid)),
                                     capture_output=True, text=True, timeout=3)
            if screen2.returncode or screen2.stdout != screen.stdout or path.stat().st_size != before:
                continue
            if not managed.send_input(sid, "/compact"):
                return
            log(sid, "requested")
            for _ in range(120):
                time.sleep(1)
                if facts(path, engine)["boundary"] > initial["boundary"]:
                    log(sid, "compacted")
                    return
                if not managed.is_open(sid):
                    break
            log(sid, "failed: no compaction boundary within 120 seconds")
            return


def main():
    if os.environ.get("SKIP_SESSION_HOOK") == "1":
        return
    payload = json.load(sys.stdin)
    subprocess.Popen([sys.executable, "-m", __name__, json.dumps(payload)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


if __name__ == "__main__":
    try:
        run(json.loads(sys.argv[1]))
    except Exception as exc:
        log("", "error: " + str(exc))
